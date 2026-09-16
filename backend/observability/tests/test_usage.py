"""
Tests for the two claims part 7 makes checkable:

1. Cost data is customer data. A user may read the cost of their own
   conversation and nobody else's - the same "404 not 403" contract as
   every other endpoint in this project - while a support agent may
   read any (the same RBAC exception as orders_visible_to).
2. The totals are honest. Failed runs are counted, Tavily credits are
   counted separately from tokens, and a conversation whose runs were
   partly estimated reports "mixed" rather than claiming everything was
   measured.

Run with: pytest backend/observability/tests/ -v
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Role, User
from conversations import services as conversation_services
from conversations.models import Conversation
from observability import services
from observability.models import AgentRun, RunSpan, RunStatus, SpanType, TokenSource
from orders.models import Order, OrderStatus, Product


@pytest.fixture
def product(db):
    return Product.objects.create(name="Widget", sku="W-1", price_cents=1000)


@pytest.fixture
def alice(db):
    return User.objects.create_user(username="alice", password="pw")


@pytest.fixture
def bob(db):
    return User.objects.create_user(username="bob", password="pw")


@pytest.fixture
def support_agent(db):
    return User.objects.create_user(username="agent", password="pw", role=Role.SUPPORT_AGENT)


@pytest.fixture
def alice_conversation(db, alice, product) -> Conversation:
    order = Order.objects.create(user=alice, product=product, status=OrderStatus.PENDING)
    return conversation_services.get_or_create_conversation_for_order(alice, order.id)


@pytest.fixture
def bob_conversation(db, bob, product) -> Conversation:
    order = Order.objects.create(user=bob, product=product, status=OrderStatus.PENDING)
    return conversation_services.get_or_create_conversation_for_order(bob, order.id)


def make_run(
    conversation: Conversation,
    *,
    status=RunStatus.COMPLETED,
    input_tokens=100,
    output_tokens=50,
    token_source=TokenSource.PROVIDER,
    llm_cost="0.000030",
    tavily_credits=0,
    tavily_cost="0",
    attempt=0,
) -> AgentRun:
    import uuid

    started = timezone.now()
    run = AgentRun.objects.create(
        conversation=conversation,
        correlation_id=conversation.correlation_id,
        command_message_id=uuid.uuid4(),
        attempt=attempt,
        status=status,
        started_at=started,
        finished_at=started + timedelta(milliseconds=1200),
        duration_ms=1200,
        llm_call_count=1,
        tool_call_count=1 if tavily_credits else 0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        token_source=token_source,
        llm_cost_usd=Decimal(llm_cost),
        tavily_credits=tavily_credits,
        tavily_cost_usd=Decimal(tavily_cost),
        total_cost_usd=Decimal(llm_cost) + Decimal(tavily_cost),
    )
    RunSpan.objects.create(
        run=run,
        sequence=1,
        span_type=SpanType.LLM,
        name="gpt-4o-mini",
        started_at=started,
        finished_at=started + timedelta(milliseconds=900),
        duration_ms=900,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cost_usd=Decimal(llm_cost),
        detail={"token_source": token_source, "price_source": "table:gpt-4o-mini"},
    )
    return run


# --- authorization ---------------------------------------------------


@pytest.mark.django_db
def test_owner_customer_gets_redacted_usage_payload(alice, alice_conversation):
    """
    A customer's own conversation still returns 200, but the payload is
    stripped to the three headline fields the customer UI renders. Per-
    run data and every non-headline total are zeroed BEFORE they leave
    the server, so DevTools cannot reveal what the panel hides.
    """
    make_run(alice_conversation, input_tokens=100, output_tokens=50, llm_cost="0.000030")
    client = APIClient()
    client.force_authenticate(user=alice)

    resp = client.get(f"/api/conversations/{alice_conversation.id}/usage/")

    assert resp.status_code == 200
    totals = resp.data["totals"]
    # The three the customer UI actually renders survive verbatim.
    assert totals["run_count"] == 1
    assert totals["total_tokens"] == 150
    assert Decimal(totals["total_cost_usd"]) == Decimal("0.000030")
    # Everything else is zeroed / anonymized.
    for key in (
        "failed_run_count",
        "llm_call_count",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
        "tavily_credits",
        "total_duration_ms",
    ):
        assert totals[key] == 0, key
    for key in ("llm_cost_usd", "tavily_cost_usd"):
        assert Decimal(totals[key]) == Decimal("0"), key
    assert totals["token_source"] == "none"
    # No per-run rows for a customer.
    assert resp.data["runs"] == []


@pytest.mark.django_db
def test_usage_endpoint_404s_for_other_users_conversation(alice, bob_conversation):
    make_run(bob_conversation)
    client = APIClient()
    client.force_authenticate(user=alice)

    resp = client.get(f"/api/conversations/{bob_conversation.id}/usage/")

    assert resp.status_code == 404


@pytest.mark.django_db
def test_support_agent_receives_full_usage_payload(support_agent, bob_conversation):
    """
    The admin/operator view: full totals AND every run with its spans,
    including correlation ids and per-span costs.
    """
    make_run(bob_conversation, llm_cost="0.000030")
    client = APIClient()
    client.force_authenticate(user=support_agent)

    resp = client.get(f"/api/conversations/{bob_conversation.id}/usage/")

    assert resp.status_code == 200
    totals = resp.data["totals"]
    assert totals["run_count"] == 1
    assert totals["llm_call_count"] == 1
    assert totals["input_tokens"] == 100
    assert totals["output_tokens"] == 50
    assert Decimal(totals["total_cost_usd"]) == Decimal("0.000030")
    assert len(resp.data["runs"]) == 1
    assert len(resp.data["runs"][0]["spans"]) == 1


@pytest.mark.django_db
def test_usage_endpoint_requires_authentication(alice_conversation):
    resp = APIClient().get(f"/api/conversations/{alice_conversation.id}/usage/")
    assert resp.status_code == 401


# --- honest totals ---------------------------------------------------


@pytest.mark.django_db
def test_failed_runs_are_counted_in_the_cost(alice_conversation):
    """A run that spent tokens and then failed still changed the bill."""
    make_run(alice_conversation, llm_cost="0.000100")
    make_run(alice_conversation, status=RunStatus.FAILED, llm_cost="0.000050")

    _conversation, runs = services.runs_for_conversation(
        alice_conversation.user, alice_conversation.id
    )
    totals = services.summarize(runs)

    assert totals["run_count"] == 2
    assert totals["failed_run_count"] == 1
    assert Decimal(totals["total_cost_usd"]) == Decimal("0.000150")


@pytest.mark.django_db
def test_tavily_credits_are_counted_separately_from_tokens(alice_conversation):
    make_run(
        alice_conversation,
        llm_cost="0.000100",
        tavily_credits=2,
        tavily_cost="0.016000",
    )

    _conversation, runs = services.runs_for_conversation(
        alice_conversation.user, alice_conversation.id
    )
    totals = services.summarize(runs)

    assert totals["tavily_credits"] == 2
    assert Decimal(totals["tavily_cost_usd"]) == Decimal("0.016000")
    assert Decimal(totals["total_cost_usd"]) == Decimal("0.016100")


@pytest.mark.django_db
def test_token_source_is_mixed_when_any_run_was_estimated(alice_conversation):
    make_run(alice_conversation, token_source=TokenSource.PROVIDER)
    make_run(alice_conversation, token_source=TokenSource.ESTIMATED)

    _conversation, runs = services.runs_for_conversation(
        alice_conversation.user, alice_conversation.id
    )

    assert services.summarize(runs)["token_source"] == "mixed"


@pytest.mark.django_db
def test_empty_conversation_reports_zero_not_an_error(alice, alice_conversation):
    client = APIClient()
    client.force_authenticate(user=alice)

    resp = client.get(f"/api/conversations/{alice_conversation.id}/usage/")

    assert resp.status_code == 200
    assert resp.data["totals"]["run_count"] == 0
    assert Decimal(resp.data["totals"]["total_cost_usd"]) == Decimal("0")
    assert resp.data["totals"]["token_source"] == "none"


@pytest.mark.django_db
def test_retry_attempts_are_recorded_as_separate_runs(alice_conversation):
    """
    The uniqueness constraint is (command_message_id, attempt), so the
    same command retried once produces two rows - and both are billed.
    """
    import uuid

    message_id = uuid.uuid4()
    started = timezone.now()
    for attempt in (0, 1):
        AgentRun.objects.create(
            conversation=alice_conversation,
            correlation_id=alice_conversation.correlation_id,
            command_message_id=message_id,
            attempt=attempt,
            status=RunStatus.FAILED if attempt == 0 else RunStatus.COMPLETED,
            started_at=started + timedelta(seconds=attempt),
            finished_at=started + timedelta(seconds=attempt, milliseconds=500),
            duration_ms=500,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            token_source=TokenSource.PROVIDER,
            llm_cost_usd=Decimal("0.000030"),
            total_cost_usd=Decimal("0.000030"),
        )

    _conversation, runs = services.runs_for_conversation(
        alice_conversation.user, alice_conversation.id
    )
    totals = services.summarize(runs)

    assert totals["run_count"] == 2
    assert Decimal(totals["total_cost_usd"]) == Decimal("0.000060")


@pytest.mark.django_db
def test_money_is_serialized_as_strings_everywhere(support_agent, alice_conversation):
    """
    Costs go to six decimal places, so a float round-trip would change
    their last digits. Both the aggregate dict and the per-run
    serializer must hand the browser the same type: a string.

    Runs through a support agent because the customer response redacts
    the per-run list (see test_owner_customer_gets_redacted_usage_payload).
    """
    make_run(alice_conversation, llm_cost="0.000123")

    client = APIClient()
    client.force_authenticate(user=support_agent)
    resp = client.get(f"/api/conversations/{alice_conversation.id}/usage/")

    assert isinstance(resp.data["totals"]["total_cost_usd"], str)
    assert resp.data["totals"]["total_cost_usd"] == "0.000123"
    assert isinstance(resp.data["runs"][0]["total_cost_usd"], str)
    assert resp.data["runs"][0]["total_cost_usd"] == "0.000123"
