"""
Entry point for the AI worker process.

This process is intentionally separate from the Django web process
(project brief, section 3 intro): response generation must never block
a web worker. It connects to RabbitMQ, consumes "command" messages
(currently: PROCESS_USER_MESSAGE), and publishes "event" messages back
out for the SSE view to relay to the browser.

What this file implements (project brief, section 3):
    (a) Parses the shared, versioned envelope (envelope.py) and
        publishes events carrying the same correlation_id as the
        command that triggered them.
    (b) Reliability: manual ack after successful processing, retry
        with exponential backoff via staged retry queues, and
        idempotency enforced in the database (db.py).

Part 4, commit 1 update: `run_agent_placeholder`'s echo has been
replaced by `run_agent`, which makes a REAL (but still tool-less,
single LLM call) reply: it loads the conversation's history from the
database (db.fetch_conversation_history), calls the configured LLM via
llm/factory.py (with automatic provider fallback), streams the reply
back as buffered delta events, and persists the final text
(db.save_assistant_message) as the system of record. What it still
does NOT do: call any tool (database lookup, RAG, web search, refund
action) or run inside a LangGraph loop - the model can only rely on
conversation history, not look anything up. That is part 4 commit 2,
once the graph and its four tools exist. Everything AROUND the agent
call (idempotency, ack/nack, retry, DLQ, event publishing) is
unchanged from part 3 and will not need to change again when the real
graph is added in commit 2 - only `run_agent`'s body will.

--- The two required answers (project brief, section 3(b)) ---

Q1: "اگر همان فرمان دو بار برسد چه می‌شود؟"
    (What happens if the same command arrives twice?)
A1: See `handle_command`, two layers:
    1. BEFORE doing any work, db.already_processed(message_id) does a
       cheap read. If a prior delivery already finished (the common
       case - e.g. redelivery after the original ack was lost on the
       wire), this is true and the handler acks immediately without
       re-running the agent or re-publishing anything.
    2. AFTER the agent runs, db.mark_processed(message_id) attempts
       the real INSERT, guarded by PostgreSQL's UNIQUE constraint on
       message_id. If two deliveries raced past check #1 at nearly the
       same moment and both ran the agent, only one INSERT wins; the
       other raises db.DuplicateMessageError, which is caught and
       treated as success (ack, no further side effects).
    Layer 1 is what makes the common case cheap; layer 2 is what makes
    the guarantee actually hold under a race. See "Known limitation"
    below for the one case even layer 2 does not fully close: events
    already published to RabbitMQ before the crash.

Q2: "اگر worker پیش از تأیید از کار بیفتد چه می‌شود؟"
    (What happens if the worker crashes before acking?)
A2: RabbitMQ only removes a message from the queue when it receives an
    explicit ack (see `channel.basic_qos(prefetch_count=1)` and manual
    `basic_ack` below - never auto-ack). If the worker process dies
    (or its TCP connection to the broker drops) after doing work but
    before sending that ack, RabbitMQ detects the dead consumer and
    redelivers the message - to this worker after it restarts, or to
    another worker instance. That redelivery is exactly the "same
    command arrives twice" case answered in Q1: the idempotency check
    makes the redelivery a safe no-op instead of a duplicate side
    effect (e.g. a second RefundRequest).

--- Known limitation: idempotency covers the database, not the events already on the wire ---

The idempotency guarantee above (Q1) is enforced by a PostgreSQL UNIQUE
constraint, which only protects work that is still inside the open
database transaction when a crash happens. Publishing an event to
RabbitMQ (`publish_event`, called from `run_agent`) is a SEPARATE
system from PostgreSQL - the two cannot be committed together as one
atomic unit without a more advanced pattern (e.g. a transactional
outbox), which is out of scope for this project's size.

Consequently, `handle_command` deliberately does the agent's work
(including publishing every event) BEFORE opening the database
transaction that records the message as processed, and that record is
written as the LAST step, immediately before ack. This ordering
shrinks - but does not eliminate - the risk window: if the worker
crashes after events are published but before the idempotency row
commits, a redelivery will run the agent again and publish the events
a second time, even though nothing is wrong from the database's point
of view. This is called out explicitly (not hidden) in the README as a
known limitation of this project's scope, together with why closing it
fully is not attempted here.
"""

