"""
Tool 3 of 4: external, public data - the web, via Tavily.

Project brief, section 4 ("سه نوع دانش"): "داده بیرونی: وب عمومی، از
طریق Tavily. «اعتصاب پیک این هفته بر ارسال اثر دارد؟»" - external data
(the public web), via Tavily. "Will this week's courier strike affect
shipping?" - the canonical example of a question the internal database
and internal documents genuinely cannot answer.

This is the tool the project brief singles out as the one that turns
"routing" into a real decision rather than a formality (section 4:
"با یک منبع، مسیریابی کار خاصی ندارد؛ با سه منبع، تصمیم واقعی می‌گیرد"
- with one source, routing does nothing special; with three sources,
it becomes a real decision) and names as one of the two mistakes worse
than a wrong answer (section 4: "جست‌وجو در وب برای چیزی که در پایگاه
داده بوده" - searching the web for something that was in the
database). The system prompt (graph/agent_graph.py) is explicit that
this tool is for information the company's OWN data cannot answer -
current events, general knowledge, other companies - never as a first
resort for anything about the customer's own order or the company's
own policies.

Every result is tagged `"source": "web"` with its originating URL, the
deliberate opposite number of `"source": "internal_document"` in
knowledge_base_tools.py, so the two can never be visually confused in
whatever the frontend renders as a citation (project brief 5).
"""

from __future__ import annotations

import json
import os

import httpx
from langchain_core.tools import StructuredTool

from tools.schemas import WebSearchInput

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def make_web_search_tool() -> StructuredTool:
    """
    No per-run identity closure needed here either - the public web is
    not user-scoped. A missing TAVILY_API_KEY degrades to a clear
    error result (not a crash) so a demo/defense environment without a
    key configured still shows correct ROUTING behavior even if the
    call itself cannot complete.
    """

    def _web_search(query: str) -> str:
        if not TAVILY_API_KEY:
            return json.dumps(
                {
                    "source": "web",
                    "error": "web_search_unavailable",
                    "message": "TAVILY_API_KEY is not configured for this deployment.",
                },
                ensure_ascii=False,
            )

        try:
            response = httpx.post(
                TAVILY_SEARCH_URL,
                json={
                    "api_key": TAVILY_API_KEY,
                    "query": query,
                    "max_results": 3,
                    "search_depth": "basic",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            return json.dumps(
                {"source": "web", "error": "web_search_failed", "message": str(exc)},
                ensure_ascii=False,
            )

        matches = [
            {
                "source": "web",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "excerpt": item.get("content", ""),
            }
            for item in data.get("results", [])
        ]

        if not matches:
            return json.dumps(
                {"source": "web", "matches": [], "message": "No web results found."},
                ensure_ascii=False,
            )

        return json.dumps({"matches": matches}, ensure_ascii=False)

    return StructuredTool.from_function(
        func=_web_search,
        name="web_search",
        description=(
            "Search the public web via Tavily for information that is "
            "NOT about this company's own orders or internal policies "
            "- general knowledge, current events, other companies, or "
            "anything requiring up-to-date public information. Do NOT "
            "use this for questions get_order_info or "
            "search_knowledge_base could answer; searching the web for "
            "something already in the company's own data is a mistake, "
            "not a safe fallback. Every result includes its source URL "
            "for citation."
        ),
        args_schema=WebSearchInput,
    )
