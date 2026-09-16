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

Part 4 commit 2 update: `run_agent` no longer makes a single tool-less
LLM call. It now builds and runs the real LangGraph agent
(graph/agent_graph.py), bound to the four tools the project brief
requires (database, document retrieval, web search, one action tool),
and streams BOTH the model's text deltas and real tool-call events as
they happen. What changed, concretely, versus commit 1:

  - `run_agent` now calls `graph/agent_graph.py::build_agent_graph`
    (scoped to this command's acting_user_id) instead of calling
    `model.stream(...)` directly on a fixed message list.
  - The agent may now call tools mid-run; every tool call publishes a
    REAL `AGENT_TOOL_CALLED` event (name + arguments + result), not a
    placeholder.
  - The final assistant message persisted to the database
    (db.save_assistant_message_with_tool_calls) now also carries the
    tool-call trace, for a future "agent activity" panel to render.

Everything AROUND the agent call - idempotency, ack/nack, retry, DLQ,
the outer event publishing shape (AGENT_RUN_STARTED /
AGENT_RESPONSE_CHUNK / AGENT_RUN_COMPLETED / AGENT_RUN_FAILED) - is
UNCHANGED from commits 1 and part 3, exactly as commit 1's own
docstring predicted: "Everything AROUND the agent call ... will not
need to change again when the real graph is added in commit 2 - only
run_agent's body will."

Part 7 update (رصدپذیری و هزینه): `run_agent` is now a thin wrapper
that owns this run's OBSERVABILITY, and the work it used to do lives in
`_execute_agent_run` unchanged. The wrapper builds two observers - the
local `RunTracer` and, when keys are configured, a Langfuse handler -
hands both to the graph as callbacks (so every model call and every
tool call is observed from INSIDE LangChain, not guessed from the
event stream), and then, in a `finally`, writes one ledger row per run
plus one row per span to PostgreSQL and publishes a new
AGENT_RUN_USAGE event carrying the run's real token counts, Tavily
credits and reference cost. The `finally` matters: a failed or
timed-out run spent real tokens too, and only counting successful runs
would report a number below the real bill.

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
    already published to RabbitMQ before the crash - and, as of commit
    2, any tool side-effect (e.g. a RefundRequest row) executed before
    the crash. Tool side-effects carry the same caveat as published
    events: see tools/action_tools.py's own docstring for the accepted
    limitation this leaves for create_refund_request specifically.

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
RabbitMQ (`publish_event`) is a SEPARATE system from PostgreSQL - the
two cannot be committed together as one atomic unit without a more
advanced pattern (e.g. a transactional outbox), which is out of scope
for this project's size.

Consequently, `handle_command` deliberately does the agent's work
(including publishing every event AND running every tool, as of commit
2) BEFORE opening the database transaction that records the message as
processed, and that record is written as the LAST step, immediately
before ack. This ordering shrinks - but does not eliminate - the risk
window: if the worker crashes after events/tool-effects happen but
before the idempotency row commits, a redelivery will run the agent
again (including its tools) and publish the events a second time, even
though nothing is wrong from the database's point of view. This is
called out explicitly (not hidden) in the README as a known limitation
of this project's scope, together with why closing it fully is not
attempted here.
"""

import json
import os
import queue
import threading
import time
import traceback

import pika
from dotenv import load_dotenv

from db import (
    DuplicateMessageError,
    already_processed,
    fetch_conversation_history,
    fetch_order_for_user,
    insert_agent_run,
    mark_processed,
    save_assistant_message_with_tool_calls,
)
from envelope import Envelope, MessageType
from events import publish_event
from graph.agent_graph import AGENT_RECURSION_LIMIT, build_agent_graph, build_initial_messages
from llm.factory import get_default_chat_models
from observability import langfuse_client
from observability.tracing import RunTracer
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

# Hard wall-clock ceiling on a single agent run (LLM calls + every tool
# call combined), independent of whatever timeout llm/factory.py asked
# the provider's SDK for. This exists because that per-provider timeout
# turned out NOT to be a sufficient backstop on its own: with a
# network path that silently black-holes packets (observed in
# practice with LLM_PRIMARY_PROVIDER=google_genai against a blocked/
# unreachable endpoint), the underlying google-generativeai client
# hung well past its configured `timeout=30`, because that timeout is
# not honored by every code path in that SDK's transport layer. A
# provider/network stall that langchain's own timeout does not catch
# would otherwise block this line of run_agent forever - and because
# the worker consumes with `basic_qos(prefetch_count=1)` and never
# acks/nacks while stuck, that is a silent full outage: no further
# command is ever picked up, not just this one. Running the graph on a
# background thread and enforcing this deadline from the main thread
# (which alone owns the pika `channel` - pika objects are not
# thread-safe, so the background thread only ever pushes plain data
# into `queue.Queue`, never touches `channel`) guarantees run_agent
# returns within this budget no matter what the provider SDK does,
# feeding the existing retry/backoff/DLQ + AGENT_RUN_FAILED path
# (project brief 3(b)) instead of hanging.
#
# Set to match the frontend's own AWAITING_RESPONSE_TIMEOUT_MS
# (useConversationStream.ts) - the point the UI itself already treats
# a turn as "taking too long", so the backend's hard ceiling is not
# looser than the promise the UI is already making to the user.
AGENT_RUN_TIMEOUT_SECONDS = 45

class AgentRunTimeoutError(Exception):
    """Raised when one agent run exceeds AGENT_RUN_TIMEOUT_SECONDS."""


def _stream_graph_in_background(graph, messages, graph_config, out_queue: queue.Queue) -> None:
    """
    Runs on a background thread: drives `graph.stream(...)` (which may
    block for a long time inside a provider HTTP/gRPC call or a tool's
    own I/O) and forwards every item to `out_queue` for the main thread
    to consume. Never touches the pika `channel` - see
    AGENT_RUN_TIMEOUT_SECONDS's docstring for why that matters.
    """
    try:
        for item in graph.stream(
            {"messages": messages},
            config=graph_config,
            stream_mode=["messages", "updates"],
        ):
            out_queue.put(("item", item))
    except Exception as exc:  # noqa: BLE001 - forwarded to the main thread, not swallowed
        print(f"[bg-thread] ERROR: {type(exc).__name__}: {exc}", flush=True)
        out_queue.put(("error", exc))
    finally:
        out_queue.put(("done", None))


# Built lazily, once per worker process - constructing a LangChain chat
# model only wires up an HTTP client, it makes no network call itself,
# so there is no reason to rebuild it on every command. The GRAPH,
# unlike the model, is rebuilt per command (see build_agent_graph's
# docstring) because it is scoped to one acting_user_id.
_chat_models = None


def _get_chat_models():
    """
    The configured models, primary first - one element normally, two
    when LLM_FALLBACK_PROVIDER is set. Returned UNWIRED: the fallback
    chain is built per run, after tools are bound, by
    llm/factory.py::bind_tools_with_fallback (see its docstring for
    why that order is the only correct one).
    """
    global _chat_models
    if _chat_models is None:
        _chat_models = get_default_chat_models()
    return _chat_models


def _publish_buffered_delta(channel, buffer: str, correlation_id: str) -> None:
    if buffer:
        publish_event(
            channel,
            MessageType.AGENT_RESPONSE_CHUNK,
            payload={"delta": buffer},
            correlation_id=correlation_id,
        )


def run_agent(channel, envelope: Envelope) -> None:
    """
    One agent run, wrapped in its execution trace and cost accounting
    (project brief, section 7: "رصدپذیری و هزینه").

    This function owns three things `_execute_agent_run` deliberately
    does not:

      1. AGENT_RUN_STARTED, published here rather than inside the body
         so the browser sees "the agent started" even if building the
         trace or the graph itself fails immediately.
      2. The observers: a `RunTracer` (always) and a Langfuse handler
         (only when LANGFUSE_* keys are configured). Both are handed to
         the graph as callbacks, so they observe every model call and
         every tool call from inside LangChain rather than from our
         loop - see observability/tracing.py.
      3. The ledger write, in a `finally`. This is the important part:
         a run that called the model three times and then hit the
         wall-clock ceiling spent exactly as many tokens as a
         successful one, and a cost report that only counted successes
         would read lower than the real bill. The brief is explicit
         that "«رایگان بود»" is not an acceptable answer, and neither
         is "we only count the runs that worked".

    The exception, if any, is re-raised unchanged after the ledger is
    written, so `handle_command`'s existing retry/backoff/DLQ path
    (project brief 3(b)) behaves exactly as it did before part 7.
    """
    correlation_id = envelope.correlation_id
    conversation_id = envelope.payload.get("conversation_id")
    acting_user_id = envelope.payload.get("acting_user_id")

    publish_event(
        channel,
        MessageType.AGENT_RUN_STARTED,
        payload={"acting_user_id": acting_user_id},
        correlation_id=correlation_id,
    )

    tracer = RunTracer(
        correlation_id=correlation_id,
        conversation_id=conversation_id,
        command_message_id=envelope.message_id,
        # The envelope's retry_count at the moment this attempt began:
        # a retried command runs the agent a second time and spends a
        # second time, so each attempt gets its own ledger row (see
        # backend/observability/models.py on the uniqueness constraint).
        attempt=envelope.retry_count,
        acting_user_id=acting_user_id,
    )
    langfuse_handler = langfuse_client.build_handler(
        correlation_id=correlation_id,
        conversation_id=conversation_id,
        command_message_id=envelope.message_id,
        attempt=envelope.retry_count,
        acting_user_id=acting_user_id,
    )
    callbacks = [tracer] + ([langfuse_handler] if langfuse_handler is not None else [])

    status = "completed"
    error_message = ""
    try:
        _execute_agent_run(channel, envelope, callbacks)
    except BaseException as exc:  # noqa: BLE001 - re-raised below, never swallowed
        status = "failed"
        error_message = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _record_run(channel, tracer, langfuse_handler, status, error_message)


def _record_run(channel, tracer: RunTracer, langfuse_handler, status: str, error_message: str) -> None:
    """
    Close out one run's observability: flush Langfuse, write the
    PostgreSQL ledger row (+ one row per model/tool span), and publish
    AGENT_RUN_USAGE so the UI's cost display updates live.

    Every step is best-effort and independently guarded. This runs in
    a `finally` - frequently while an exception is already on its way
    to the retry/DLQ path - and a failure to RECORD what happened must
    never change WHAT happened. The run's real outcome has already
    been decided by the time we get here.
    """
    try:
        langfuse_client.flush(langfuse_handler)
        snapshot = tracer.snapshot(
            status=status,
            error_message=error_message,
            trace_url=langfuse_client.trace_url(langfuse_handler),
        )
        snapshot.log_summary()
        insert_agent_run(snapshot)

        publish_event(
            channel,
            MessageType.AGENT_RUN_USAGE,
            payload=snapshot.to_event_payload(),
            correlation_id=snapshot.correlation_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[worker] Failed to record run observability (ignored): {exc}", flush=True)


def _execute_agent_run(channel, envelope: Envelope, callbacks: list) -> None:
    """
    The actual work of one PROCESS_USER_MESSAGE command: load history,
    build the graph, stream it, publish events, persist the reply.

    Steps: load conversation history from the database -> build this
    run's agent graph (tools bound to this command's acting_user_id)
    -> stream the graph, publishing buffered text deltas AND real
    tool-call events as they occur -> persist the final text (with its
    tool-call trace) as a Message row -> publish AGENT_RUN_COMPLETED.

    `callbacks` are LangChain callback handlers (part 7: RunTracer,
    plus the Langfuse handler when configured) that `run_agent` built
    and will read back afterwards. They are passed INTO the graph
    config rather than wrapped around this call because that is the
    only way to observe what happens INSIDE the graph - per model call
    and per tool call - rather than just its total duration. See
    observability/tracing.py's docstring.

    Raises to simulate a failure so the retry/backoff/DLQ path is
    exercisable: set payload["text"] to the literal string
    "__simulate_failure__" when testing that path manually.
    """
    correlation_id = envelope.correlation_id
    conversation_id = envelope.payload.get("conversation_id")
    acting_user_id = envelope.payload.get("acting_user_id")
    order_id = envelope.payload.get("order_id")
    user_text = envelope.payload.get("text", "")

    if user_text == "__simulate_failure__":
        # Deliberate failure path for demonstrating retry/backoff/DLQ
        # during the defense, without needing a real agent bug.
        raise RuntimeError("Simulated failure for retry/DLQ demonstration")

    # The user's own message was already persisted by Django
    # (support/views.py) BEFORE this command was published, so it is
    # already the last row returned here if the read-after-write is
    # visible; build_initial_messages tolerates either case (see its
    # own docstring) so this history is never duplicated in the prompt.
    history = fetch_conversation_history(conversation_id) if conversation_id else []
    order_context = None
    if order_id and acting_user_id:
        order_context = fetch_order_for_user(acting_user_id, order_id)
    messages = build_initial_messages(history, user_text, order_context=order_context)

    models = _get_chat_models()
    graph = build_agent_graph(models, acting_user_id)

    # A fresh thread_id per command: this graph's checkpointer
    # (MemorySaver, see agent_graph.py's docstring) only needs to
    # track state for THIS invocation, so there is no reason to reuse
    # thread ids across commands or conversations.
    graph_config = {
        "configurable": {"thread_id": envelope.message_id},
        "recursion_limit": AGENT_RECURSION_LIMIT,
        # Part 7: every model call and every tool call inside this
        # graph run is observed through these handlers - the local
        # RunTracer (always) and Langfuse (when configured). Attaching
        # them here, on the run config, rather than to the model or the
        # tools individually, is what guarantees nothing inside the
        # graph can execute untraced.
        "callbacks": callbacks,
    }

    reply_text_parts: list[str] = []
    delta_buffer = ""
    tool_calls_trace: list[dict] = []
    # Tracks which AIMessage tool_call ids we've already announced via
    # AGENT_TOOL_CALLED (with args, before the result exists) so each
    # call is announced exactly once.
    announced_tool_call_ids: set[str] = set()
    # Tracks which ToolMessages (by tool_call_id) we've already folded
    # into tool_calls_trace, for the same "seen once" reason.
    recorded_tool_result_ids: set[str] = set()

    # stream_mode=["messages", "updates"] gives us BOTH true per-token
    # deltas from inside agent_node's chat-model call ("messages": a
    # (chunk, metadata) pair per token, the real "پخش زنده" the brief
    # asks for in section 6) AND node-level completion events
    # ("updates": the full state update once a node finishes, which is
    # where a finished AIMessage's tool_calls or a ToolMessage's result
    # become visible). Using token-level streaming here - rather than
    # commit 1's coarser "wait for the whole message" approach - is
    # what makes AGENT_RESPONSE_CHUNK events genuine incremental
    # deltas instead of one big flush at the end.
    event_queue: queue.Queue = queue.Queue()
    background_thread = threading.Thread(
        target=_stream_graph_in_background,
        args=(graph, messages, graph_config, event_queue),
        daemon=True,
        name=f"agent-run-{envelope.message_id}",
    )
    background_thread.start()

    deadline = time.monotonic() + AGENT_RUN_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # The background thread is left running (daemon=True) since
            # Python cannot forcibly cancel a blocked network call; we
            # simply stop waiting on it so this command can be routed
            # to retry/DLQ and the worker can move on to the next
            # message instead of hanging forever. See
            # AGENT_RUN_TIMEOUT_SECONDS's docstring above.
            raise AgentRunTimeoutError(
                f"Agent run exceeded {AGENT_RUN_TIMEOUT_SECONDS}s wall-clock budget "
                "(a provider or tool call likely hung without honoring its own timeout)."
            )

        try:
            kind, value = event_queue.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue

        if kind == "error":
            raise value
        if kind == "done":
            break

        stream_kind, chunk = value

        if stream_kind == "messages":
            message_chunk, _metadata = chunk
            # Only agent_node's own token stream has real text content
            # to relay; ToolMessage chunks (tool node) carry step
            # results, not narration, and are handled via "updates"
            # below instead.
            if getattr(message_chunk, "type", None) in ("ai", "AIMessageChunk"):
                piece = message_chunk.content or ""
                if piece:
                    reply_text_parts.append(piece)
                    delta_buffer += piece
                    if len(delta_buffer) >= DELTA_BUFFER_CHARS:
                        _publish_buffered_delta(channel, delta_buffer, correlation_id)
                        delta_buffer = ""
            continue

        # stream_kind == "updates": one dict per node that just
        # finished, e.g. {"agent": {"messages": [AIMessage(...)]}} or
        # {"tools": {"messages": [ToolMessage(...), ...]}}.
        for node_output in chunk.values():
            for message in node_output.get("messages", []):
                tool_calls = getattr(message, "tool_calls", None) or []
                for call in tool_calls:
                    call_id = call.get("id")
                    if not call_id or call_id in announced_tool_call_ids:
                        continue
                    announced_tool_call_ids.add(call_id)
                    publish_event(
                        channel,
                        MessageType.AGENT_TOOL_CALLED,
                        payload={
                            "tool_call_id": call_id,
                            "tool_name": call.get("name"),
                            "tool_args": call.get("args"),
                            "phase": "started",
                        },
                        correlation_id=correlation_id,
                    )

                tool_call_id = getattr(message, "tool_call_id", None)
                if tool_call_id and tool_call_id not in recorded_tool_result_ids:
                    recorded_tool_result_ids.add(tool_call_id)
                    result_content = message.content
                    publish_event(
                        channel,
                        MessageType.AGENT_TOOL_CALLED,
                        payload={
                            "tool_call_id": tool_call_id,
                            "tool_name": getattr(message, "name", None),
                            "result": result_content,
                            "phase": "completed",
                        },
                        correlation_id=correlation_id,
                    )
                    tool_calls_trace.append(
                        {
                            "tool_call_id": tool_call_id,
                            "tool_name": getattr(message, "name", None),
                            "result": result_content,
                        }
                    )

    _publish_buffered_delta(channel, delta_buffer, correlation_id)
    reply_text = "".join(reply_text_parts)
    if conversation_id:
        save_assistant_message_with_tool_calls(
            conversation_id,
            reply_text,
            json.dumps(tool_calls_trace, ensure_ascii=False) if tool_calls_trace else None,
        )

    publish_event(
        channel,
        MessageType.AGENT_RUN_COMPLETED,
        payload={"final_text": reply_text, "tool_calls": tool_calls_trace},
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
        # event and executing every tool call) FIRST, and only record
        # "this message_id is done" as the LAST step, right before
        # ack. See this file's module docstring ("Known limitation")
        # for why this order - agent work, then the idempotency write
        # - is the deliberate choice: RabbitMQ and PostgreSQL are two
        # separate systems that cannot be committed together, so the
        # idempotency row can only ever protect what happens AFTER it
        # is written. Writing it last minimizes that gap; it does not
        # eliminate it.
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