import json
import os
import time
import traceback

import pika
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from db import (
    DuplicateMessageError,
    already_processed,
    fetch_conversation_history,
    mark_processed,
    save_assistant_message,
)
from envelope import Envelope, MessageType
from events import publish_event
from llm.factory import get_default_chat_model
from topology import DLQ_QUEUE, COMMAND_QUEUE, declare_topology, retry_queue_for_attempt

load_dotenv()

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "guest")

# How many characters to accumulate before publishing one
# AGENT_RESPONSE_CHUNK event, instead of one event per token (project
# brief section 6: "رویدادهای تدریجی را بافر کنید؛ به ازای هر token یک
# پیام روی صف نفرستید"). A plain character count, not a token count -
# simple, and good enough to keep the number of RabbitMQ/SSE messages
# per reply small without pulling in a tokenizer just for this.
DELTA_BUFFER_CHARS = 40

SUPPORT_SYSTEM_PROMPT = (
    "You are Maya, a customer support assistant for an online store. "
    "Answer the customer's question clearly and concisely, in the same "
    "language they wrote in, based on the conversation so far. You do "
    "not yet have access to the order database, company documents, or "
    "the web - if answering well would require looking one of those "
    "up, say so plainly instead of guessing; that capability is added "
    "in a later step of this project."
)

# Built lazily, once per worker process - constructing a LangChain chat
# model only wires up an HTTP client, it makes no network call itself,
# so there is no reason to rebuild it on every command.
_chat_model = None


def _get_chat_model():
    global _chat_model
    if _chat_model is None:
        _chat_model = get_default_chat_model()
    return _chat_model


def _history_to_messages(history: list[dict]) -> list:
    """Converts the plain dict rows from db.fetch_conversation_history
    into LangChain message objects, prefixed with the system prompt."""
    messages = [SystemMessage(content=SUPPORT_SYSTEM_PROMPT)]
    for entry in history:
        if entry["role"] == "user":
            messages.append(HumanMessage(content=entry["content"]))
        else:
            messages.append(AIMessage(content=entry["content"]))
    return messages


def _stream_reply_as_buffered_deltas(channel, model, messages, correlation_id: str) -> str:
    """
    Streams the model's reply token-by-token internally, but only
    publishes an AGENT_RESPONSE_CHUNK event once roughly
    DELTA_BUFFER_CHARS characters have accumulated (plus one final
    flush for whatever remains) - the buffering the project brief
    requires. Returns the full, unbuffered reply text for persistence.
    """
    full_text_parts = []
    buffer = ""

    for chunk in model.stream(messages):
        piece = chunk.content or ""
        if not piece:
            continue
        full_text_parts.append(piece)
        buffer += piece
        if len(buffer) >= DELTA_BUFFER_CHARS:
            publish_event(
                channel,
                MessageType.AGENT_RESPONSE_CHUNK,
                payload={"delta": buffer},
                correlation_id=correlation_id,
            )
            buffer = ""

    if buffer:
        publish_event(
            channel,
            MessageType.AGENT_RESPONSE_CHUNK,
            payload={"delta": buffer},
            correlation_id=correlation_id,
        )

    return "".join(full_text_parts)


