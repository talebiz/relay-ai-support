"""
Direct PostgreSQL access for the AI worker.

The worker is a plain Python process, not a Django app - it does not
load Django's ORM or settings. It connects to the SAME PostgreSQL
database Django uses (shared resource, per project brief section 3
intro: "RabbitMQ scope is narrow - only the backend<->AI-worker bridge;
PostgreSQL and ChromaDB are shared resources accessed directly by
multiple services") using plain psycopg.

Two groups of functions live here:

  Idempotency (project brief 3(b), against the
  `messaging_processedmessage` table Django's migration
  (backend/messaging/migrations/0001_initial.py) created):

    already_processed(message_id)  - a cheap, early, READ-ONLY check,
        called BEFORE doing any agent work. Lets a redelivered message
        skip re-running the agent (and re-publishing its events)
        entirely, in the common case.

    mark_processed(message_id, ...) - the actual INSERT that claims
        this message_id, called AFTER the agent work is done, as the
        very last step before ack. See worker.py's module docstring
        ("Known limitation") for the honest limit this leaves.

  Conversation history (part 4, against the `conversations_message`
  table Django's migration (backend/conversations/migrations/0001_initial.py)
  created) - the worker's read/write access to the system of record for
  chat messages, described in streaming/views.py's docstring:

    fetch_conversation_history(conversation_id) - read the messages
        recorded so far for one conversation, oldest first, to build
        the prompt for the LLM.

    save_assistant_message(conversation_id, content) - persist the
        assistant's finished reply. This, not the RabbitMQ event
        stream, is what makes a reply durable and re-fetchable after
        an SSE reconnect.

Neither idempotency function on its own is a complete guarantee - see
the module docstring in worker.py for the honest limitation this
leaves.

Part 4 commit 2 adds a third group: authorization-scoped reads/writes
against `orders_order` and `orders_refundrequest`
(fetch_order_for_user, fetch_refund_history_for_order,
insert_refund_request). These exist because the two database tools
(tools/order_tools.py, tools/action_tools.py) need to run the EXACT
SAME "a user may only see their own rows, unless they are a support
agent" rule that backend/orders/services.py enforces for the human-
facing REST API - the worker cannot import that Django module (it is
not a Django app, see this file's top docstring), so the rule is
re-implemented here in plain SQL. This is intentional duplication, not
an oversight: see tools/order_tools.py's module docstring for why
duplicating the RULE (while sharing nothing else) is exactly what
"authorization lives in code, not in the prompt" requires even across
two separate services.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "relay")
DB_USER = os.getenv("POSTGRES_USER", "relay_user")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")


def _connect() -> psycopg.Connection:
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


@contextmanager
def transaction():
    """
    One connection, one transaction, for a single database operation
    that must commit atomically. Callers do their work inside this
    `with` block, using the yielded cursor.
    """
    conn = _connect()
    try:
        with conn:  # psycopg commits on clean exit, rolls back on exception
            with conn.cursor() as cur:
                yield cur
    finally:
        conn.close()


@contextmanager
def transaction_dict_rows():
    """
    Same as `transaction()`, but the cursor returns dict rows (column
    name -> value) instead of plain tuples. Used by the read-heavy
    tool functions below, where returning a dict straight to the LLM
    (as tool output) is far less error-prone than positional tuple
    unpacking that would silently break if a SELECT's column order
    ever changed.
    """
    conn = _connect()
    try:
        with conn:
            with conn.cursor(row_factory=dict_row) as cur:
                yield cur
    finally:
        conn.close()


class DuplicateMessageError(Exception):
    """
    Raised by `mark_processed` when this message_id already has a row -
    i.e. this exact message was already (or is concurrently being)
    handled by another delivery. The caller acks the message as normal
    without repeating any side-effecting work.
    """


def already_processed(message_id: str) -> bool:
    """
    Cheap, early check: has this message_id already been recorded as
    processed? Called BEFORE running the agent, so a redelivered
    message (project brief 3(b), Q1: "same command arrives twice")
    normally short-circuits here without re-running anything or
    re-publishing any event.

    This is a plain SELECT, not a lock - it does not by itself prevent
    two concurrent deliveries from both passing this check at the same
    moment. That race is closed by `mark_processed`'s UNIQUE constraint
    at the end, not by this function. This function only exists to
    make the COMMON case (a later redelivery, well after the first one
    finished) cheap and side-effect-free.
    """
    with transaction() as cursor:
        cursor.execute(
            "SELECT 1 FROM messaging_processedmessage WHERE message_id = %s",
            (message_id,),
        )
        return cursor.fetchone() is not None


def mark_processed(message_id: str, correlation_id: str, message_type: str) -> None:
    """
    Record `message_id` as processed. Called AFTER the agent has
    already done its work (and published its events), as the last
    step before the message is ack'd - see worker.py's module
    docstring ("Known limitation") for why this function is called
    last rather than first.

    Raises DuplicateMessageError if a row for this message_id already
    exists (UNIQUE constraint on messaging_processedmessage.message_id).
    This is the real guarantee - unlike `already_processed`, this is
    safe even if two deliveries reach this point concurrently: only
    one INSERT can win.
    """
    try:
        with transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO messaging_processedmessage
                    (message_id, correlation_id, message_type, processed_at)
                VALUES (%s, %s, %s, now())
                """,
                (message_id, correlation_id, message_type),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise DuplicateMessageError(message_id) from exc


