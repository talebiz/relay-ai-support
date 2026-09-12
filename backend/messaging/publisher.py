"""
Publishes commands from Django to RabbitMQ.

This is the ONLY place in the Django codebase that talks to RabbitMQ
for outgoing commands. Views never construct pika connections directly,
for the same reason orders/services.py is the only place that queries
Order/RefundRequest - one chokepoint, one thing to get right, one thing
to test.

Connection lifecycle: we open a short-lived blocking connection per
publish call rather than holding one open for the life of the Django
process. Django (ASGI) handles many concurrent requests; a single
shared pika BlockingConnection is not thread/async-safe to share across
them. A short-lived connection per publish is simple and correct at
this project's scale - the tradeoff (connection setup cost per publish)
is called out here rather than hidden, and would be the first thing to
revisit under real load (e.g. a small connection pool).
"""

from __future__ import annotations

import json
import os

import pika

from messaging.envelope import Envelope, MessageType, new_correlation_id
from messaging.topology import COMMAND_QUEUE, declare_topology

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "guest")


def _connection() -> pika.BlockingConnection:
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
    parameters = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        credentials=credentials,
    )
    return pika.BlockingConnection(parameters)


def publish_command(
    message_type: MessageType,
    payload: dict,
    correlation_id: str | None = None,
) -> Envelope:
    """
    Publish one command to the AI worker's queue.

    `correlation_id`: pass the id already generated for this HTTP
    request/conversation if one exists; otherwise a new one is minted
    here. This is deliberately the ONE place a correlation_id is
    allowed to originate for anything travelling through RabbitMQ -
    everything downstream (worker, events, Langfuse traces, SSE) must
    receive and forward it, never invent a new one.

    Returns the Envelope that was published so the caller (typically a
    DRF view) can hand `correlation_id` back to the frontend, e.g. so
    the browser knows which SSE messages belong to this request.
    """
    envelope = Envelope.new_command(
        message_type=message_type,
        payload=payload,
        correlation_id=correlation_id or new_correlation_id(),
        producer="backend",
    )

    connection = _connection()
    try:
        channel = connection.channel()
        declare_topology(channel)

        channel.basic_publish(
            exchange="",
            routing_key=COMMAND_QUEUE,
            body=json.dumps(envelope.to_dict()).encode("utf-8"),
            properties=pika.BasicProperties(
                # Persist the message to disk so it survives a broker
                # restart while sitting in a durable queue. Paired with
                # the queue's own durable=True (see topology.py) - both
                # are required, one without the other does not survive
                # a restart.
                delivery_mode=pika.DeliveryMode.Persistent,
                content_type="application/json",
                correlation_id=envelope.correlation_id,
                message_id=envelope.message_id,
                type=envelope.message_type.value,
            ),
        )
    finally:
        connection.close()

    return envelope