def run_agent(channel, envelope: Envelope) -> None:
    """
    Generate and publish a reply for one PROCESS_USER_MESSAGE command.

    Steps: publish AGENT_RUN_STARTED -> load conversation history from
    the database -> call the LLM (streamed, buffered deltas) -> persist
    the final text as a Message row -> publish AGENT_RUN_COMPLETED.

    Raises to simulate a failure so the retry/backoff/DLQ path is
    exercisable: set payload["text"] to the literal string
    "__simulate_failure__" when testing that path manually.
    """
    correlation_id = envelope.correlation_id
    conversation_id = envelope.payload.get("conversation_id")
    user_text = envelope.payload.get("text", "")

    publish_event(
        channel,
        MessageType.AGENT_RUN_STARTED,
        payload={"acting_user_id": envelope.payload.get("acting_user_id")},
        correlation_id=correlation_id,
    )

    if user_text == "__simulate_failure__":
        # Deliberate failure path for demonstrating retry/backoff/DLQ
        # during the defense, without needing a real agent bug.
        raise RuntimeError("Simulated failure for retry/DLQ demonstration")

    # The user's own message was already persisted by Django
    # (support/views.py) BEFORE this command was published, so it is
    # already the last row returned here - it must not be appended a
    # second time.
    history = fetch_conversation_history(conversation_id) if conversation_id else []
    messages = _history_to_messages(history)

    model = _get_chat_model()
    reply_text = _stream_reply_as_buffered_deltas(channel, model, messages, correlation_id)

    if conversation_id:
        save_assistant_message(conversation_id, reply_text)

    publish_event(
        channel,
        MessageType.AGENT_RUN_COMPLETED,
        payload={"final_text": reply_text},
        correlation_id=correlation_id,
    )


def connect_with_retry(max_attempts: int = 10, delay_seconds: float = 3.0) -> pika.BlockingConnection:
    """
    Retry connecting to RabbitMQ with a fixed delay.

    RabbitMQ may still be starting up when this container starts
    (depends_on does not mean "ready", only "started" - see README
    for the healthcheck discussion). This retry loop covers that gap
    during local development; production would lean entirely on the
    Compose healthcheck instead.
    """
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
    parameters = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        credentials=credentials,
    )

    for attempt in range(1, max_attempts + 1):
        try:
            return pika.BlockingConnection(parameters)
        except pika.exceptions.AMQPConnectionError:
            print(f"[worker] RabbitMQ not ready (attempt {attempt}/{max_attempts}), retrying...")
            time.sleep(delay_seconds)

    raise RuntimeError("Could not connect to RabbitMQ after multiple attempts")


def _route_to_retry_or_dlq(channel, envelope: Envelope, error: Exception) -> None:
    """
    Decide where a failed command goes next: the appropriate backoff
    stage queue, or the DLQ if the retry budget is exhausted.

    We republish explicitly (rather than `basic_nack(requeue=True)`,
    which would put the message straight back at the front/back of
    agent_commands with no delay) so that the exponential backoff is
    actually enforced by the retry queues' TTL, per project brief
    3(b) ("تلاش مجدد با تأخیر نمایی").
    """
    next_envelope = envelope.with_incremented_retry()
    target_queue = retry_queue_for_attempt(envelope.retry_count)

    if target_queue is None:
        target_queue = DLQ_QUEUE
        print(
            f"[worker] Command {envelope.message_id} exhausted retries "
            f"({envelope.retry_count}); routing to DLQ. Last error: {error}"
        )
    else:
        print(
            f"[worker] Command {envelope.message_id} failed (attempt "
            f"{envelope.retry_count + 1}); routing to {target_queue}. Error: {error}"
        )

    channel.basic_publish(
        exchange="",
        routing_key=target_queue,
        body=json.dumps(next_envelope.to_dict()).encode("utf-8"),
        properties=pika.BasicProperties(
            delivery_mode=pika.DeliveryMode.Persistent,
            content_type="application/json",
            correlation_id=next_envelope.correlation_id,
            message_id=next_envelope.message_id,
            type=next_envelope.message_type.value,
        ),
    )

    if target_queue == DLQ_QUEUE:
        publish_event(
            channel,
            MessageType.AGENT_RUN_FAILED,
            payload={"reason": str(error), "retry_count": envelope.retry_count},
            correlation_id=envelope.correlation_id,
        )


