"""
Tool 4 of 4: the ACTION tool.

Project brief, section 2: "به‌علاوه دست کم یک ابزار که به‌جای خواندن،
کاری انجام دهد: ثبت بازگشت وجه، یا ایجاد تیکت." - at least one tool
that performs an action instead of reading: filing a refund, or
creating a ticket. This project's domain (mirroring the reference
course's storefront) makes "file a refund request" the natural choice.

Why this is worth calling out as categorically different from the
other three tools: get_order_info, search_knowledge_base, and
web_search can all be called speculatively and re-called with no
consequence beyond wasted tokens. create_refund_request has a
side-effect that OUTLIVES the conversation - once it runs, a real
RefundRequest row exists in the database and a human (or another
agent run later) will act on it. This is exactly the class of tool the
brief's "bonus" list flags for extra caution ("تأیید انسانی پیش از
اجرای ابزار مخرب" - human confirmation before running a destructive
tool). This project does not implement a human-confirmation step (that
bonus item is explicitly out of scope here, called out in the README),
but it DOES keep every other authorization guarantee identical to the
read tools: the order this refund is filed against is re-resolved
through the exact same ownership check as get_order_info
(db.py::fetch_order_for_user, called internally by
db.py::insert_refund_request) - a customer cannot file a refund
against an order that isn't theirs merely by asking the agent to.

Idempotency note: unlike the RabbitMQ command layer (which has a real
UNIQUE-constraint-backed idempotency guarantee - see
messaging/models.py), calling this tool twice in the same agent run
(e.g. because the model retries after a transient LLM hiccup) WILL
create two RefundRequest rows. This is a known, accepted limitation
for this project's size, not something silently swept under the rug:
a production system would give this tool its own idempotency key
(e.g. hash of order_id + conversation turn) the same way the command
queue does for redelivered commands.
"""

from __future__ import annotations

import json

from langchain_core.tools import StructuredTool

import db
from tools.schemas import CreateRefundRequestInput


def make_create_refund_request_tool(acting_user_id: int) -> StructuredTool:
    """
    Builds the `create_refund_request` tool bound to ONE acting_user_id,
    for the same reason order_tools.make_get_order_info_tool does: a
    fresh closure per agent run, never a shared tool reading identity
    from anywhere the LLM could influence.
    """

    def _create_refund_request(order_id: str, reason: str) -> str:
        refund = db.insert_refund_request(acting_user_id, order_id, reason)
        if refund is None:
            return json.dumps(
                {
                    "error": "not_found",
                    "message": "No order with that id was found for this customer; "
                    "the refund request was NOT created.",
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "status": "created",
                "refund_request_id": refund["id"],
                "order_id": refund["order_id"],
                "refund_status": refund["status"],
            },
            ensure_ascii=False,
        )

    return StructuredTool.from_function(
        func=_create_refund_request,
        name="create_refund_request",
        description=(
            "File a new refund request against one of the current "
            "customer's own orders. This is a WRITE action with a "
            "real, lasting effect - only call it when the customer has "
            "clearly asked for a refund (or explicitly agreed to file "
            "one) and you have both the order id and a reason. Do not "
            "call this speculatively or more than once for the same "
            "request. It cannot be used on an order that does not "
            "belong to the current customer."
        ),
        args_schema=CreateRefundRequestInput,
    )
