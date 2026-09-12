"""
Shared message envelope for everything that crosses RabbitMQ between
Django (backend) and the AI worker (ai-worker service).

Project brief, section 3(a):

    "همه پیام ها باید ساختار یکسان و نسخه‌دار داشته باشند، شامل شناسه
    رویداد، correlation_id و زمان. طراحی این ساختار با شماست."

    ("All messages must share one versioned structure, including an
    event id, a correlation_id, and a timestamp. Designing this
    structure is your responsibility.")

Design decisions (be ready to defend these):

1. ONE envelope shape for both commands and events. A command and an
   event differ in *meaning* (do this / this happened), not in
   *transport shape* - both need an id, a correlation id, a timestamp,
   and a schema version. Giving them the same envelope means any piece
   of code that logs, traces, or replays messages can do so generically,
   without a type switch. What differs is `message_type`, `kind`
   ("command" or "event") and, of course, `payload`.

2. `message_id` vs `correlation_id` - these are NOT the same thing:
   - `message_id` (aka event_id) identifies THIS specific message on
     the wire. It is the idempotency key: the same message redelivered
     by RabbitMQ (at-least-once delivery) carries the same message_id,
     so the consumer can recognize "I already did this."
   - `correlation_id` identifies ONE end-to-end user interaction. A
     single correlation_id is attached to the original HTTP request,
     copied onto the command that request produces, copied onto every
     event the worker publishes while handling that command, and
     forwarded to Langfuse as a trace attribute. It is how a human in
     the defense session can pick one id and follow it through Django
     logs, the RabbitMQ message, the worker's processing, and the SSE
     stream reaching React (project brief, section 3(a) and section 6.1).

3. `schema_version` is a plain integer on the envelope itself (not
   encoded only in the queue/exchange name), so a consumer can look at
   one field and decide "I understand this version" before touching
   `payload` at all - important once this system evolves and old and
   new producers/consumers might briefly coexist.

4. This module is intentionally duplicated (not imported) between
   backend/ and ai-worker/. They are two separate deployable Python
   processes/images with independent dependency lists; sharing a
   package between them would mean either a private PyPI package or a
   volume-mounted shared folder, both of which are more operational
   complexity than this project's size justifies. The tradeoff is
   explicit: if the envelope shape changes, it must be changed in two
   places. This is called out in the README.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


ENVELOPE_SCHEMA_VERSION = 1


class MessageKind(str, Enum):
    """
    Command vs event, per project brief section 3(a):

        "command یعنی «این کار را انجام بده» و یک مصرف کننده دارد؛
        event یعنی «این اتفاق افتاد» و ممکن است هیچ مصرف کننده‌ای
        نداشته باشد."

    COMMAND: an imperative instruction with exactly one intended
    consumer (the AI worker). If it fails, that is a real failure -
    someone was depending on it being carried out - so commands are
    the ones subject to manual ack, retry-with-backoff, and the DLQ.

    EVENT: a statement of fact about something that already happened.
    Zero or more consumers may care. The publisher does not know or
    control who (if anyone) is listening. Events in this system are
    fanned out (see messaging/topology.py) rather than delivered to one
    named queue, and are not retried the same way commands are - a
    missed UI update is not the same class of problem as a dropped
    instruction.
    """

    COMMAND = "command"
    EVENT = "event"


class MessageType(str, Enum):
    """
    Concrete catalogue of every command/event this system defines.

    Keeping this as an explicit enum (rather than free-form strings
    scattered across the codebase) is what makes the "list of events"
    required by section 3(a) of the brief a single, greppable source of
    truth instead of something reconstructed by hand for the README.
    """

    # --- Commands: exactly one consumer (the AI worker) ---
    PROCESS_USER_MESSAGE = "command.process_user_message"

    # --- Events: zero or more consumers (currently: the SSE view) ---
    AGENT_RUN_STARTED = "event.agent_run_started"
    AGENT_RESPONSE_CHUNK = "event.agent_response_chunk"
    AGENT_TOOL_CALLED = "event.agent_tool_called"
    AGENT_RUN_COMPLETED = "event.agent_run_completed"
    AGENT_RUN_FAILED = "event.agent_run_failed"


def new_message_id() -> str:
    return str(uuid.uuid4())


def new_correlation_id() -> str:
    """
    Called once, at the point a user-initiated interaction begins
    (the Django view that first receives the chat message). Every
    downstream command/event for that interaction reuses this value -
    it must never be regenerated partway through the pipeline.
    """
    return str(uuid.uuid4())


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Envelope:
    """
    The one message shape used for every command and every event.

    Fields:
        message_id:      Unique id of THIS message. Idempotency key.
        correlation_id:  Id shared by every message belonging to the
                          same end-to-end user interaction.
        kind:            "command" or "event" (MessageKind).
        message_type:    Concrete type (MessageType), e.g.
                          "command.process_user_message".
        schema_version:  Integer version of the *payload* shape for
                          this message_type. Independent per
                          message_type so evolving one command/event
                          doesn't force a bump on every other one.
        occurred_at:     ISO-8601 UTC timestamp of when this message
                          was created (not when it is processed).
        producer:        Name of the service that created the message,
                          for debugging (e.g. "backend", "ai-worker").
        payload:         The message-type-specific data.
        retry_count:     How many times this exact logical command has
                          already been retried. Only meaningful for
                          commands; always 0 for events. Carried on the
                          envelope itself (rather than only inferred
                          from RabbitMQ's `x-death` header) so the
                          worker's own backoff/DLQ logic does not have
                          to parse broker-internal headers to decide
                          whether it has hit the retry ceiling.
    """

    message_id: str
    correlation_id: str
    kind: MessageKind
    message_type: MessageType
    payload: dict[str, Any]
    schema_version: int = ENVELOPE_SCHEMA_VERSION
    occurred_at: str = field(default_factory=utc_now_iso)
    producer: str = "backend"
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "correlation_id": self.correlation_id,
            "kind": self.kind.value,
            "message_type": self.message_type.value,
            "schema_version": self.schema_version,
            "occurred_at": self.occurred_at,
            "producer": self.producer,
            "retry_count": self.retry_count,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Envelope":
        return cls(
            message_id=data["message_id"],
            correlation_id=data["correlation_id"],
            kind=MessageKind(data["kind"]),
            message_type=MessageType(data["message_type"]),
            payload=data.get("payload", {}),
            schema_version=data.get("schema_version", ENVELOPE_SCHEMA_VERSION),
            occurred_at=data.get("occurred_at", utc_now_iso()),
            producer=data.get("producer", "unknown"),
            retry_count=data.get("retry_count", 0),
        )

    @classmethod
    def new_command(
        cls,
        message_type: MessageType,
        payload: dict[str, Any],
        correlation_id: str,
        producer: str = "backend",
    ) -> "Envelope":
        return cls(
            message_id=new_message_id(),
            correlation_id=correlation_id,
            kind=MessageKind.COMMAND,
            message_type=message_type,
            payload=payload,
            producer=producer,
        )

    @classmethod
    def new_event(
        cls,
        message_type: MessageType,
        payload: dict[str, Any],
        correlation_id: str,
        producer: str = "ai-worker",
    ) -> "Envelope":
        return cls(
            message_id=new_message_id(),
            correlation_id=correlation_id,
            kind=MessageKind.EVENT,
            message_type=message_type,
            payload=payload,
            producer=producer,
        )
