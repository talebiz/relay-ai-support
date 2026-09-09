"""
Single authorization choke point for order/refund data.

Why this file exists (see project brief, section 2 - "دامنه و احراز هویت"):

    "کاربری نباید به داده کاربر دیگر دسترسی داشته باشد. مجوزدهی باید در
    کد باشد، نه در prompt: هر ابزاری که به داده مشتری دست می‌زند باید بر
    اساس کاربر احراز هویت شده فیلتر کند."

    ("A user must not be able to access another user's data.
    Authorization must live in code, not in the prompt: every tool that
    touches customer data must filter based on the authenticated user.")

The DRF views in this app call these functions. In part 3 of this
project, the AI worker's database tools (get_order_details,
get_refund_history, ...) will ALSO call these exact same functions,
given the authenticated user's id that Django resolved from the JWT
before the command was ever published to RabbitMQ.

That is the whole point: there is one function that knows how to fetch
"this user's orders", and it is used identically whether the caller is
a human clicking around the React app or an LLM-driven tool acting on
that human's behalf. The LLM is never trusted to say "show me user 42's
orders" and have that be believed - it can only ever call a tool that
already has `acting_user` bound to whoever is authenticated, and the
functions below refuse to return anything outside that boundary.

None of this is enforced by asking the model nicely in a system prompt.
It is enforced by the fact that these functions do not accept an
arbitrary user_id from the caller in the customer-facing path - they
take `acting_user` (a real Django User instance resolved from auth) and
filter on it directly. There is no code path here that lets a customer
argument their way into another customer's rows.
"""

from __future__ import annotations

from typing import Iterable

from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet

from accounts.models import Role, User
from orders.models import Order, RefundRequest


def _is_support_agent(user: User) -> bool:
    return user.is_authenticated and user.role == Role.SUPPORT_AGENT


def orders_visible_to(acting_user: User) -> QuerySet[Order]:
    """
    Return the Order queryset `acting_user` is allowed to see.

    - A customer sees only their own orders (filtered on `user=acting_user`).
    - A support agent sees all orders (the only role-based exception,
      matching the project brief's bonus RBAC scenario).

    This is the ONLY function in the codebase that should build an
    unfiltered `Order.objects.all()` queryset. Everything else -
    views, serializers, and (later) AI-worker tools - must call this
    function rather than querying Order directly.
    """
    if not acting_user or not acting_user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    if _is_support_agent(acting_user):
        return Order.objects.select_related("product", "user").all()
    return Order.objects.select_related("product", "user").filter(user=acting_user)


def get_order_for_user(acting_user: User, order_id) -> Order:
    """
    Fetch a single order, raising Order.DoesNotExist if `acting_user`
    is not allowed to see it - deliberately the same exception as "the
    order doesn't exist", so this endpoint (and the future AI tool
    wrapping it) never leaks "the order exists but isn't yours" to an
    unauthorized caller.
    """
    return orders_visible_to(acting_user).get(id=order_id)


def refund_requests_visible_to(acting_user: User) -> QuerySet[RefundRequest]:
    """Same authorization rule as orders_visible_to, for refund history."""
    if not acting_user or not acting_user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    if _is_support_agent(acting_user):
        return RefundRequest.objects.select_related("order", "order__product", "user").all()
    return RefundRequest.objects.select_related("order", "order__product", "user").filter(user=acting_user)


def refund_history_for_order(acting_user: User, order_id) -> Iterable[RefundRequest]:
    """
    Refund history for one order, scoped to what `acting_user` may see.
    Implemented via get_order_for_user so the same "can't tell whether
    it's missing or forbidden" property applies here too.
    """
    order = get_order_for_user(acting_user, order_id)
    return refund_requests_visible_to(acting_user).filter(order=order)


def create_refund_request(acting_user: User, order_id, reason: str) -> RefundRequest:
    """
    The one write/"action" operation in this domain (the project brief
    requires at least one tool that performs an action rather than just
    reading data). `order_id` is still resolved through
    get_order_for_user, so a customer cannot open a refund against an
    order that isn't theirs by guessing a UUID - support agents can,
    consistent with their elevated role.
    """
    order = get_order_for_user(acting_user, order_id)
    return RefundRequest.objects.create(user=order.user, order=order, reason=reason)
