"""
Publishes events from the AI worker to the fanout events exchange.

Every event MUST carry the same correlation_id as the command that
triggered it - this is what lets the SSE view (backend/streaming/views.py)
find the right browser connection to deliver it to, and what lets a
Langfuse trace and this event stream be tied back together during the
defense (project brief 3(a): "correlation_id باید کل مسیر را به هم وصل
کند: درخواست، فرمان، اجرای worker، رد Langfuse و SSE").
"""

from __future__ import annotations

import json

import pika

from envelope import Envelope, MessageType
from topology import EVENTS_EXCHANGE, declare_topology


def publish_event(
    channel: pika.channel.Channel,
    message_type: MessageType,
    payload: dict,
    correlation_id: str,
) -> Envelope:
    """
    Publish one event to the fanout exchange. Does not know or care
    whether anyone is subscribed right now - that is the defining
    property of an event vs a command (see envelope.py MessageKind
    docstring). If no SSE connection happens to be listening, the
    event is simply not delivered to anyone; nothing retries it,
    because nothing was ever guaranteed to consume it.
    """
    declare_topology(channel)

    envelope = Envelope.new_event(
        message_type=message_type,
        payload=payload,
        correlation_id=correlation_id,
    )

    channel.basic_publish(
        exchange=EVENTS_EXCHANGE,
        routing_key="",  # ignored by a fanout exchange
        body=json.dumps(envelope.to_dict()).encode("utf-8"),
        properties=pika.BasicProperties(
            content_type="application/json",
            correlation_id=envelope.correlation_id,
            message_id=envelope.message_id,
            type=envelope.message_type.value,
        ),
    )
    return envelope
