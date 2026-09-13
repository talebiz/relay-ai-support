"""
Single authorization choke point for conversation/message data - the
same pattern as orders/services.py, deliberately duplicated rather than
generalized, so each domain's authorization rule stays a short,
readable function that is easy to defend line by line.

Every view (conversations/views.py, support/views.py, streaming/views.py)
resolves a Conversation through one of these functions, never through
`Conversation.objects.get(...)` directly. That is what makes "a user
cannot access another user's conversation" a fact about the codebase,
not a convention someone has to remember.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet

from accounts.models import Role, User
from conversations.models import Conversation, Message
from orders import services as order_services


def _is_support_agent(user: User) -> bool:
    return user.is_authenticated and user.role == Role.SUPPORT_AGENT


def conversations_visible_to(acting_user: User) -> QuerySet[Conversation]:
    """
    - A customer sees only conversations attached to their own orders.
    - A support agent sees every conversation (same bonus RBAC
      exception as orders_visible_to).
    """
    if not acting_user or not acting_user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    if _is_support_agent(acting_user):
        return Conversation.objects.select_related("order", "user").all()
    return Conversation.objects.select_related("order", "user").filter(user=acting_user)


def get_or_create_conversation_for_order(acting_user: User, order_id) -> Conversation:
    """
    Fetch-or-create the ONE conversation for this order.

    `orders.services.get_order_for_user` already enforces the exact
    authorization rule used everywhere else in the codebase (own
    order, or any order if support agent) and raises
    `Order.DoesNotExist` - not `PermissionDenied` - when the order
    isn't visible. We deliberately let that exception propagate
    unchanged so this function has the same "can't tell missing from
    forbidden" property the rest of the app relies on.

    The conversation's owner is always the ORDER's owner
    (`order.user`), never `acting_user`: a support agent opening bob's
    order gets bob's conversation, not a new one credited to the
    agent.
    """
    order = order_services.get_order_for_user(acting_user, order_id)
    conversation, _created = Conversation.objects.get_or_create(
        order=order, defaults={"user": order.user}
    )
    return conversation


def get_conversation_for_user(acting_user: User, conversation_id) -> Conversation:
    """Same 404-not-403 property as orders.services.get_order_for_user."""
    return conversations_visible_to(acting_user).get(id=conversation_id)


def get_conversation_for_user_by_correlation_id(
    acting_user: User, correlation_id
) -> Conversation:
    """
    Used exclusively by streaming/views.py to authorize an SSE
    connection BEFORE opening it, keyed by the correlation_id in the
    URL rather than the conversation's primary key.
    """
    return conversations_visible_to(acting_user).get(correlation_id=correlation_id)


def list_messages(acting_user: User, conversation_id):
    """Full message history, scoped by the same rule as above."""
    conversation = get_conversation_for_user(acting_user, conversation_id)
    return conversation.messages.all()


def add_message(conversation: Conversation, role: str, content: str, tool_calls=None) -> Message:
    """
    The one write path for a Message from Django's side. Used by
    support/views.py to persist the customer's message synchronously,
    before the command is even published - so the message exists in
    the system of record regardless of what happens to it downstream.
    """
    return Message.objects.create(
        conversation=conversation,
        role=role,
        content=content,
        tool_calls_json=tool_calls,
    )
