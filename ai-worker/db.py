"""
Direct PostgreSQL access for the AI worker.

The worker is a plain Python process, not a Django app - it does not
load Django's ORM or settings. It connects to the SAME PostgreSQL
database Django uses (shared resource, per project brief section 3
intro: "RabbitMQ scope is narrow - only the backend<->AI-worker bridge;
PostgreSQL and ChromaDB are shared resources accessed directly by
multiple services") using plain psycopg, against the `messaging_processedmessage`
table that Django's migration (backend/messaging/migrations/0001_initial.py)
created.

This is the database-enforced half of idempotency described in that
same migration's model docstring (backend/messaging/models.py):
whichever process INSERTs a message_id first "wins"; a second attempt
to insert the same message_id hits the UNIQUE constraint and is
recognized as already-handled.

Two functions are exposed, used in two different moments by
worker.py::handle_command (see that module's docstring, "Known
limitation" section, for the full reasoning):

    already_processed(message_id)  - a cheap, early, READ-ONLY check,
        called BEFORE doing any agent work. Lets a redelivered message
        skip re-running the agent (and re-publishing its events)
        entirely, in the common case.

    mark_processed(message_id, ...) - the actual INSERT that claims
        this message_id, called AFTER the agent work is done, as the
        very last step before ack. This is what makes the guarantee
        real (a UNIQUE constraint, not just the early check, which by
        itself would still have a race window between two concurrent
        deliveries).

Neither function on its own is a complete guarantee - see the module
docstring in worker.py for the honest limitation this leaves.
"""

from __future__ import annotations

import os
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
