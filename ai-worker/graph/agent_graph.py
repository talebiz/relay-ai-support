"""
The actual LangGraph agent - Part 4 commit 2.

Project brief, section 4 ("عامل هوشمند با LangGraph"): "یک گراف کوچک
که میان چهار ابزار مسیریابی می‌کند: پایگاه داده، بازیابی اسناد،
جست‌وجوی وب و یک ابزار اقدام. یک عامل خوش‌ساخت بهتر از پنج عامل
نمایشی است؛ تعداد عامل نمره ندارد، عمق دارد." - a small graph that
routes among four tools: the database, document retrieval, web search,
and one action tool. One well-built agent beats five decorative
agents; depth is scored, not agent count.

This replaces commit 1's `run_agent`, which made exactly one
tool-less LLM call per message. This file is what commit 1's own
worker.py docstring predicted: "part 4 commit 2, once the graph and
its four tools exist."

--- Why one agent (a single graph), not multiple cooperating agents ---

The project brief's "where not to follow the course" table (section 4)
explicitly scores this: "سه عامل همکار / یک گراف خوش‌ساخت / عمق نمره
دارد، تعداد عامل نه" - three cooperating agents vs. one well-built
graph; depth is scored, not agent count. The reference course builds a
support agent, a manager agent, and a risk agent as three separate LLM
loops. This project deliberately does NOT reproduce that split: one
agent, bound to all four tools, with a system prompt that tells it how
to choose between them, is a more honest match for what the brief asks
for than three thin agents with little real work to divide between
them at this project's scope. (The `risk`/`fraud` scenario from the
reference course simply is not part of this domain: Relay's brief is
about routing across three KNOWLEDGE SOURCES plus one action, not
detecting fraud.)

--- The graph itself ---

Two nodes, in a loop:

    agent_node: calls the LLM (with all four tools bound) on the
        current message list. If the LLM's response contains tool
        calls, we transition to tools_node; if not, the LLM's reply IS
        the final answer and the graph ends.

    tools_node: a prebuilt LangGraph `ToolNode` that executes every
        tool call the agent_node just requested and appends each
        tool's result as a ToolMessage, then loops back to agent_node
        so the LLM can read the tool results and decide what to do
        next (call another tool, or answer).

This is the textbook LangGraph "ReAct-style" loop, built from
LangGraph's own `StateGraph` primitives rather than the higher-level
`create_react_agent` helper, specifically so the recursion limit and
the tool-binding-per-run (see `build_agent_for_run` below) are both
fully visible and explainable line-by-line in the defense, per the
project brief's requirement (section 1, "درباره دوره": "کدی که نتوانید
توضیح دهید، بیشتر از کدی که ننوشته‌اید به شما آسیب می‌زند" - code you
cannot explain hurts you more than code you never wrote).

--- Tool boundaries (project brief section 4) ---

"مرزبندی ابزارها: ورودی هر ابزار اعتبارسنجی شود، مدل نتواند پرس‌وجوی
دلخواه بسازد و حلقه گراف سقف تعداد گام داشته باشد تا اعتبار شما را در
یک درخواست نسوزاند." - tool boundaries: every tool's input must be
validated, the model must not be able to build an arbitrary query, and
the graph's loop must have a step ceiling so one request cannot burn
through your credit. All three are enforced here:

  1. Validated input: every tool is a `StructuredTool` built from a
     narrow Pydantic schema (tools/schemas.py) - LangChain rejects a
     tool call whose arguments do not match the schema before the
     tool function body ever runs.
  2. No arbitrary queries: see each tool module's own docstring for
     why its specific schema shape prevents this (order_tools.py in
     particular - only an order_id, never a raw SQL-shaped filter).
  3. Step ceiling: `AGENT_RECURSION_LIMIT` below, passed to
     `graph.invoke(..., config={"recursion_limit": ...})` in
     worker.py. LangGraph counts one "step" per node transition, so
     this bounds the total number of agent<->tool round trips in a
     single command, independent of how many tools exist.

--- Checkpointer: MemorySaver, not a Postgres checkpointer ---

`MemorySaver` (in-process, in-memory) is deliberately used instead of
LangGraph's Postgres checkpointer. This is a considered choice, not an
oversight:

  - The REAL system of record for conversation history is
    `conversations.Message` in PostgreSQL (see db.py's docstring and
    conversations/models.py) - written by Django synchronously for the
    user's turn and by this worker for the assistant's turn. That is
    what survives an SSE reconnect and what a fresh worker process
    reads to reconstruct the message list BEFORE ever invoking the
    graph (see `run_agent` in worker.py).
  - LangGraph's own checkpointer, by contrast, only needs to persist
    state for the DURATION OF ONE GRAPH RUN - the back-and-forth
    between agent_node and tools_node within a single command. That
    state is fully consumed by the time `graph.invoke()` returns (the
    graph's own `thread_id` is scoped to one command, minted fresh
    per run - see `build_agent_for_run`), so persisting it beyond the
    worker process's lifetime buys nothing here.
  - Using a heavier Postgres-backed checkpointer to persist state that
    is thrown away at the end of every single invocation anyway would
    add real operational complexity (another schema, another
    migration path) for no corresponding benefit at this project's
    scale. If a future version needed the graph to pause mid-run
    across worker restarts (e.g. a human-in-the-loop approval step -
    see the bonus list's "تأیید انسانی پیش از اجرای ابزار مخرب"), that
    would be the point to revisit this choice.
"""

