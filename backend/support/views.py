"""
Entry point of the event-driven pipeline described in project brief
section 3: the ONE place where an HTTP request turns into a command on
RabbitMQ.

This view intentionally does almost nothing:
    1. Validate the input.
    2. Mint (or accept) a correlation_id.
    3. Publish a PROCESS_USER_MESSAGE command.
    4. Return immediately with the correlation_id.

It does NOT call the LLM, does NOT wait for a reply, and does NOT block
on anything the AI worker will do - that is precisely the difference
from the reference course, where the agent runs inside this same
request/response cycle (project brief, section 3 intro paragraph).
The frontend uses the returned correlation_id to open the SSE stream
(GET /api/stream/<correlation_id>/, see streaming/views.py) and watches
that connection for the reply as it is generated.

Conversation/Message persistence is intentionally NOT implemented yet
in this pass - this view only proves the publish path end-to-end. The
AI worker on the other side is likewise a placeholder that acknowledges
the command without yet running a real agent (see ai-worker/worker.py).
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from messaging.envelope import MessageType, new_correlation_id
from messaging.publisher import publish_command


class SendMessageView(APIView):
    """POST /api/support/messages/"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        text = request.data.get("text", "").strip()
        if not text:
            return Response(
                {"detail": "text is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        # A real implementation would look up an existing Conversation
        # by id from the request and reuse its correlation_id for every
        # message in that conversation; since Conversation/Message
        # models don't exist yet in this pass, we mint a fresh
        # correlation_id per call. Swapping this for
        # "conversation.correlation_id" later is a one-line change,
        # not a redesign - the correlation_id is already threaded
        # through the envelope, the worker, and the SSE view.
        correlation_id = new_correlation_id()

        envelope = publish_command(
            message_type=MessageType.PROCESS_USER_MESSAGE,
            payload={
                # acting_user id is resolved from the verified JWT here,
                # in Django - never something the AI worker is asked to
                # trust from anywhere else. The worker's tools will use
                # this id the same way orders/services.py uses
                # request.user today: as the sole basis for filtering,
                # never as a hint an LLM could talk its way around.
                "acting_user_id": request.user.id,
                "text": text,
            },
            correlation_id=correlation_id,
        )

        return Response(
            {
                "correlation_id": envelope.correlation_id,
                "message_id": envelope.message_id,
                "status": "queued",
            },
            status=status.HTTP_202_ACCEPTED,
        )
