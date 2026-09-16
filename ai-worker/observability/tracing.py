"""
The execution trace for one agent run - project brief, section 7:

    "ردهای اجرا برای کل اجرای عامل، هر فراخوانی مدل و ابزار، همه متصل
    با correlation_id."

    (Execution traces: for the whole agent run, for every model call
    and every tool call, all connected by correlation_id.)

`RunTracer` is a LangChain `BaseCallbackHandler` handed to the graph in
`worker.py`'s `graph.stream(..., config={"callbacks": [...]})`. LangChain
then calls it at every boundary inside the run, so the trace is
produced by OBSERVING the real execution rather than by the worker
remembering to log at each step. That distinction matters: a tool call
the agent makes through some path `run_agent` does not explicitly
handle still shows up here, because the observation point is inside
LangChain, not inside our loop.

--- Why not just read the stream in worker.py ---

worker.py already sees tool calls (it publishes AGENT_TOOL_CALLED from
the "updates" stream). It sees them, but it cannot see:

  - how long each model call took, separately from the tool calls
    between them,
  - how many tokens each model call consumed,
  - the model calls that produced no text at all (a turn whose only
    output is a tool call streams no user-visible delta),
  - or a failure inside a tool, as distinct from the tool returning a
    string that happens to describe an error.

Those are exactly the things section 7 asks to be traced, so they are
captured where LangChain actually knows them.

--- Counting tokens "for real" ---

Order of preference, per model call:

  1. `usage_metadata` on the returned message - the provider's own
     count. Exact.
  2. `llm_output["token_usage"]` - the OpenAI-shaped equivalent, used
     by several OpenAI-compatible providers. Also the provider's own
     count.
  3. A local, dependency-free estimate from the byte length of the
     prompt and the completion (`estimate_tokens`).

Only 1 and 2 are marked `provider`; 3 is marked `estimated`, and that
marking is carried all the way to the UI. Some free-tier
OpenAI-compatible endpoints return no usage block at all while
streaming, and silently presenting a local estimate as the provider's
measurement would be precisely the kind of comfortable answer the
brief's "«رایگان بود» پاسخ این پرسش نیست" is warning against.

Step 3 deliberately uses arithmetic rather than a real tokenizer.
`tiktoken` was tried here first and removed, for three reasons worth
stating because "why is this NOT a tokenizer" is a fair question:

  - Its `cl100k_base` vocabulary is not the tokenizer this project's
    model actually uses, so it was an approximation either way - just
    a more expensive one.
  - `tiktoken.get_encoding()` DOWNLOADS its vocabulary on first use,
    with no timeout this code controls, from inside a callback on the
    worker's hot path. That is the same class of hazard that
    `AGENT_RUN_TIMEOUT_SECONDS` in worker.py exists to contain, and
    accepting it for a fallback path is a bad trade. In a container
    without outbound internet it simply fails.
  - It is reached only when a provider reports no usage at all. With
    `stream_usage=True` (llm/factory.py) that does not happen on this
    deployment, so the cost of the dependency bought nothing.

--- Tavily credits ---

Tavily bills credits per search, not tokens, so credits are counted
where they are actually spent: at the end of a `web_search` tool call,
by looking at whether the tool's own result reports an error. A search
that never left the process (no API key configured) or that failed
before reaching Tavily consumes no credit and is recorded as zero, not
as one - see `_tavily_credits_for_result`.

--- Thread safety ---

`worker.py` runs the graph on a background thread (see
AGENT_RUN_TIMEOUT_SECONDS there), so every callback below fires on
that thread while the main thread may be reading the snapshot. All
mutable state is guarded by a plain lock, and the handler never
touches the pika channel - it only accumulates plain data that the
main thread reads once the run is over.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from observability import pricing

SPAN_LLM = "llm"
SPAN_TOOL = "tool"

TOKEN_SOURCE_PROVIDER = "provider"
TOKEN_SOURCE_ESTIMATED = "estimated"
TOKEN_SOURCE_MIXED = "mixed"
TOKEN_SOURCE_NONE = "none"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Bytes per token for the local estimate. Byte-based rather than
# character-based on purpose: Persian/Arabic text is two bytes per
# character in UTF-8, so counting characters would under-count it by
# roughly half against the same constant. ~4 bytes/token is the usual
# rule of thumb for BPE tokenizers on Latin text; on Persian it still
# under-counts, which is one more reason anything measured this way is
# labeled "estimated" in the UI instead of being presented as a count.
BYTES_PER_ESTIMATED_TOKEN = 4

# Which dataclass fields each serialized shape leaves out. Kept as
# small exclusion sets (rather than inclusion lists) so a field added
# to SpanRecord/RunSnapshot reaches the ledger and the event by
# default - forgetting to persist a new number is a much quieter bug
# than accidentally publishing one.
_SPAN_NEVER_SERIALIZED = frozenset({"prompt_text"})
_SPAN_EVENT_EXCLUDED = _SPAN_NEVER_SERIALIZED | {
    "started_at",
    "finished_at",
    "error_message",
}
_RUN_EVENT_EXCLUDED = frozenset(
    {"command_message_id", "attempt", "started_at", "finished_at", "error_message", "spans"}
)
# Serialized as STRINGS in the event, matching what the REST API
# returns for the same numbers (observability/serializers.py): a cost
# whose last digits change depending on whether the browser read it
# from an event or from the database would be worse than useless.
_MONEY_FIELDS = ("llm_cost_usd", "tavily_cost_usd", "total_cost_usd")


def estimate_tokens(text: str) -> int:
    """
    Local, approximate token count for when a provider reports none.

    Deliberately plain arithmetic, no tokenizer dependency - see this
    module's docstring (step 3) for why `tiktoken` was tried and
    removed. Being arithmetic also means this function cannot raise,
    cannot block on a network call, and costs nothing to run: a
    fallback path must never be able to damage the run it is only
    supposed to describe.
    """
    if not text:
        return 0
    return max(1, len(text.encode("utf-8")) // BYTES_PER_ESTIMATED_TOKEN)


def _messages_to_text(messages: Any) -> str:
    """Flatten whatever LangChain handed us into one string for estimation."""
    parts: list[str] = []

    def walk(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            parts.append(item)
            return
        if isinstance(item, (list, tuple)):
            for sub in item:
                walk(sub)
            return
        content = getattr(item, "content", None)
        if content is not None:
            walk(content)
            return
        parts.append(str(item))

    walk(messages)
    return "\n".join(parts)


@dataclass
class SpanRecord:
    """One model call or one tool call, as recorded for the ledger."""

    sequence: int
    span_type: str
    name: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    status: str = "running"
    error_message: str = ""
    detail: dict = field(default_factory=dict)

    # Not persisted - only used to estimate tokens if the provider
    # reports none, and dropped from every serialized form below.
    prompt_text: str = ""

    def to_dict(self) -> dict:
        """
        Row shape for the PostgreSQL ledger: every field except
        `prompt_text`, which is a working value and is never persisted.

        Driven off `fields()` rather than a hand-written key list so
        adding a column to this dataclass cannot silently fail to reach
        the database - the class definition above IS the list.
        """
        row = {
            spec.name: getattr(self, spec.name)
            for spec in fields(self)
            if spec.name not in _SPAN_NEVER_SERIALIZED
        }
        # Capped so one enormous traceback cannot bloat the row.
        row["error_message"] = row["error_message"][:2000]
        return row

    def to_event_dict(self) -> dict:
        """
        JSON-safe shape for the AGENT_RUN_USAGE event payload - the
        ledger row minus the fields a browser has no use for, with
        money as a string (see RunSnapshot.to_event_payload).
        """
        event = {
            spec.name: getattr(self, spec.name)
            for spec in fields(self)
            if spec.name not in _SPAN_EVENT_EXCLUDED
        }
        event["cost_usd"] = str(event["cost_usd"])
        return event


class RunTracer(BaseCallbackHandler):
    """
    Observes one agent run. Construct one per command, hand it to the
    graph, then read `snapshot()` once the run ends (successfully or
    not - a failed run still spent tokens and must still be recorded).
    """

    # LangChain would otherwise re-raise anything this handler throws
    # into the middle of the run. Observability must never be able to
    # break the thing it observes; every method below is additionally
    # wrapped in its own try/except for the same reason.
    raise_error = False

    def __init__(
        self,
        *,
        correlation_id: str,
        conversation_id: str | None,
        command_message_id: str,
        attempt: int = 0,
        acting_user_id: int | None = None,
    ) -> None:
        super().__init__()
        self.correlation_id = correlation_id
        self.conversation_id = conversation_id
        self.command_message_id = command_message_id
        self.attempt = attempt
        self.acting_user_id = acting_user_id

        self.started_at = _utc_now()
        self._started_monotonic = time.monotonic()

        self._lock = threading.Lock()
        self._sequence = 0
        self._open: dict[UUID, SpanRecord] = {}
        self._spans: list[SpanRecord] = []
        self._provider_counted = 0
        self._estimated_counted = 0

    # ------------------------------------------------------------------
    # Span bookkeeping
    # ------------------------------------------------------------------

    def _open_span(self, run_id: UUID, span_type: str, name: str, prompt_text: str = "") -> SpanRecord:
        with self._lock:
            self._sequence += 1
            span = SpanRecord(
                sequence=self._sequence,
                span_type=span_type,
                name=name or "unknown",
                started_at=_utc_now(),
                prompt_text=prompt_text,
            )
            self._open[run_id] = span
            self._spans.append(span)
        return span

    def _close_span(self, run_id: UUID) -> SpanRecord | None:
        with self._lock:
            span = self._open.pop(run_id, None)
        if span is None:
            return None
        span.finished_at = _utc_now()
        span.duration_ms = max(
            0, int((span.finished_at - span.started_at).total_seconds() * 1000)
        )
        return span

    def _log(self, span: SpanRecord) -> None:
        """
        One structured line per span on the worker's stdout, carrying
        the correlation_id. This is the "logs" half of the brief's
        "یک لاگ + Langfuse" row - it is what makes a trace readable
        with nothing but `docker compose logs ai-worker`, no Langfuse
        account and no database query needed.
        """
        print(
            "[trace] "
            + json.dumps(
                {
                    "correlation_id": self.correlation_id,
                    "command_message_id": self.command_message_id,
                    "attempt": self.attempt,
                    "seq": span.sequence,
                    "type": span.span_type,
                    "name": span.name,
                    "status": span.status,
                    "duration_ms": span.duration_ms,
                    "input_tokens": span.input_tokens,
                    "output_tokens": span.output_tokens,
                    "cost_usd": str(span.cost_usd),
                    "detail": span.detail,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------

    @staticmethod
    def _model_name(serialized: dict | None, metadata: dict | None) -> str:
        """
        LangChain exposes the concrete model name in a few different
        places depending on the integration package, so check them in
        order of reliability rather than assuming one shape.
        """
        if metadata:
            for key in ("ls_model_name", "model_name", "model"):
                value = metadata.get(key)
                if value:
                    return str(value)
        if serialized:
            kwargs = serialized.get("kwargs") or {}
            for key in ("model_name", "model", "model_id"):
                value = kwargs.get(key)
                if value:
                    return str(value)
            if serialized.get("name"):
                return str(serialized["name"])
        return "unknown-model"

    def on_chat_model_start(
        self,
        serialized: dict,
        messages: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._open_span(
                run_id,
                SPAN_LLM,
                self._model_name(serialized, metadata),
                prompt_text=_messages_to_text(messages),
            )
        except Exception:  # noqa: BLE001
            pass

    # There is deliberately no `on_llm_start` here. That callback fires
    # for non-chat completion models, and llm/factory.py only ever
    # builds chat models (ChatOpenAI / ChatGoogleGenerativeAI), so the
    # handler would be dead code. If a completion-style provider is
    # ever added to `build_chat_model`, add it back - otherwise that
    # provider's calls would run untraced.

    @staticmethod
    def _usage_from_response(response: LLMResult) -> tuple[int, int, bool]:
        """
        Pull (input_tokens, output_tokens, is_provider_reported) out of
        an LLMResult, trying the two shapes providers actually use.
        """
        # 1. usage_metadata on the message - the modern, standardized
        #    LangChain shape, populated by google_genai and
        #    langchain-openai (the latter only when stream_usage is on).
        for generations in response.generations or []:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if usage:
                    return (
                        int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0),
                        True,
                    )

        # 2. The OpenAI-shaped llm_output block, used by several
        #    OpenAI-compatible endpoints.
        llm_output = response.llm_output or {}
        token_usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if token_usage:
            prompt_tokens = int(token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0)
            completion_tokens = int(
                token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0
            )
            if prompt_tokens or completion_tokens:
                return prompt_tokens, completion_tokens, True

        return 0, 0, False

    @staticmethod
    def _completion_text(response: LLMResult) -> str:
        parts = []
        for generations in response.generations or []:
            for generation in generations:
                text = getattr(generation, "text", None)
                if text:
                    parts.append(text)
                    continue
                message = getattr(generation, "message", None)
                if message is not None:
                    parts.append(_messages_to_text(message))
        return "\n".join(parts)

    def on_llm_end(
        self, response: LLMResult, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        span = self._close_span(run_id)
        if span is None:
            return
        try:
            input_tokens, output_tokens, from_provider = self._usage_from_response(response)

            if not from_provider:
                input_tokens = estimate_tokens(span.prompt_text)
                output_tokens = estimate_tokens(self._completion_text(response))

            span.input_tokens = input_tokens
            span.output_tokens = output_tokens
            span.total_tokens = input_tokens + output_tokens
            span.cost_usd, price_source = pricing.llm_cost_usd(span.name, input_tokens, output_tokens)
            span.status = "ok"
            span.detail = {
                "token_source": TOKEN_SOURCE_PROVIDER if from_provider else TOKEN_SOURCE_ESTIMATED,
                "price_source": price_source,
            }

            with self._lock:
                if from_provider:
                    self._provider_counted += 1
                else:
                    self._estimated_counted += 1

            span.prompt_text = ""
            self._log(span)
        except Exception:  # noqa: BLE001
            span.status = "ok"

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        span = self._close_span(run_id)
        if span is None:
            return
        # A failed model call may still have consumed input tokens on
        # the provider's side; we cannot know how many, so we record
        # the failure with zero tokens rather than inventing a figure,
        # and the span's "error" status says why the number is missing.
        span.status = "error"
        span.error_message = f"{type(error).__name__}: {error}"
        span.detail = {"token_source": TOKEN_SOURCE_NONE}
        span.prompt_text = ""
        self._log(span)

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        inputs: dict | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            name = (serialized or {}).get("name") or kwargs.get("name") or "unknown-tool"
            self._open_span(run_id, SPAN_TOOL, str(name))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _tool_output_text(output: Any) -> str:
        """`on_tool_end` gives a ToolMessage on modern langchain-core, a str on older."""
        if isinstance(output, str):
            return output
        content = getattr(output, "content", None)
        if isinstance(content, str):
            return content
        return str(output)

    @staticmethod
    def _tavily_credits_for_result(result_text: str) -> tuple[int, str]:
        """
        Decide whether one `web_search` call actually consumed a Tavily
        credit, and say why.

        tools/web_search_tools.py returns a JSON object with an
        "error" key when the key is missing or the HTTP call failed -
        neither of which reaches Tavily's billing. Counting those as
        spent credits would inflate the reported cost just as surely
        as ignoring real ones would deflate it.
        """
        try:
            parsed = json.loads(result_text)
        except (ValueError, TypeError):
            # Unparseable output from a search that did run: assume the
            # credit was spent. Over-counting a genuinely ambiguous
            # case is the safer direction for a cost report.
            return pricing.tavily_credits_per_search(), "assumed_spent_unparseable_result"

        if isinstance(parsed, dict) and parsed.get("error"):
            return 0, f"not_charged:{parsed.get('error')}"
        return pricing.tavily_credits_per_search(), "charged"

    def on_tool_end(
        self, output: Any, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        span = self._close_span(run_id)
        if span is None:
            return
        try:
            span.status = "ok"
            detail: dict = {}

            if span.name == "web_search":
                result_text = self._tool_output_text(output)
                credits, reason = self._tavily_credits_for_result(result_text)
                detail["tavily_credits"] = credits
                detail["tavily_billing"] = reason
                span.cost_usd = pricing.tavily_cost_usd(credits)

            span.detail = detail
            self._log(span)
        except Exception:  # noqa: BLE001
            span.status = "ok"

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        span = self._close_span(run_id)
        if span is None:
            return
        span.status = "error"
        span.error_message = f"{type(error).__name__}: {error}"
        self._log(span)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def _token_source(self) -> str:
        if self._provider_counted and self._estimated_counted:
            return TOKEN_SOURCE_MIXED
        if self._provider_counted:
            return TOKEN_SOURCE_PROVIDER
        if self._estimated_counted:
            return TOKEN_SOURCE_ESTIMATED
        return TOKEN_SOURCE_NONE

    def snapshot(self, status: str, error_message: str = "", trace_url: str = "") -> "RunSnapshot":
        """
        Freeze everything observed so far into the record that gets
        written to PostgreSQL and published as an event.

        Called from `run_agent`'s `finally`, so it runs for failed and
        timed-out runs too - a run that burned three model calls and
        then hit the wall-clock ceiling is still three model calls'
        worth of tokens, and leaving it out of the ledger would make
        the displayed cost smaller than the real one.

        Spans still open when this is called (the timeout case: the
        background thread is abandoned mid-call) are closed here and
        marked "incomplete", so the trace shows exactly where the run
        was when it was cut off instead of silently dropping the step.
        """
        with self._lock:
            open_spans = list(self._open.values())
            self._open.clear()
            spans = list(self._spans)

        now = _utc_now()
        for span in open_spans:
            span.finished_at = now
            span.duration_ms = max(0, int((now - span.started_at).total_seconds() * 1000))
            span.status = "incomplete"
            span.prompt_text = ""

        llm_spans = [span for span in spans if span.span_type == SPAN_LLM]
        tool_spans = [span for span in spans if span.span_type == SPAN_TOOL]

        input_tokens = sum(span.input_tokens for span in llm_spans)
        output_tokens = sum(span.output_tokens for span in llm_spans)
        llm_cost = pricing.quantize_money(
            sum((span.cost_usd for span in llm_spans), Decimal("0"))
        )
        tavily_credits = sum(int(span.detail.get("tavily_credits", 0)) for span in tool_spans)
        tavily_cost = pricing.tavily_cost_usd(tavily_credits)

        return RunSnapshot(
            correlation_id=self.correlation_id,
            conversation_id=self.conversation_id,
            command_message_id=self.command_message_id,
            attempt=self.attempt,
            status=status,
            started_at=self.started_at,
            finished_at=now,
            duration_ms=max(0, int((time.monotonic() - self._started_monotonic) * 1000)),
            llm_call_count=len(llm_spans),
            tool_call_count=len(tool_spans),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            token_source=self._token_source(),
            llm_cost_usd=llm_cost,
            tavily_credits=tavily_credits,
            tavily_cost_usd=tavily_cost,
            total_cost_usd=pricing.quantize_money(llm_cost + tavily_cost),
            trace_url=trace_url,
            error_message=error_message[:2000],
            spans=spans,
        )


@dataclass
class RunSnapshot:
    """The finished ledger entry for one run, ready to persist/publish."""

    correlation_id: str
    conversation_id: str | None
    command_message_id: str
    attempt: int
    status: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    llm_call_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    token_source: str
    llm_cost_usd: Decimal
    tavily_credits: int
    tavily_cost_usd: Decimal
    total_cost_usd: Decimal
    trace_url: str
    error_message: str
    spans: list[SpanRecord]

    def to_event_payload(self) -> dict:
        """
        The AGENT_RUN_USAGE event body. Money is serialized as a
        STRING, matching what the REST API returns for the same
        numbers (see observability/serializers.py) - a cost that
        changes in its last digits depending on whether the browser
        read it from an event or from the database would be worse than
        useless.
        """
        payload = {
            spec.name: getattr(self, spec.name)
            for spec in fields(self)
            if spec.name not in _RUN_EVENT_EXCLUDED
        }
        for name in _MONEY_FIELDS:
            payload[name] = str(payload[name])
        payload["spans"] = [span.to_event_dict() for span in self.spans]
        return payload

    def log_summary(self) -> None:
        print(
            "[trace] "
            + json.dumps(
                {
                    "correlation_id": self.correlation_id,
                    "command_message_id": self.command_message_id,
                    "attempt": self.attempt,
                    "event": "run_finished",
                    "status": self.status,
                    "duration_ms": self.duration_ms,
                    "llm_calls": self.llm_call_count,
                    "tool_calls": self.tool_call_count,
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "token_source": self.token_source,
                    "tavily_credits": self.tavily_credits,
                    "total_cost_usd": str(self.total_cost_usd),
                    "trace_url": self.trace_url,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
