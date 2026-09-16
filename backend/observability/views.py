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

from accounts.models import Role
from observability import services
from observability.serializers import AgentRunSerializer


class ConversationUsageView(APIView):
    """
    GET /api/conversations/<uuid:conversation_id>/usage/

    Role-based response shape (server-side RBAC):
      - support_agent: full payload — totals + every run with its spans,
        correlation ids, model names, per-span durations and costs.
        This is the operator view the brief's section 7 requires.
      - customer:      only the three headline fields their own UI
        renders (run_count, total_tokens, total_cost_usd). Everything
        else is redacted BEFORE it leaves the server — a customer
        opening DevTools sees the same JSON their screen shows, not
        internal tool names or per-span traces.

    The frontend hides the expand caret for customers separately; this
    endpoint is the second half of the same RBAC boundary, so a client
    that bypassed the UI still gets nothing more.
    """

    permission_classes = [IsAuthenticated]

    # Fields kept in `totals` for customers. Everything omitted here is
    # explicitly zeroed below rather than dropped, so the JSON shape the
    # frontend types describe stays intact regardless of role.
    _CUSTOMER_TOTALS_FIELDS = frozenset({"run_count", "total_tokens", "total_cost_usd"})

    def get(self, request, conversation_id):
        try:
            conversation, runs = services.runs_for_conversation(request.user, conversation_id)
        except ObjectDoesNotExist as exc:
            raise NotFound("Conversation not found.") from exc

        totals = services.summarize(runs)
        payload = {
            "conversation_id": str(conversation.id),
            "correlation_id": str(conversation.correlation_id),
        }

        if request.user.role == Role.SUPPORT_AGENT:
            payload["totals"] = totals
            payload["runs"] = AgentRunSerializer(runs, many=True).data
        else:
            payload["totals"] = self._redact_totals(totals)
            payload["runs"] = []

        return Response(payload)

    @classmethod
    def _redact_totals(cls, totals: dict) -> dict:
        """
        Keep the three headline fields the customer UI renders; zero
        every numeric field it does not, and blank out `token_source`.
        Same key set as the full totals so the frontend's
        `UsageTotals` type is still satisfied.
        """
        redacted = {}
        for key, value in totals.items():
            if key in cls._CUSTOMER_TOTALS_FIELDS:
                redacted[key] = value
            elif key == "token_source":
                redacted[key] = "none"
            elif isinstance(value, str):
                redacted[key] = "0"
            else:
                redacted[key] = 0
        return redacted
