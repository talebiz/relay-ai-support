"""
Views for the conversations domain.

Every view resolves data exclusively through conversations/services.py,
passing `request.user` - never anything read from the request body -
exactly like orders/views.py.
"""

from django.core.exceptions import ObjectDoesNotExist
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from conversations import services
from conversations.serializers import ConversationSerializer, MessageSerializer


class OrderConversationView(APIView):
    """
    GET/POST /api/orders/<uuid:order_id>/conversation/

    Fetch-or-create the one conversation belonging to this order.
    GET and POST are deliberately identical (idempotent "give me the
    conversation for this order") - POST exists mainly so the frontend
    has an explicit "open chat" action without worrying about ever
    creating a duplicate conversation for the same order.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        return self._fetch_or_create(request, order_id)

    def post(self, request, order_id):
        return self._fetch_or_create(request, order_id)

    def _fetch_or_create(self, request, order_id):
        try:
            conversation = services.get_or_create_conversation_for_order(request.user, order_id)
        except ObjectDoesNotExist as exc:
            raise NotFound("Order not found.") from exc
        return Response(ConversationSerializer(conversation).data)


class ConversationMessagesView(APIView):
    """
    GET /api/conversations/<uuid:conversation_id>/messages/

    Full message history for one conversation - this is the resync
    endpoint the frontend calls after an SSE reconnect (project brief
    section 6: the live stream has no memory of its own; the database
    does).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        try:
            messages = services.list_messages(request.user, conversation_id)
        except ObjectDoesNotExist as exc:
            raise NotFound("Conversation not found.") from exc
        return Response(MessageSerializer(messages, many=True).data)