from __future__ import annotations

import operator
from typing import Annotated, Sequence, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from llm.factory import bind_tools_with_fallback
from tools.action_tools import make_create_refund_request_tool
from tools.knowledge_base_tools import make_search_knowledge_base_tool
from tools.order_tools import make_get_order_info_tool
from tools.web_search_tools import make_web_search_tool

# Hard ceiling on agent<->tool round trips for a single command. Each
# full loop (agent_node -> tools_node -> agent_node) costs 2 steps, so
# 8 allows up to ~4 tool-calling round trips before the graph gives up
# and returns whatever the model has said so far - generous enough for
# "look up the order, then check the refund policy" chains, but bounded
# so one confused run cannot loop indefinitely and burn the LLM budget.
AGENT_RECURSION_LIMIT = 8

SUPPORT_SYSTEM_PROMPT = (
    "You are Maya, a customer support assistant for an online store. "
    "Answer clearly and concisely, in the same language the customer "
    "wrote in.\n\n"
    "You have exactly three sources of knowledge, each behind its own "
    "tool, and you must pick the right one instead of guessing:\n"
    "1. get_order_info - the company's OWN order database. Use this "
    "for anything about a SPECIFIC order: status, tracking, refund "
    "history. Only call it when you have (or the customer has given "
    "you) an order id from this conversation.\n"
    "2. search_knowledge_base - the company's OWN internal documents "
    "(return policy, shipping policy, warranty policy). Use this for "
    "questions about company policy or procedure in general, not tied "
    "to one order.\n"
    "3. web_search - the public web. Use this ONLY for information "
    "the company's own data cannot possibly contain: general "
    "knowledge, current events, other companies, anything external.\n\n"
    "You also have one ACTION tool, create_refund_request, which files "
    "a real refund request. Only call it when the customer has clearly "
    "asked for a refund and you have both the order id and a reason - "
    "never speculatively, and never more than once for the same "
    "request.\n\n"
    "Two mistakes are worse than an imperfect answer: (a) searching "
    "the web for something that was actually in the order database or "
    "the internal documents, and (b) answering from your own general "
    "knowledge when you should have called a tool instead - if the "
    "question is about this company, this store, or this customer's "
    "own data, use a tool rather than guessing. If none of your tools "
    "can answer something, say so plainly instead of making something "
    "up.\n\n"
    "When you use search_knowledge_base or web_search, mention what "
    "kind of source backed your answer (an internal company document, "
    "or a web result) so the customer knows where the information came "
    "from."
)


class AgentState(TypedDict):
    """
    The graph's per-run state: just the running message list. Kept
    intentionally minimal - no cross-run fields - because the graph's
    job is ONE command's worth of reasoning, not the whole
    conversation's memory (see this module's docstring on the
    checkpointer choice: full conversation history is reconstructed
    from the database BEFORE this state is even seeded, in worker.py).
    """

    messages: Annotated[Sequence[BaseMessage], operator.add]


