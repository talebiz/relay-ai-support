"""
Read-only HTTP surface for section 7.

One endpoint, authorized through observability/services.py (which in
turn goes through conversations/services.py - never a bespoke check
written only for this app):

    GET /api/conversations/<uuid>/usage/
        Everything the UI needs to answer "what has this conversation
        cost, and what exactly did the agent do?" in one round trip:
        the rolled-up totals plus every run with its spans.

One, not two: a deployment-wide totals endpoint used to live here and
was removed. The brief asks for the cost of each CONVERSATION
("هزینه هر گفت‌وگو را در رابط کاربری نشان دهید"), nothing in the UI
ever called the global one, and an endpoint with no caller is an
endpoint whose authorization nobody is checking.

There is deliberately no WRITE endpoint here. Runs are written by the
AI worker directly (ai-worker/db.py::insert_agent_run), inside the same
process that actually spent the tokens - an HTTP "please record this
cost" endpoint would be one more thing that can fail silently between
the spend and the ledger.
"""

from django.core.exceptions import ObjectDoesNotExist
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from observability import services
from observability.serializers import AgentRunSerializer


class ConversationUsageView(APIView):
    """GET /api/conversations/<uuid:conversation_id>/usage/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        try:
            conversation, runs = services.runs_for_conversation(request.user, conversation_id)
        except ObjectDoesNotExist as exc:
            raise NotFound("Conversation not found.") from exc

        return Response(
            {
                "conversation_id": str(conversation.id),
                "correlation_id": str(conversation.correlation_id),
                "totals": services.summarize(runs),
                "runs": AgentRunSerializer(runs, many=True).data,
            }
        )
