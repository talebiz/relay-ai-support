"""
Tool 1 of 4: structured, internal data - the order/refund database.

Project brief, section 4 ("سه نوع دانش"): "داده ساختاریافته داخلی:
پایگاه داده، با رعایت مجوز دسترسی. «وضعیت سفارش من چیست؟»" - structured
internal data (the database), respecting access permissions. "What is
the status of my order?"

Authorization, once more, in code, not in the prompt
------------------------------------------------------
This is the single most important property this file has to uphold,
and it is worth restating exactly why (see also
backend/orders/services.py's own docstring, which this file mirrors
rule-for-rule): `acting_user_id` never comes from anything the LLM
wrote. It is read straight off the command envelope's payload
(`envelope.payload["acting_user_id"]`), which Django's
support/views.py populated from `request.user.id` - a value DRF
resolved from a cryptographically verified JWT, long before this
command was ever published to RabbitMQ or this tool was ever called.

The tool function signature below takes `acting_user_id` as an
explicit parameter that the AGENT LOOP supplies from that envelope
(see graph/agent_graph.py's `build_tools_for_run`), NOT a field the
model fills in when it decides to call the tool (see
tools/schemas.py's docstring: none of the Pydantic input schemas carry
an identity field at all). Concretely: the LLM can say "call
get_order_info with order_id=X"; it can never say "...as user 42" and
have that be believed, because there is no argument slot for it to say
that into, and even if there were, db.py's fetch_order_for_user
ignores anything but the acting_user_id closed over here.

Why one tool, not two ("get order" + "get refund history")
------------------------------------------------------------
A customer asking about their order almost always wants both pieces of
context together (status AND whether they already have a refund
request open) - splitting this into two round trips would cost an
extra full agent-loop iteration (extra LLM call, extra tokens, extra
latency) for information that is one WHERE clause away in the same
row's neighborhood. The project brief scores "depth over agent/tool
count" (section 4: "یک عامل خوش‌ساخت بهتر از پنج عامل نمایشی است") -
the same reasoning applies to over-splitting tools. This is still ONE
category of knowledge (the internal database), so it stays one tool.
"""

from __future__ import annotations

import json

from langchain_core.tools import StructuredTool

import db
from tools.schemas import GetOrderInfoInput


def _format_order_result(order: dict, refunds: list[dict]) -> str:
    """
    Renders the order + its refund history as a compact JSON string -
    good enough for the LLM to read and cite specifics from (status,
    tracking number, refund status) without us hand-writing English
    prose the model would just have to re-parse anyway.
    """
    payload = {
        "order_id": str(order["id"]),
        "customer_username": order["username"],
        "product_name": order["product_name"],
        "status": order["status"],
        "tracking_number": order["tracking_number"] or None,
        "total_cents": order["total_cents"],
        "created_at": order["created_at"].isoformat(),
        "refund_requests": [
            {
                "id": str(refund["id"]),
                "status": refund["status"],
                "reason": refund["reason"],
                "resolution_note": refund["resolution_note"] or None,
                "created_at": refund["created_at"].isoformat(),
            }
            for refund in refunds
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def make_get_order_info_tool(acting_user_id: int) -> StructuredTool:
    """
    Builds the `get_order_info` tool bound to ONE specific
    acting_user_id, for ONE specific agent run. A fresh tool instance
    is built per command (see graph/agent_graph.py) rather than a
    single shared tool that reads acting_user_id from some external
    state, so there is no way for one conversation's tool calls to
    accidentally close over another conversation's identity.
    """

    def _get_order_info(order_id: str) -> str:
        order = db.fetch_order_for_user(acting_user_id, order_id)
        if order is None:
            # Deliberately the SAME message whether the order does not
            # exist at all or exists but belongs to someone else - see
            # db.py::fetch_order_for_user's docstring for why that
            # distinction must not leak through a tool result either.
            return json.dumps(
                {
                    "error": "not_found",
                    "message": "No order with that id was found for this customer.",
                },
                ensure_ascii=False,
            )

        refunds = db.fetch_refund_history_for_order(acting_user_id, order_id) or []
        return _format_order_result(order, refunds)

    return StructuredTool.from_function(
        func=_get_order_info,
        name="get_order_info",
        description=(
            "Look up one order belonging to the current customer by its "
            "id: status, tracking number, total price, and any refund "
            "requests already filed against it. Use this whenever the "
            "customer asks about the status, contents, or refund "
            "history of a SPECIFIC order they refer to by id. This "
            "tool can only ever see the current customer's own orders "
            "(or, for a support agent, any customer's orders) - it "
            "will report 'not_found' for any other order id, and "
            "cannot be used to browse or list orders in bulk."
        ),
        args_schema=GetOrderInfoInput,
    )
