"""
Shared message envelope - AI worker side.

This is intentionally the same shape as backend/messaging/envelope.py.
See that file's docstring for the full design rationale (message_id vs
correlation_id, schema_version, why command and event share one shape,
and why this module is duplicated rather than imported across the two
services/images). Keep the two files in sync by hand when the envelope
shape changes - that tradeoff is documented in the README.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


ENVELOPE_SCHEMA_VERSION = 1


class MessageKind(str, Enum):
    COMMAND = "command"
    EVENT = "event"


class MessageType(str, Enum):
    # --- Commands: exactly one consumer (this worker) ---
    PROCESS_USER_MESSAGE = "command.process_user_message"

    # --- Events: zero or more consumers (currently: the SSE view) ---
    AGENT_RUN_STARTED = "event.agent_run_started"
    AGENT_RESPONSE_CHUNK = "event.agent_response_chunk"
    AGENT_TOOL_CALLED = "event.agent_tool_called"
    AGENT_RUN_COMPLETED = "event.agent_run_completed"
    AGENT_RUN_FAILED = "event.agent_run_failed"


def new_message_id() -> str:
    return str(uuid.uuid4())


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Envelope:
    """See backend/messaging/envelope.py for full field documentation."""

    message_id: str
    correlation_id: str
    kind: MessageKind
    message_type: MessageType
    payload: dict[str, Any]
    schema_version: int = ENVELOPE_SCHEMA_VERSION
    occurred_at: str = field(default_factory=utc_now_iso)
    producer: str = "ai-worker"
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

    def with_incremented_retry(self) -> "Envelope":
        """Used when re-publishing this same command to a retry queue."""
        return Envelope(
            message_id=self.message_id,
            correlation_id=self.correlation_id,
            kind=self.kind,
            message_type=self.message_type,
            payload=self.payload,
            schema_version=self.schema_version,
            occurred_at=self.occurred_at,
            producer=self.producer,
            retry_count=self.retry_count + 1,
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
