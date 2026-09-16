"""
Execution traces and cost accounting - project brief, section 7
("رصدپذیری و هزینه").

The brief asks for exactly two things, and this app is the system of
record for both:

  1. "ردهای اجرا برای کل اجرای عامل، هر فراخوانی مدل و ابزار، همه
     متصل با correlation_id" - execution traces for the whole agent
     run, for every model call and every tool call, all tied together
     by correlation_id.  -> `AgentRun` (one row per agent run) and
     `RunSpan` (one row per model call or tool call inside that run).

  2. "هزینه هر گفت‌وگو را در رابط کاربری نشان دهید. API رایگان است،
     اما «رایگان بود» پاسخ این پرسش نیست: token ها را واقعی بشمارید،
     اعتبارهای Tavily را اضافه کنید." - show the cost of each
     conversation in the UI; the API being free is not the answer:
     count tokens for real and add Tavily credits.  -> the token and
     cost columns below, aggregated per conversation by
     observability/services.py and rendered by the frontend's
     UsagePanel.

--- Why a database table when Langfuse is also wired up ---

The worker also sends every run to Langfuse (ai-worker/observability/
langfuse_client.py) when keys are configured, and `AgentRun.trace_url`
links straight to that trace. Langfuse is the deep, per-span debugging
view; these tables are the part the PRODUCT needs and must not depend
on a third-party SaaS being reachable:

  - the UI must be able to show "this conversation cost X" without
    calling out to Langfuse's API on every page render,
  - the project must still satisfy section 7 when Langfuse keys are
    not configured at all (a defense on a laptop with no internet),
  - and cost is data this system owns; it belongs in this system's
    own database next to the conversation it was spent on.

So: Langfuse for the timeline, PostgreSQL for the ledger. Both are
keyed by the same `correlation_id`, which is exactly the "همه متصل با
correlation_id" the brief asks for.

--- Who writes these rows ---

The AI worker, in plain SQL (ai-worker/db.py::insert_agent_run), for
the same reason it writes `conversations_message` that way: the worker
is not a Django app and does not load the ORM (see ai-worker/db.py's
module docstring). Django only ever READS these tables, through
observability/services.py. Keep the column names here and the INSERT
in ai-worker/db.py in sync by hand - the same, already-documented
tradeoff as the duplicated envelope.

--- Why one row per ATTEMPT, not per command ---

`command_message_id` is NOT unique on its own: a command that fails
and is retried (messaging/topology.py's backoff staircase) runs the
agent again, and that second attempt really does spend more tokens.
Recording only the last attempt would under-report the true cost,
which is precisely the kind of comfortable rounding the brief's
"«رایگان بود» پاسخ این پرسش نیست" warns against. So the uniqueness
constraint is (command_message_id, attempt): every execution that
burned tokens gets its own row, and a redelivery that is skipped by
the idempotency check burns nothing and writes nothing.
"""

import uuid

from django.db import models


class RunStatus(models.TextChoices):
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class TokenSource(models.TextChoices):
    """
    How the token counts on this row were obtained - recorded rather
    than hidden, because "count tokens for real" is only an honest
    claim if the rows that were NOT counted for real say so.

    PROVIDER  - the model provider reported usage itself (LangChain's
                `usage_metadata` / `token_usage`). Exact.
    ESTIMATED - the provider returned no usage at all (some
                OpenAI-compatible free tiers do not, especially while
                streaming), so the worker tokenized the text locally.
                Close, not exact.
    MIXED     - a run with several model calls where some reported
                usage and some did not.
    NONE      - the run made no model call at all (e.g. it failed
                before reaching one).
    """

    PROVIDER = "provider", "Reported by provider"
    ESTIMATED = "estimated", "Estimated locally"
    MIXED = "mixed", "Mixed"
    NONE = "none", "No model call"


class AgentRun(models.Model):
    """One execution of the agent for one command (one user turn)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Nullable so a run can still be recorded if the conversation row
    # disappears underneath it; SET_NULL rather than CASCADE because a
    # deleted conversation should not silently erase the record of what
    # was already spent on it.
    conversation = models.ForeignKey(
        "conversations.Conversation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_runs",
    )

    # The one id that ties this row to the command envelope, the
    # RabbitMQ events, the SSE frames the browser received, and the
    # Langfuse trace (section 7: "همه متصل با correlation_id").
    correlation_id = models.UUIDField(db_index=True)
    command_message_id = models.UUIDField()
    attempt = models.PositiveSmallIntegerField(
        default=0,
        help_text="Envelope retry_count at the time of this execution; 0 is the first try.",
    )

    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.RUNNING)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    llm_call_count = models.PositiveIntegerField(default=0)
    tool_call_count = models.PositiveIntegerField(default=0)

    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    token_source = models.CharField(
        max_length=20, choices=TokenSource.choices, default=TokenSource.NONE
    )

    # 6 decimal places: a single free-tier call can cost a small
    # fraction of a cent, and rounding those to 2 places would turn
    # every real number in this table into 0.00.
    llm_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    tavily_credits = models.PositiveIntegerField(default=0)
    tavily_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    total_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)

    trace_url = models.URLField(blank=True, default="", max_length=500)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["command_message_id", "attempt"],
                name="unique_run_per_command_attempt",
            )
        ]
        # Named explicitly (rather than letting Django hash a name) so
        # the hand-written migration in this app stays byte-stable.
        indexes = [
            models.Index(fields=["conversation", "started_at"], name="obs_run_conv_started_idx"),
        ]

    def __str__(self):
        return f"AgentRun({self.id}) {self.status} ${self.total_cost_usd}"


class SpanType(models.TextChoices):
    LLM = "llm", "Model call"
    TOOL = "tool", "Tool call"


class RunSpan(models.Model):
    """
    One step inside a run: either a model call or a tool call.

    This is the "هر فراخوانی مدل و ابزار" half of section 7. A run with
    two tool calls typically produces five spans: llm -> tool -> llm ->
    tool -> llm, and their `sequence` numbers replay the agent's actual
    decision path in order.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="spans")
    sequence = models.PositiveIntegerField(default=0)
    span_type = models.CharField(max_length=10, choices=SpanType.choices)
    name = models.CharField(
        max_length=200, help_text="Model name for llm spans, tool name for tool spans."
    )

    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)

    status = models.CharField(max_length=20, default="ok")
    error_message = models.TextField(blank=True, default="")

    # Small, bounded, tool-specific extras: which price row was used
    # for an llm span, whether a web_search span actually consumed a
    # Tavily credit, and so on. Deliberately NOT the tool's full
    # input/output - that lives in Langfuse (and, for tool results the
    # user is shown, in Message.tool_calls_json).
    detail = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["sequence"]
        indexes = [models.Index(fields=["run", "sequence"], name="obs_span_run_seq_idx")]

    def __str__(self):
        return f"RunSpan({self.span_type}:{self.name})"
