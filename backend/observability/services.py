"""
Read-side of the observability app: authorization + aggregation.

Same rule as every other domain in this project (orders/services.py,
conversations/services.py): a view never touches `AgentRun.objects`
directly. It asks a function here, passing `request.user`, and the
conversation the runs belong to is resolved through
conversations/services.py - the one authorization chokepoint that
already answers "may this user see this conversation?".

That indirection is the whole point: cost data is customer data. A
customer may see what their own conversations cost; a support agent
may see any conversation's (same RBAC exception as
orders_visible_to). Nobody gets a global cost dashboard by guessing a
conversation id.
"""

from __future__ import annotations

from decimal import Decimal

from accounts.models import User
from conversations import services as conversation_services
from conversations.models import Conversation
from observability.models import AgentRun, RunStatus


def _money(value: Decimal | None) -> str:
    """
    Serialize a money value as a STRING, exactly as DRF serializes the
    DecimalField on AgentRun (observability/serializers.py).

    These aggregates are returned as a plain dict rather than through a
    serializer, and DRF's JSON encoder turns a bare Decimal into a
    FLOAT. Costs here go to six decimal places, which is precisely
    where binary floating point starts changing the last digits - so
    the same number would come back slightly different depending on
    whether it was read from `totals` or from `runs[].total_cost_usd`.
    One shape, one type, everywhere.
    """
    return str(value if value is not None else Decimal("0"))


def runs_for_conversation(acting_user: User, conversation_id) -> tuple[Conversation, list[AgentRun]]:
    """
    Every recorded agent run for one conversation, oldest first, with
    spans prefetched (the UI renders each run's spans inline, so
    fetching them lazily would be one query per run).

    Raises the same `ObjectDoesNotExist` as everywhere else in this
    project when the conversation is not visible to `acting_user` -
    preserving the "can't tell missing from forbidden" property.
    """
    conversation = conversation_services.get_conversation_for_user(acting_user, conversation_id)
    runs = list(
        AgentRun.objects.filter(conversation=conversation)
        .prefetch_related("spans")
        .order_by("started_at")
    )
    return conversation, runs


def summarize(runs: list[AgentRun]) -> dict:
    """
    Roll a list of runs up into the numbers the UI shows as "what this
    conversation has cost so far".

    Two deliberate choices about what gets counted:

      1. FAILED runs are included in the cost. A run that called the
         model twice and then hit the wall-clock ceiling still spent
         those tokens; excluding it would make the displayed number
         smaller than the real bill, which is exactly the failure mode
         the brief's "«رایگان بود» پاسخ این پرسش نیست" is aimed at.
      2. Tavily credits are counted as a separate unit AND converted
         to dollars, because they are not tokens - the brief lists
         them as a distinct thing to add ("اعتبارهای Tavily را اضافه
         کنید"), and a reader needs to see both "how many searches did
         this cost me" and "what is that worth".
    """
    aggregate = {
        "run_count": len(runs),
        "failed_run_count": sum(1 for run in runs if run.status == RunStatus.FAILED),
        "llm_call_count": sum(run.llm_call_count for run in runs),
        "tool_call_count": sum(run.tool_call_count for run in runs),
        "input_tokens": sum(run.input_tokens for run in runs),
        "output_tokens": sum(run.output_tokens for run in runs),
        "total_tokens": sum(run.total_tokens for run in runs),
        "tavily_credits": sum(run.tavily_credits for run in runs),
        "llm_cost_usd": _money(sum((run.llm_cost_usd for run in runs), Decimal("0"))),
        "tavily_cost_usd": _money(sum((run.tavily_cost_usd for run in runs), Decimal("0"))),
        "total_cost_usd": _money(sum((run.total_cost_usd for run in runs), Decimal("0"))),
        "total_duration_ms": sum(run.duration_ms for run in runs),
    }

    # "Real" vs "estimated" must survive aggregation: if ANY run in
    # this conversation fell back to local tokenization, the headline
    # number is not fully provider-reported and the UI says so rather
    # than quietly presenting an estimate as a measurement.
    sources = {run.token_source for run in runs if run.token_source != "none"}
    if not sources:
        aggregate["token_source"] = "none"
    elif sources == {"provider"}:
        aggregate["token_source"] = "provider"
    elif sources == {"estimated"}:
        aggregate["token_source"] = "estimated"
    else:
        aggregate["token_source"] = "mixed"

    return aggregate
