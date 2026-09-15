"""
Entry point of the event-driven pipeline described in project brief
section 3: the ONE place where an HTTP request turns into a command on
RabbitMQ.

Part 4 update: this view no longer mints a fresh correlation_id per
call (that was an explicit placeholder in part 3, called out in the
README as something that would become a one-line change once
Conversation existed). It now:

    1. Resolves the target Conversation through
       conversations/services.py - the same authorization chokepoint
       every other endpoint in this project uses, so "can this user
       post into this conversation" is answered by code, not trust.
    2. Persists the user's message SYNCHRONOUSLY, in this request, so
       it exists in the system of record even if the AI worker never
       gets to run or no one is listening on SSE.
    3. Publishes the command using `conversation.correlation_id` -
       STABLE for the whole conversation - instead of a fresh id per
       message. This is what lets one id be followed through every
       turn of one conversation during the defense, not just one.

This view still does almost nothing else: it does NOT call the LLM,
does NOT wait for a reply, and does NOT block on anything the AI
worker will do.
"""

from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from conversations import services as conversation_services
from conversations.models import MessageRole
from messaging.envelope import MessageType
from messaging.publisher import publish_command


class SendMessageView(APIView):
    """POST /api/support/messages/  body: {"conversation_id": "...", "text": "..."}"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        conversation_id = request.data.get("conversation_id")
        text = request.data.get("text", "").strip()

        if not conversation_id:
            return Response(
                {"detail": "conversation_id is required."}, status=status.HTTP_400_BAD_REQUEST
            )
        if not text:
            return Response(
                {"detail": "text is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        # Ownership check: a customer may only post into a conversation
        # attached to one of their own orders; a support agent may post
        # into any. Raises the same "404, not 403" NotFound as every
        # other endpoint touching customer data in this project.
        try:
            conversation = conversation_services.get_conversation_for_user(request.user, conversation_id)
        except ObjectDoesNotExist as exc:
            raise NotFound("Conversation not found.") from exc

        # Persist the user's message before publishing anything. If the
        # publish step below fails, or the worker never picks it up,
        # the customer's message is still safely recorded - unlike the
        # RabbitMQ command, which only a successful worker run turns
        # into a visible reply.
        user_message = conversation_services.add_message(conversation, role=MessageRole.USER, content=text)

        # `conversation.correlation_id` is minted once, when the
        # Conversation row is first created (see conversations/models.py),
        # and reused for every message in it from here on - the
        # one-line change part 3's README predicted.
        envelope = publish_command(
            message_type=MessageType.PROCESS_USER_MESSAGE,
            payload={
                # acting_user id is resolved from the verified JWT here,
                # in Django - never something the AI worker is asked to
                # trust from anywhere else. The worker's tools use this
                # id the same way orders/services.py uses request.user
                # today: as the sole basis for filtering, never as a
                # hint an LLM could talk its way around.
                "acting_user_id": request.user.id,
                "conversation_id": str(conversation.id),
                "order_id": str(conversation.order_id),
                "user_message_id": str(user_message.id),
                "text": text,
            },
            correlation_id=str(conversation.correlation_id),
        )

        return Response(
            {
                "conversation_id": str(conversation.id),
                "correlation_id": envelope.correlation_id,
                "message_id": envelope.message_id,
                "user_message_id": str(user_message.id),
                "status": "queued",
            },
            status=status.HTTP_202_ACCEPTED,
        )
