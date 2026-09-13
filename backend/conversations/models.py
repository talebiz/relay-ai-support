"""
Conversation/Message persistence - part 4, commit 1.

Project brief section 6 requires a clear boundary between "the live
stream" and "the record of system": if the SSE connection drops mid
reply, the database - NOT the RabbitMQ event stream - must be the
source of truth for what the assistant actually said. These two models
are that source of truth.

Design decisions:

1. One Conversation per Order. The support chat lives on the order
   detail page (mirroring the reference course's chat UI, mounted next
   to one order), so `Conversation.order` is a `OneToOneField`: an
   order gets at most one conversation, created lazily on first
   message rather than at order-creation time.

2. `Conversation.user` is denormalized (also reachable via
   `order.user`) for the exact same reason `RefundRequest.user` is
   denormalized in orders/models.py: it lets every authorization check
   in conversations/services.py be a single indexed column filter,
   not a join a future refactor could accidentally drop.

3. `Conversation.correlation_id` is what makes part 3's correlation_id
   STABLE across an entire conversation instead of minted fresh per
   HTTP request (the part-3 placeholder this replaces). Every command,
   every event, and the SSE subscription for this conversation all key
   off this one field - see support/views.py and streaming/views.py.

4. `Message.tool_calls_json` is optional and unused until part 4
   commit 2 adds real tools; it exists now so the schema does not need
   another migration once `agent_tool_called` events start carrying
   real tool-call data the frontend can render in an "agent activity"
   panel.
"""

import uuid

from django.conf import settings
from django.db import models


class Conversation(models.Model):
    """
    One chat thread, always attached to exactly one order.

    Authorization note (same rule as orders/models.py Order): nothing
    on this model decides who may read or write it - that happens in
    conversations/services.py, filtering on `user`, exactly like
    orders/services.py does for Order.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField("orders.Order", on_delete=models.CASCADE, related_name="conversation")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations")
    correlation_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Conversation({self.id}) for order {self.order_id}"


class MessageRole(models.TextChoices):
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"


class Message(models.Model):
    """
    One turn in a conversation. Written by two different processes:
    Django writes the USER role synchronously in support/views.py
    (before the command is even published), and the ai-worker process
    writes the ASSISTANT role via plain psycopg (see ai-worker/db.py)
    once the LLM has finished replying - the worker is not a Django
    app and does not use the ORM.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=MessageRole.choices)
    content = models.TextField()
    tool_calls_json = models.JSONField(
        blank=True,
        null=True,
        help_text="Populated by the ai-worker once real tools exist (part 4 "
        "commit 2); drives an 'agent activity' panel in the UI.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self):
        return f"Message({self.role}) in {self.conversation_id}"
