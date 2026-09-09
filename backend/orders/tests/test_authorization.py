"""
These tests exist to make one specific claim checkable, not just
assertable in a README: "a user cannot access another user's data,
and authorization lives in code."

Run with:  pytest backend/orders/tests/test_authorization.py -v
"""

import uuid

import pytest
from django.core.exceptions import PermissionDenied
from rest_framework.test import APIClient

from accounts.models import Role, User
from orders import services
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


# --- service-layer tests: this is the function the future AI tools call ---


@pytest.mark.django_db
def test_orders_visible_to_only_returns_own_orders(alice, alice_order, bob_order):
    visible = services.orders_visible_to(alice)
    assert list(visible) == [alice_order]
    assert bob_order not in visible


@pytest.mark.django_db
def test_get_order_for_user_raises_does_not_exist_for_other_users_order(alice, bob_order):
    """
    Critically: this raises Order.DoesNotExist, not PermissionDenied.
    Alice should not be able to distinguish "not yours" from "doesn't
    exist" - that distinction itself would leak information.
    """
    with pytest.raises(Order.DoesNotExist):
        services.get_order_for_user(alice, bob_order.id)


@pytest.mark.django_db
def test_get_order_for_user_raises_for_nonexistent_order(alice):
    with pytest.raises(Order.DoesNotExist):
        services.get_order_for_user(alice, uuid.uuid4())


@pytest.mark.django_db
def test_support_agent_sees_all_orders(support_agent, alice_order, bob_order):
    visible = services.orders_visible_to(support_agent)
    assert set(visible) == {alice_order, bob_order}


@pytest.mark.django_db
def test_anonymous_user_is_rejected(db):
    from django.contrib.auth.models import AnonymousUser

    with pytest.raises(PermissionDenied):
        services.orders_visible_to(AnonymousUser())


@pytest.mark.django_db
def test_create_refund_request_cannot_target_other_users_order(alice, bob_order):
    with pytest.raises(Order.DoesNotExist):
        services.create_refund_request(alice, bob_order.id, reason="not mine")


# --- HTTP-level tests: prove the API surface enforces the same boundary ---


@pytest.mark.django_db
def test_order_detail_endpoint_404s_for_other_users_order(bob, bob_order, alice):
    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.get(f"/api/orders/{bob_order.id}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_order_detail_endpoint_200s_for_own_order(alice, alice_order):
    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.get(f"/api/orders/{alice_order.id}/")
    assert resp.status_code == 200
    assert resp.data["id"] == str(alice_order.id)


@pytest.mark.django_db
def test_order_list_endpoint_never_leaks_other_users_rows(alice, alice_order, bob_order):
    client = APIClient()
    client.force_authenticate(user=alice)
    resp = client.get("/api/orders/")
    assert resp.status_code == 200
    returned_ids = {row["id"] for row in resp.data}
    assert returned_ids == {str(alice_order.id)}


@pytest.mark.django_db
def test_order_list_endpoint_requires_authentication(db):
    client = APIClient()
    resp = client.get("/api/orders/")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_support_agent_can_view_any_order_via_api(support_agent, bob_order):
    client = APIClient()
    client.force_authenticate(user=support_agent)
    resp = client.get(f"/api/orders/{bob_order.id}/")
    assert resp.status_code == 200