def handle_command(channel, method, properties, body):
    """
    Main command handler: parse envelope -> early idempotency check ->
    run the agent -> record as processed -> ack. Any failure during
    agent execution routes the message to retry/DLQ instead of acking,
    per project brief 3(b).
    """
    try:
        envelope = Envelope.from_dict(json.loads(body))
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        # Malformed envelope: cannot be retried meaningfully (it will
        # fail to parse again identically), so it goes straight to the
        # DLQ for manual inspection rather than looping forever.
        print(f"[worker] Malformed message, sending straight to DLQ: {exc}")
        channel.basic_publish(exchange="", routing_key=DLQ_QUEUE, body=body)
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    print(
        f"[worker] Received {envelope.message_type.value} "
        f"(message_id={envelope.message_id}, correlation_id={envelope.correlation_id}, "
        f"retry_count={envelope.retry_count})"
    )

    # Early idempotency check (project brief 3(b), Q1): if this exact
    # message_id was already recorded as processed by an earlier
    # delivery, skip running the agent again entirely - this is the
    # common case for a redelivery that happens well after the first
    # delivery already finished successfully.
    if already_processed(envelope.message_id):
        print(f"[worker] {envelope.message_id} already processed, skipping re-run.")
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    try:
        # Run the agent's actual work (including publishing every
        # event) FIRST, and only record "this message_id is done" as
        # the LAST step, right before ack. See this file's module
        # docstring ("Known limitation") for why this order - agent
        # work, then the idempotency write - is the deliberate choice:
        # RabbitMQ and PostgreSQL are two separate systems that cannot
        # be committed together, so the idempotency row can only ever
        # protect what happens AFTER it is written. Writing it last
        # minimizes that gap; it does not eliminate it.
        run_agent(channel, envelope)

        mark_processed(
            message_id=envelope.message_id,
            correlation_id=envelope.correlation_id,
            message_type=envelope.message_type.value,
        )

    except DuplicateMessageError:
        # Closes the race the early check above cannot: two deliveries
        # that both passed already_processed() at nearly the same
        # moment and both ran the agent concurrently. Only one of them
        # wins the final INSERT; the other lands here. We still ack -
        # from RabbitMQ's point of view this delivery is done, even
        # though (in this narrow race) its events were also published.
        # See "Known limitation" in the module docstring.
        print(f"[worker] Duplicate delivery of {envelope.message_id}, skipping re-processing.")
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    except Exception as exc:  # noqa: BLE001 - any agent/tool failure routes to retry/DLQ
        traceback.print_exc()
        _route_to_retry_or_dlq(channel, envelope, exc)
        # We still ack the ORIGINAL delivery here: we have already
        # published a new copy of the command onto the retry/DLQ
        # queue, so acking this one just removes the original from
        # agent_commands. If we nacked+requeued instead, we'd get two
        # copies in flight (the requeued original AND the republished
        # retry) - so republish-then-ack is the correct pairing.
        channel.basic_ack(delivery_tag=method.delivery_tag)
        return

    # Success: only now do we tell RabbitMQ this message is done. If
    # the process crashed at any point above this line, RabbitMQ never
    # received this ack and will redeliver - see Q2 above.
    channel.basic_ack(delivery_tag=method.delivery_tag)


def main():
    connection = connect_with_retry()
    channel = connection.channel()

    declare_topology(channel)

    # Fair dispatch: don't hand a new message to this worker until the
    # previous one has been acknowledged. Combined with manual ack,
    # this bounds how much work is "in flight and unacknowledged" at
    # once to exactly one message per worker process.
    channel.basic_qos(prefetch_count=1)

    channel.basic_consume(queue=COMMAND_QUEUE, on_message_callback=handle_command)

    print(f"[worker] Waiting for messages on queue '{COMMAND_QUEUE}'. To exit press CTRL+C")
    channel.start_consuming()


if __name__ == "__main__":
    main()
