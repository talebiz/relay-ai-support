"""
RabbitMQ topology: exchange/queue names and the retry/DLQ wiring.

This module only DECLARES topology (queue_declare/exchange_declare are
idempotent in RabbitMQ - calling them repeatedly with the same
arguments is a no-op). Both the Django publisher and the ai-worker
consumer call `declare_topology(channel)` before doing anything else,
so whichever process starts first "wins" and the other just confirms
the same shapes already exist. Nothing here assumes a particular
startup order.

--- Commands: one queue, one consumer (the AI worker) ---

    backend  --publish-->  [agent_commands]  --consume-->  ai-worker

Reliability requirements from the project brief section 3(b):

    "تأیید دستی پس از پردازش، تلاش مجدد با تأخیر نمایی Exponential
    backoff و idempotency که در پایگاه داده اعمال شود."

    (manual ack after processing, retry with exponential backoff, and
    idempotency enforced in the database)

Manual ack is a per-message consumer behavior (see ai-worker/worker.py),
not something declared in topology. What IS topology is retry/backoff
and the dead-letter path:

    agent_commands            <--- redeliver on retry ---+
        |  (nack after handler raises)                   |
        v                                                 |
    agent_commands.retry.1s   (TTL 1s,  DLX -> agent_commands) --+
        (message expires -> dead-lettered back to agent_commands)
    agent_commands.retry.4s   (TTL 4s,  DLX -> agent_commands)
    agent_commands.retry.16s  (TTL 16s, DLX -> agent_commands)
        |
        v (after MAX_RETRIES attempts, worker publishes to DLQ itself)
    agent_commands.dlq

We use RabbitMQ's built-in Dead Letter Exchange (DLX) + per-queue
message TTL for backoff - this needs no broker plugin, just the
standard `x-dead-letter-exchange` / `x-message-ttl` queue arguments
that ship with every RabbitMQ image. Exponential backoff is approximated
with a small, fixed staircase of retry queues (1s -> 4s -> 16s) rather
than a single queue whose TTL changes per message, because RabbitMQ has
no notion of "per-message dynamic TTL based on a header" - TTL is a
queue-level (or at best per-publish) property, and the simplest
standard pattern for staged backoff is exactly this: one short-lived
queue per backoff stage, each dead-lettering back to the main queue
once its message expires.

After MAX_COMMAND_RETRIES failed attempts, the worker publishes the
message directly to `agent_commands.dlq` (a plain durable queue, no
consumer in this project) instead of retrying again. Messages there are
inspectable via the RabbitMQ management UI - see README for the
operational answer to "what happens to a command that keeps failing".

--- Events: zero or more consumers, fan-out ---

Per the command/event distinction, an event's publisher does not know
or care who (if anyone) is listening. We model that literally with a
`fanout` exchange: the AI worker publishes every event to
`agent_events_exchange` without naming a queue. Each SSE connection
(see backend/streaming/views.py) declares its OWN exclusive,
auto-delete queue bound to that exchange when a client connects, and
that queue disappears when the client disconnects. This mirrors the
brief's requirement (section 6) that "هر اتصال اشتراک کوتاه‌عمر خودش را
داشته باشد" (each connection has its own short-lived subscription).
"""

from __future__ import annotations

import pika

# --- Commands ---
COMMAND_EXCHANGE = ""  # default exchange; we publish directly to the queue by name
COMMAND_QUEUE = "agent_commands"

RETRY_STAGES_MS = [1_000, 4_000, 16_000]  # 1s, 4s, 16s - the backoff staircase
MAX_COMMAND_RETRIES = len(RETRY_STAGES_MS)

DLQ_QUEUE = "agent_commands.dlq"


def _retry_queue_name(stage_index: int) -> str:
    ttl_seconds = RETRY_STAGES_MS[stage_index] // 1000
    return f"agent_commands.retry.{ttl_seconds}s"


# --- Events ---
EVENTS_EXCHANGE = "agent_events_exchange"


def declare_topology(channel: pika.channel.Channel) -> None:
    """
    Idempotently declare every queue/exchange this system needs.
    Safe to call from both the Django publisher and the ai-worker
    consumer, in any order, any number of times.
    """
    # Main command queue. Durable so it survives a RabbitMQ restart.
    # Messages that exhaust their TTL in a retry queue land back here
    # via dead-lettering (see retry queues below).
    channel.queue_declare(queue=COMMAND_QUEUE, durable=True)

    # DLQ: final resting place for commands that failed
    # MAX_COMMAND_RETRIES times. No consumer in this project; inspected
    # manually via the RabbitMQ management UI (see README).
    channel.queue_declare(queue=DLQ_QUEUE, durable=True)

    # One retry queue per backoff stage. Each has:
    #   - x-message-ttl: how long a message waits here before expiring
    #   - x-dead-letter-exchange="" + x-dead-letter-routing-key=COMMAND_QUEUE:
    #     where an EXPIRED message goes next (back to the main queue,
    #     for another delivery attempt).
    # No consumer ever reads from a retry queue directly - messages
    # only ever leave it by expiring (TTL) and being dead-lettered.
    for stage_index, ttl_ms in enumerate(RETRY_STAGES_MS):
        channel.queue_declare(
            queue=_retry_queue_name(stage_index),
            durable=True,
            arguments={
                "x-message-ttl": ttl_ms,
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": COMMAND_QUEUE,
            },
        )

    # Events fan out to whoever happens to be listening right now (or
    # to no one). SSE views bind their own exclusive queue to this at
    # connection time (see backend/streaming/views.py).
    channel.exchange_declare(exchange=EVENTS_EXCHANGE, exchange_type="fanout", durable=True)


def retry_queue_for_attempt(retry_count: int) -> str | None:
    """
    Given how many times a command has already been retried, return the
    name of the retry queue to publish it to next, or None if the retry
    budget is exhausted (caller should route to the DLQ instead).
    """
    if retry_count >= MAX_COMMAND_RETRIES:
        return None
    return _retry_queue_name(retry_count)
