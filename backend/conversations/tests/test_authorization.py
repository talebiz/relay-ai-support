"""
Tests for the two claims this app exists to make checkable:

1. A conversation belongs to its order's owner, not to whoever
   happened to trigger its creation (relevant when a support agent
   opens a customer's order for the first time).
2. A user cannot read another user's conversation or messages -
   authorization lives in code (conversations/services.py), the same
   "404 not 403" contract as orders/tests/test_authorization.py.

Run with: pytest backend/conversations/tests/ -v
"""

import pytest
from django.core.exceptions import PermissionDenied
from rest_framework.test import APIClient

from accounts.models import Role, User
from conversations import services
from conversations.models import Conversation
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
def alice_order(db, alice, product):
    return Order.objects.create(user=alice, product=product, status=OrderStatus.PENDING)


@pytest.fixture
def bob_order(db, bob, product):
    return Order.objects.create(user=bob, product=product, status=OrderStatus.PENDING)


# --- service-layer tests ---


@pytest.mark.django_db
def test_get_or_create_conversation_is_idempotent(alice, alice_order):
    first = services.get_or_create_conversation_for_order(alice, alice_order.id)
    second = services.get_or_create_conversation_for_order(alice, alice_order.id)
    assert first.id == second.id
    assert Conversation.objects.filter(order=alice_order).count() == 1


@pytest.mark.django_db
def test_conversation_belongs_to_order_owner_not_acting_support_agent(support_agent, bob_order, bob):
    conversation = services.get_or_create_conversation_for_order(support_agent, bob_order.id)
    assert conversation.user_id == bob.id


@pytest.mark.django_db
def test_cannot_create_conversation_for_other_users_order(alice, bob_order):
    with pytest.raises(Order.DoesNotExist):
        services.get_or_create_conversation_for_order(alice, bob_order.id)


@pytest.mark.django_db
def test_get_conversation_for_user_raises_does_not_exist_for_other_users_conversation(
    alice, bob, bob_order
):
    conversation = services.get_or_create_conversation_for_order(bob, bob_order.id)
    with pytest.raises(Conversation.DoesNotExist):
        services.get_conversation_for_user(alice, conversation.id)


@pytest.mark.django_db
def test_support_agent_can_view_any_conversation(support_agent, bob, bob_order):
    conversation = services.get_or_create_conversation_for_order(bob, bob_order.id)
    fetched = services.get_conversation_for_user(support_agent, conversation.id)
    assert fetched.id == conversation.id


@pytest.mark.django_db
def test_anonymous_user_is_rejected(db):
    from django.contrib.auth.models import AnonymousUser

    with pytest.raises(PermissionDenied):
        services.conversations_visible_to(AnonymousUser())


@pytest.mark.django_db
def test_get_conversation_by_correlation_id_enforces_same_boundary(alice, bob, bob_order):
    conversation = services.get_or_create_conversation_for_order(bob, bob_order.id)
    with pytest.raises(Conversation.DoesNotExist):
        services.get_conversation_for_user_by_correlation_id(alice, conversation.correlation_id)

    # The owner (and a support agent) can.
    found = services.get_conversation_for_user_by_correlation_id(bob, conversation.correlation_id)
    assert found.id == conversation.id


@pytest.mark.django_db
def test_list_messages_empty_for_new_conversation(alice, alice_order):
    conversation = services.get_or_create_conversation_for_order(alice, alice_order.id)
    assert list(services.list_messages(alice, conversation.id)) == []


@pytest.mark.django_db
def test_add_message_persists_and_orders_by_created_at(alice, alice_order):
    conversation = services.get_or_create_conversation_for_order(alice, alice_order.id)
    services.add_message(conversation, role="user", content="Where is my order?")
    services.add_message(conversation, role="assistant", content="Let me check.")

    messages = list(services.list_messages(alice, conversation.id))
    assert [m.content for m in messages] == ["Where is my order?", "Let me check."]


# --- HTTP-level tests ---


@pytest.mark.django_db
def test_order_conversation_endpoint_creates_and_is_idempotent(alice, alice_order):
    client = APIClient()
    client.force_authenticate(user=alice)

    resp1 = client.get(f"/api/orders/{alice_order.id}/conversation/")
    assert resp1.status_code == 200

    resp2 = client.get(f"/api/orders/{alice_order.id}/conversation/")
    assert resp2.status_code == 200
    assert resp1.data["id"] == resp2.data["id"]


@pytest.mark.django_db
def test_order_conversation_endpoint_404s_for_other_users_order(alice, bob_order):
    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.get(f"/api/orders/{bob_order.id}/conversation/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_messages_endpoint_404s_for_other_users_conversation(alice, bob, bob_order):
    conversation = services.get_or_create_conversation_for_order(bob, bob_order.id)
    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.get(f"/api/conversations/{conversation.id}/messages/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_messages_endpoint_returns_saved_messages_in_order(alice, alice_order):
    conversation = services.get_or_create_conversation_for_order(alice, alice_order.id)
    services.add_message(conversation, role="user", content="Hi")
    services.add_message(conversation, role="assistant", content="Hello!")

    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.get(f"/api/conversations/{conversation.id}/messages/")

    assert resp.status_code == 200
    assert [m["content"] for m in resp.data] == ["Hi", "Hello!"]
