"""
RabbitMQ topology - AI worker side. Same shape as
backend/messaging/topology.py; see that file's docstring for the full
retry/DLQ design rationale. Duplicated for the same reason as
envelope.py (two independent deployable services).
"""

from __future__ import annotations

import pika

COMMAND_QUEUE = "agent_commands"

RETRY_STAGES_MS = [1_000, 4_000, 16_000]  # 1s, 4s, 16s backoff staircase
MAX_COMMAND_RETRIES = len(RETRY_STAGES_MS)

DLQ_QUEUE = "agent_commands.dlq"

EVENTS_EXCHANGE = "agent_events_exchange"


def _retry_queue_name(stage_index: int) -> str:
    ttl_seconds = RETRY_STAGES_MS[stage_index] // 1000
    return f"agent_commands.retry.{ttl_seconds}s"


def declare_topology(channel: pika.channel.Channel) -> None:
    """Idempotent: safe to call every time the worker starts."""
    channel.queue_declare(queue=COMMAND_QUEUE, durable=True)
    channel.queue_declare(queue=DLQ_QUEUE, durable=True)

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

    channel.exchange_declare(exchange=EVENTS_EXCHANGE, exchange_type="fanout", durable=True)


def retry_queue_for_attempt(retry_count: int) -> str | None:
    """
    Which retry queue a command with this many prior attempts should
    go to next, or None if MAX_COMMAND_RETRIES is already reached (in
    which case the caller routes to DLQ_QUEUE instead).
    """
    if retry_count >= MAX_COMMAND_RETRIES:
        return None
    return _retry_queue_name(retry_count)