def _build_tools_for_run(acting_user_id: int) -> list:
    """
    Builds a fresh list of tool instances for THIS run only.

    order_tools and action_tools are closures over `acting_user_id`
    (see their own docstrings for why: authorization must be bound at
    tool-construction time, never read from a model-supplied
    argument). knowledge_base_tools and web_search_tools carry no user
    identity at all, since neither the company's documents nor the
    public web are user-scoped - but they are still rebuilt here for
    symmetry and because a shared module-level tool cache would be an
    unnecessary optimization for this project's per-command volume.
    """
    return [
        make_get_order_info_tool(acting_user_id),
        make_search_knowledge_base_tool(),
        make_web_search_tool(),
        make_create_refund_request_tool(acting_user_id),
    ]


def build_agent_graph(models: list[BaseChatModel], acting_user_id: int):
    """
    Compiles one graph instance for one command, bound to one
    customer's authorization scope.

    Rebuilding (rather than caching) the graph per command is
    deliberate: it is the simplest way to guarantee the tools bound to
    the model can never leak from one acting_user_id to another - each
    command gets its own tool closures, built fresh from the envelope
    payload's acting_user_id (see worker.py's run_agent), with no
    shared mutable state between runs.

    `models` is a LIST (primary first), not a single model, because the
    fallback chain has to be built AFTER tools are bound - see
    llm/factory.py::bind_tools_with_fallback, which owns that rule. One
    element is the no-fallback case and behaves exactly as before.
    """
    tools = _build_tools_for_run(acting_user_id)
    model_with_tools = bind_tools_with_fallback(models, tools)

    def agent_node(state: AgentState) -> dict:
        response = model_with_tools.invoke(state["messages"])
        return {"messages": [response]}

    def should_continue(state: AgentState) -> str:
        """
        Routes to the tools node if the last AI message requested any
        tool calls, otherwise ends the graph - the model's reply is
        final. This is the entire "loop until done" condition; there
        is no other exit path besides this or the recursion limit.
        """
        last_message = state["messages"][-1]
        tool_calls = getattr(last_message, "tool_calls", None)
        return "tools" if tool_calls else END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(tools))

    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    # See module docstring ("Checkpointer") for why MemorySaver, not a
    # Postgres-backed checkpointer, is the right choice here: this
    # state only needs to live for the duration of this one
    # graph.invoke() call.
    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


def build_initial_messages(
    history: list[dict],
    current_user_text: str,
    order_context: dict | None = None,
) -> list[BaseMessage]:
    """
    Converts the plain dict rows from db.fetch_conversation_history
    into LangChain message objects, prefixed with the system prompt -
    the same shape worker.py's commit-1 `_history_to_messages` built,
    now living next to the graph that consumes it.

    `current_user_text` is passed separately rather than assumed to
    already be the last row of `history`, because worker.py's
    run_agent calls this BEFORE the user's row is guaranteed to be
    visible to a read replica / same transaction in every deployment;
    passing it explicitly removes that ambiguity. If the history
    already ends with this exact text (the common case), it is not
    duplicated.

    `order_context`, when provided, is appended to the system prompt
    so the agent already knows which order this conversation is about
    without the customer having to state their order id.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    system_text = SUPPORT_SYSTEM_PROMPT
    if order_context:
        system_text += (
            f"\n\n--- Current order context ---\n"
            f"This conversation is about order {order_context.get('id')}.\n"
            f"Customer: {order_context.get('username')}\n"
            f"Product: {order_context.get('product_name')}\n"
            f"Quantity: {order_context.get('quantity')}\n"
            f"Status: {order_context.get('status')}\n"
            f"Tracking: {order_context.get('tracking_number') or 'N/A'}\n"
            f"You already have the order id — use get_order_info with it "
            f"directly instead of asking the customer for it."
        )

    messages: list[BaseMessage] = [SystemMessage(content=system_text)]
    for entry in history:
        if entry["role"] == "user":
            messages.append(HumanMessage(content=entry["content"]))
        else:
            messages.append(AIMessage(content=entry["content"]))

    history_ends_with_current = bool(history) and history[-1]["role"] == "user" and history[-1]["content"] == current_user_text
    if not history_ends_with_current:
        messages.append(HumanMessage(content=current_user_text))

    return messages
