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
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import psycopg

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
