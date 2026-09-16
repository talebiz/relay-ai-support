"""
Optional Langfuse tracing, keyed by correlation_id.

The project brief names Langfuse twice: once in section 3 ("worker باید
کل مسیر را به هم وصل کند: درخواست، فرمان، اجرای worker، رد Langfuse و
SSE" - the worker must connect the whole path: request, command, worker
execution, Langfuse trace, and SSE) and once in section 4's comparison
table ("یک لاگ + Langfuse + پایگاه داده"). So Langfuse is the timeline
view of a run; it is not the ledger. The ledger is PostgreSQL (see
backend/observability/models.py for why that split exists).

Three properties this module is built around:

1. OPTIONAL. If LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are unset - or
   the package is not installed, or the host is unreachable - every
   function here degrades to "no Langfuse", and the run proceeds
   normally with its local trace and its database ledger intact. A
   defense on a laptop with no internet must still satisfy section 7.

2. CONNECTED BY correlation_id. Each run's trace carries:
       session_id = correlation_id   (so every turn of ONE conversation
                                      groups into one Langfuse session)
       metadata   = correlation_id, conversation_id, command message_id,
                    retry attempt
       tags       = the correlation_id itself, for direct search
   This is the literal "همه متصل با correlation_id" requirement: the
   same id is in the RabbitMQ envelope, the SSE frames, the
   `observability_agentrun` row, the worker's stdout log lines, and the
   Langfuse trace.

3. NON-BLOCKING ON FAILURE. Langfuse's SDK buffers and flushes in a
   background thread. `flush_trace` is best-effort and swallows
   everything: an observability backend having a bad day must never
   turn a working reply into a failed command.
"""

from __future__ import annotations

import os


def is_enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def build_handler(
    *,
    correlation_id: str,
    conversation_id: str | None,
    command_message_id: str,
    attempt: int,
    acting_user_id: int | None,
):
    """
    Build a Langfuse LangChain callback handler for one run, or return
    None if Langfuse is not configured/available.

    The returned object is appended to the same `callbacks` list as
    RunTracer in worker.py, so both observe the identical execution -
    there is no second code path that could drift from the first.
    """
    if not is_enabled():
        return None

    try:
        from langfuse.callback import CallbackHandler
    except Exception as exc:  # noqa: BLE001 - missing/incompatible package is not fatal
        print(f"[langfuse] Disabled (import failed): {exc}", flush=True)
        return None

    try:
        return CallbackHandler(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            # One Langfuse SESSION per conversation, one TRACE per turn:
            # correlation_id is stable for the whole conversation (see
            # conversations/models.py), so this is what makes a
            # multi-turn conversation readable as one thing in Langfuse.
            session_id=correlation_id,
            user_id=str(acting_user_id) if acting_user_id is not None else None,
            trace_name="relay.agent_run",
            tags=["relay", "agent_run", f"correlation:{correlation_id}"],
            metadata={
                "correlation_id": correlation_id,
                "conversation_id": conversation_id,
                "command_message_id": command_message_id,
                "attempt": attempt,
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[langfuse] Disabled (handler construction failed): {exc}", flush=True)
        return None


def trace_url(handler) -> str:
    """
    URL of the trace this handler just produced, stored on the
    AgentRun row so the UI can deep-link from "this turn cost X" to
    the full timeline. Empty string when unavailable - the UI simply
    omits the link rather than rendering a dead one.
    """
    if handler is None:
        return ""
    try:
        return handler.get_trace_url() or ""
    except Exception:  # noqa: BLE001
        return ""


def flush(handler) -> None:
    """
    Push this run's buffered spans to Langfuse now.

    Worth doing explicitly per run rather than relying on the SDK's
    periodic flush: this worker is a long-lived process that may sit
    idle for minutes between commands, and an un-flushed trace is one
    that simply does not exist if the container is restarted in the
    meantime.
    """
    if handler is None:
        return
    try:
        handler.flush()
    except Exception as exc:  # noqa: BLE001
        print(f"[langfuse] Flush failed (ignored): {exc}", flush=True)
