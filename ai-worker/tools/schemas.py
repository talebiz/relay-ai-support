"""
Pydantic input schemas for every tool the agent can call.

Project brief, section 4 ("مرزبندی ابزارها"): "ورودی هر ابزار
اعتبارسنجی شود، مدل نتواند پرس‌وجوی دلخواه بسازد" - every tool's input
must be validated, and the model must not be able to construct an
arbitrary query.

This is why every tool below takes a narrow, typed Pydantic model
instead of a single free-text "query" string it could stuff anything
into. Concretely:

  - GetOrderInfoInput only accepts an order_id (a UUID string). The
    model cannot ask for "all orders" or pass a filter expression -
    it can only name ONE order, and even then only its own (enforced
    in tools/order_tools.py via acting_user_id, not by anything here -
    this schema only checks SHAPE, never AUTHORIZATION).
  - CreateRefundRequestInput requires both order_id and a bounded
    reason string - the model cannot omit the reason or attach extra
    unvalidated fields the tool would blindly use.
  - SearchKnowledgeBaseInput and WebSearchInput take a `query` string
    for the two tools where free text genuinely is the right shape (a
    search query IS a string) - the validation there is about type
    and length, not about withholding the input, since a semantic
    search tool has no useful narrower schema than "the search text".

None of these schemas embed acting_user_id or any authorization data -
that is deliberate. The identity used to authorize a tool call is
always the acting_user_id carried on the message envelope (resolved by
Django from the verified JWT before the command was ever published,
see backend/support/views.py), never anything the LLM writes into a
tool call argument. See tools/order_tools.py and tools/action_tools.py
for where that identity is actually applied.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GetOrderInfoInput(BaseModel):
    """Look up one order (and, if it has any, its refund history) by id."""

    order_id: str = Field(
        description="The UUID of the order to look up, exactly as it "
        "appears in the conversation context or as the customer wrote "
        "it. Do not guess or invent an order_id."
    )


class SearchKnowledgeBaseInput(BaseModel):
    """Semantic search over the company's internal documents (policies, FAQs)."""

    query: str = Field(
        min_length=3,
        max_length=500,
        description="A short natural-language question or phrase to "
        "search the company's internal documents for, e.g. 'return "
        "policy for opened items' or 'how long does shipping take'.",
    )


class WebSearchInput(BaseModel):
    """Search the public web for information outside the company's own data."""

    query: str = Field(
        min_length=3,
        max_length=500,
        description="A short web search query for information that is "
        "NOT about this company's own orders or policies - general "
        "public knowledge, current events, other companies, etc.",
    )


class CreateRefundRequestInput(BaseModel):
    """File a refund request against one of the customer's own orders."""

    order_id: str = Field(
        description="The UUID of the order this refund request is for."
    )
    reason: str = Field(
        min_length=5,
        max_length=2000,
        description="A clear explanation of why the customer is "
        "requesting a refund, in their own words or summarized from "
        "the conversation.",
    )
