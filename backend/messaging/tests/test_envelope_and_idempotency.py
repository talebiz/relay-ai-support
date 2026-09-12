"""
Tests for the two claims we need to make checkable in the defense
(project brief 3(b)):

1. The envelope round-trips (to_dict/from_dict) without losing the
   fields that matter for correlation/idempotency/versioning.
2. The database actually rejects a second row with the same
   message_id - i.e. the idempotency guarantee is a real constraint,
   not just a convention the application code happens to follow.

Run with: pytest backend/messaging/tests/ -v
"""

import uuid

import pytest
from django.db import IntegrityError

from messaging.envelope import Envelope, MessageKind, MessageType
from messaging.models import ProcessedMessage


def test_envelope_round_trip_preserves_correlation_and_ids():
    envelope = Envelope.new_command(
        message_type=MessageType.PROCESS_USER_MESSAGE,
        payload={"text": "where is my order"},
        correlation_id=str(uuid.uuid4()),
    )

    restored = Envelope.from_dict(envelope.to_dict())

    assert restored.message_id == envelope.message_id
    assert restored.correlation_id == envelope.correlation_id
    assert restored.kind == MessageKind.COMMAND
    assert restored.message_type == MessageType.PROCESS_USER_MESSAGE
    assert restored.schema_version == envelope.schema_version
    assert restored.payload == envelope.payload


def test_new_command_and_new_event_share_the_same_correlation_id():
    """
    The whole point of correlation_id: a command and every event it
    causes must carry the SAME value, even though each gets its own
    unique message_id.
    """
    correlation_id = str(uuid.uuid4())
    command = Envelope.new_command(
        message_type=MessageType.PROCESS_USER_MESSAGE,
        payload={},
        correlation_id=correlation_id,
    )
    event = Envelope.new_event(
        message_type=MessageType.AGENT_RUN_STARTED,
        payload={},
        correlation_id=correlation_id,
    )

    assert command.correlation_id == event.correlation_id
    assert command.message_id != event.message_id


@pytest.mark.django_db
def test_processed_message_rejects_duplicate_message_id():
    """
    This is the database-level guarantee db.py (ai-worker side) relies
    on: a second INSERT with the same message_id must fail, which is
    what makes "the same command arrives twice" a safe no-op instead
    of a duplicate side effect.
    """
    message_id = uuid.uuid4()

    ProcessedMessage.objects.create(
        message_id=message_id,
        correlation_id=uuid.uuid4(),
        message_type=MessageType.PROCESS_USER_MESSAGE.value,
    )

    with pytest.raises(IntegrityError):
        ProcessedMessage.objects.create(
            message_id=message_id,  # same message_id again
            correlation_id=uuid.uuid4(),
            message_type=MessageType.PROCESS_USER_MESSAGE.value,
        )


@pytest.mark.django_db
def test_processed_message_allows_different_message_ids():
    ProcessedMessage.objects.create(
        message_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        message_type=MessageType.PROCESS_USER_MESSAGE.value,
    )
    ProcessedMessage.objects.create(
        message_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        message_type=MessageType.PROCESS_USER_MESSAGE.value,
    )

    assert ProcessedMessage.objects.count() == 2