def fetch_conversation_history(conversation_id: str, limit: int = 20) -> list[dict]:
    """
    Reads up to the last `limit` messages (oldest first) for one
    conversation directly from the `conversations_message` table
    Django's migration created. Plain SQL, not the ORM, for the same
    reason as the idempotency functions above: this process is not a
    Django app.

    `limit` bounds the prompt size sent to the LLM - a long-running
    conversation should not silently grow the request (and the bill)
    without limit. 20 turns is a simple, generous-enough default for
    this project's scale; a production system would summarize older
    history instead of just truncating it.
    """
    with transaction() as cursor:
        cursor.execute(
            """
            SELECT role, content
            FROM conversations_message
            WHERE conversation_id = %s
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (conversation_id, limit),
        )
        rows = cursor.fetchall()
    return [{"role": role, "content": content} for role, content in rows]


def save_assistant_message(conversation_id: str, content: str) -> str:
    """
    Persists the assistant's finished reply as the system of record
    (project brief section 6: the database, not the SSE stream, is
    the source of truth for "what did the assistant actually say").
    Returns the new message's id.

    `tool_calls_json` is left NULL here - part 4 commit 1 has no real
    tools yet. Commit 2 will pass the tool-call trace through once the
    LangGraph agent exists.
    """
    message_id = str(uuid.uuid4())
    with transaction() as cursor:
        cursor.execute(
            """
            INSERT INTO conversations_message
                (id, conversation_id, role, content, tool_calls_json, created_at)
            VALUES (%s, %s, %s, %s, NULL, now())
            """,
            (message_id, conversation_id, "assistant", content),
        )
    return message_id


def save_assistant_message_with_tool_calls(
    conversation_id: str, content: str, tool_calls_json: str | None
) -> str:
    """
    Same as `save_assistant_message`, but also records the tool-call
    trace (as a JSON string, or None if the agent used no tools for
    this turn) in `Message.tool_calls_json` - the field
    conversations/models.py added specifically so a future "agent
    activity" panel in the UI can render which tools were used,
    without re-deriving that from the AGENT_TOOL_CALLED events (which,
    unlike this row, are not guaranteed to still be reachable after an
    SSE reconnect - see conversations/models.py's docstring, point 4).
    """
    message_id = str(uuid.uuid4())
    with transaction() as cursor:
        cursor.execute(
            """
            INSERT INTO conversations_message
                (id, conversation_id, role, content, tool_calls_json, created_at)
            VALUES (%s, %s, %s, %s, %s, now())
            """,
            (message_id, conversation_id, "assistant", content, tool_calls_json),
        )
    return message_id


def _acting_user_is_support_agent(acting_user_id: int) -> bool:
    """
    Re-derives the exact same role check as
    accounts/permissions.py::IsSupportAgent, but from the database
    directly rather than a Django `request.user` object, since the
    worker has no Django request cycle. `role` is read from
    `accounts_user` - the same column, the same values
    ("support_agent"), the same authorization meaning - never
    something the LLM is asked about or could influence.
    """
    with transaction_dict_rows() as cursor:
        cursor.execute(
            "SELECT role FROM accounts_user WHERE id = %s",
            (acting_user_id,),
        )
        row = cursor.fetchone()
    return bool(row) and row["role"] == "support_agent"


def fetch_order_for_user(acting_user_id: int, order_id: str) -> dict | None:
    """
    The tool-side equivalent of backend/orders/services.py::get_order_for_user.

    Returns the order as a plain dict (joined with product name/price
    so the tool's output is immediately useful to the LLM without a
    second lookup), or None if the order does not exist OR does not
    belong to `acting_user_id` and the acting user is not a support
    agent - deliberately indistinguishable, the same "can't tell
    missing from forbidden" property the REST API guarantees
    (orders/services.py's docstring explains why that distinction
    itself would leak information).

    This is a SELECT with the ownership check baked directly into the
    WHERE clause (support agents get no WHERE-user filter at all)
    rather than "fetch then check in Python" - so there is no code
    path where a row for another user's order is ever pulled into this
    process's memory in the first place, for a plain customer.
    """
    try:
        uuid.UUID(str(order_id))
    except (ValueError, TypeError):
        return None
    
    is_agent = _acting_user_is_support_agent(acting_user_id)

    query = """
        SELECT
            o.id, o.quantity, o.status, o.tracking_number,
            o.created_at, o.user_id,
            p.name AS product_name, p.price_cents,
            u.username
        FROM orders_order o
        JOIN orders_product p ON p.id = o.product_id
        JOIN accounts_user u ON u.id = o.user_id
        WHERE o.id = %s
    """
    params: tuple = (order_id,)
    if not is_agent:
        query += " AND o.user_id = %s"
        params = (order_id, acting_user_id)

    with transaction_dict_rows() as cursor:
        cursor.execute(query, params)
        row = cursor.fetchone()

    if row is None:
        return None

    row["total_cents"] = row["price_cents"] * row["quantity"]
    return row


def fetch_refund_history_for_order(acting_user_id: int, order_id: str) -> list[dict] | None:
    """
    The tool-side equivalent of
    backend/orders/services.py::refund_history_for_order.

    Returns None (not an empty list) if the order itself is not
    visible to `acting_user_id`, so the caller (tools/order_tools.py)
    can distinguish "order not found/not yours" from "order exists,
    zero refund requests on file" - the same distinction
    orders/services.py preserves by resolving the order first via
    get_order_for_user before ever touching RefundRequest.
    """
    order = fetch_order_for_user(acting_user_id, order_id)
    if order is None:
        return None

    with transaction_dict_rows() as cursor:
        cursor.execute(
            """
            SELECT id, reason, status, resolution_note, created_at
            FROM orders_refundrequest
            WHERE order_id = %s
            ORDER BY created_at DESC
            """,
            (order_id,),
        )
        return cursor.fetchall()


def insert_refund_request(acting_user_id: int, order_id: str, reason: str) -> dict | None:
    """
    The tool-side equivalent of
    backend/orders/services.py::create_refund_request - the one
    "action" tool the project brief requires at least one of (section
    2: at least one tool that performs an action rather than just
    reading data).

    Re-resolves the order through `fetch_order_for_user` FIRST, so a
    customer cannot file a refund against an order that is not
    visible to them by guessing a UUID - identical ownership rule as
    every read path above, applied here to a write. The refund is
    always recorded against the ORDER's owner (`order["user_id"]`),
    matching create_refund_request's own comment: a support agent
    filing this on a customer's behalf still credits the customer, not
    the agent.

    Returns None if the order isn't visible to acting_user_id (caller
    reports "order not found"); otherwise the newly created refund
    request as a dict.
    """
    try:
        uuid.UUID(str(order_id))
    except (ValueError, TypeError):
        return None
    
    order = fetch_order_for_user(acting_user_id, order_id)
    if order is None:
        return None

    refund_id = str(uuid.uuid4())
    with transaction() as cursor:
        cursor.execute(
            """
            INSERT INTO orders_refundrequest
                (id, user_id, order_id, reason, status, resolution_note, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'requested', '', now(), now())
            """,
            (refund_id, order["user_id"], order_id, reason),
        )

    return {
        "id": refund_id,
        "order_id": order_id,
        "reason": reason,
        "status": "requested",
    }
