"""
Tool 2 of 4: unstructured, internal data - company documents via retrieval.

Project brief, section 4 ("سه نوع دانش"): "داده غیرساختاریافته داخلی:
اسناد بارگذاری شده، از طریق بازیابی. «سیاست بازگشت کالا چیست؟»" -
unstructured internal data (uploaded documents), via retrieval. "What
is the return policy?"

Scope of this commit vs. Part 5
--------------------------------
The project brief dedicates its OWN section (5) to RAG - "از بارگذاری
سند تا ابزار بازیابی، با ذخیره‌سازی برداری در ChromaDB. پاسخ باید
ارجاع به منبع را حفظ کند" (from document ingestion to the retrieval
tool, with vector storage in ChromaDB; the answer must preserve a
source reference) - clearly a separate, larger piece of work than "one
of four tools the graph routes to." So this commit deliberately keeps
this file MINIMAL:

  - A tiny, hard-coded set of company documents (return policy,
    shipping policy, warranty policy) is embedded into a ChromaDB
    collection lazily, once, the first time the worker process needs
    it - not a real document-ingestion pipeline (chunking strategy,
    file upload endpoint, re-indexing on change). That is Part 5's job.
  - The tool itself, however, IS real: it does a genuine embedding
    similarity search against a genuine persistent ChromaDB collection
    and returns genuine retrieved passages with a source label - not a
    hard-coded if/else keyword match. This is what the project brief
    means by "a graph that already routes correctly among all four
    tools" being commit 2's job, with the ingestion pipeline itself
    slotting in later without changing the tool's calling contract.
  - Every result carries `"source": "internal_document"` plus the
    originating document's title, so a citation built from this tool's
    output can never be visually confused with a web citation (project
    brief 5: "ارجاع به سند داخلی نباید با ارجاع به صفحه وب یکسان به نظر
    برسد" - an internal-document citation must not look the same as a
    web-page citation). See web_search_tools.py for the matching
    "source": "web" marker on the other side of that requirement.

Why ChromaDB's HTTP client here, not an embedded/local instance: this
worker and the (future) Django ingestion endpoint are two separate
processes that must see the SAME collection - ChromaDB's own server
mode (already in docker-compose.yml as the `chromadb` service, per
project brief section 1's required six services) is what makes that
possible, exactly like PostgreSQL being a shared resource both
services connect to directly (see this module's sibling, db.py).
"""

from __future__ import annotations

import json
import os

import chromadb
from langchain_core.tools import StructuredTool

from tools.schemas import SearchKnowledgeBaseInput

CHROMADB_HOST = os.getenv("CHROMADB_HOST", "chromadb")
CHROMADB_PORT = int(os.getenv("CHROMADB_PORT", "8000"))

COLLECTION_NAME = "company_documents"

# Placeholder seed corpus for this commit. Each entry becomes exactly
# one ChromaDB document (no chunking - these are already short). Part
# 5 replaces this with a real ingestion pipeline that chunks larger,
# user-uploaded files; the tool below does not need to change when
# that happens, only this seed step goes away.
_SEED_DOCUMENTS = [
    {
        "id": "policy-returns",
        "title": "Return Policy",
        "text": (
            "Customers may return most items within 30 days of delivery "
            "for a full refund, provided the item is unused and in its "
            "original packaging. Opened electronics (headphones, "
            "earbuds) can only be returned if defective. Refunds are "
            "issued to the original payment method within 5-7 business "
            "days after the returned item is received and inspected."
        ),
    },
    {
        "id": "policy-shipping",
        "title": "Shipping Policy",
        "text": (
            "Standard shipping takes 3-5 business days within the "
            "country and is free on orders over $50. Expedited shipping "
            "(1-2 business days) is available at checkout for an "
            "additional fee. Once an order ships, a tracking number is "
            "attached to the order and visible on the order detail page."
        ),
    },
    {
        "id": "policy-warranty",
        "title": "Warranty Policy",
        "text": (
            "All electronics sold come with a 1-year manufacturer "
            "warranty covering defects in materials and workmanship. "
            "The warranty does not cover accidental damage, water "
            "damage, or normal wear and tear. To file a warranty claim, "
            "contact support with the order id and a description of the "
            "defect; approved claims are resolved as a repair, "
            "replacement, or refund at the company's discretion."
        ),
    },
]

_client: chromadb.HttpClient | None = None


def _get_client() -> chromadb.HttpClient:
    global _client
    if _client is None:
        _client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
    return _client


def _get_or_create_collection():
    """
    Fetches the shared collection, seeding it with the placeholder
    documents above IF (and only if) it is currently empty. Checking
    `count() == 0` rather than always re-adding by a fixed id set
    means a real ingestion pipeline (Part 5) can add more documents to
    this same collection later without this seeding step ever
    clobbering them.
    """
    client = _get_client()
    collection = client.get_or_create_collection(name=COLLECTION_NAME)

    if collection.count() == 0:
        collection.add(
            ids=[doc["id"] for doc in _SEED_DOCUMENTS],
            documents=[doc["text"] for doc in _SEED_DOCUMENTS],
            metadatas=[{"title": doc["title"]} for doc in _SEED_DOCUMENTS],
        )

    return collection


def make_search_knowledge_base_tool() -> StructuredTool:
    """
    Unlike order_tools/action_tools, this tool needs no per-run
    identity closure - company documents are not user-scoped, so a
    single tool instance is safe to reuse across every conversation.
    """

    def _search_knowledge_base(query: str) -> str:
        collection = _get_or_create_collection()
        results = collection.query(query_texts=[query], n_results=2)

        documents = results.get("documents") or [[]]
        metadatas = results.get("metadatas") or [[]]
        distances = results.get("distances") or [[]]

        matches = []
        for text, meta, distance in zip(documents[0], metadatas[0], distances[0]):
            matches.append(
                {
                    "source": "internal_document",
                    "title": meta.get("title", "Untitled document"),
                    "excerpt": text,
                    "relevance_distance": round(float(distance), 4),
                }
            )

        if not matches:
            return json.dumps(
                {"source": "internal_document", "matches": [], "message": "No matching internal documents found."},
                ensure_ascii=False,
            )

        return json.dumps({"matches": matches}, ensure_ascii=False)

    return StructuredTool.from_function(
        func=_search_knowledge_base,
        name="search_knowledge_base",
        description=(
            "Search the company's own internal documents (return "
            "policy, shipping policy, warranty policy, and other "
            "company-authored material) for an answer. Use this for "
            "any question about THIS company's policies or procedures "
            "- not for the status of a specific order (use "
            "get_order_info) and not for anything outside the company "
            "(use web_search). Every result is a real internal-document "
            "excerpt with its source title, suitable for citing back "
            "to the customer."
        ),
        args_schema=SearchKnowledgeBaseInput,
    )
