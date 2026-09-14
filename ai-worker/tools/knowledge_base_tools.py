"""
Tool 2 of 4: unstructured, internal data - company documents via retrieval.

Part 5 update: this tool now queries a ChromaDB collection that is
populated by Django's document ingestion pipeline
(backend/documents/services.py) rather than seeding its own hard-coded
placeholder documents. The tool itself is SIMPLER than in Part 4 -
it no longer needs to seed anything, because the ingestion pipeline
(and the seed_documents management command) handle that separately.

What changed from Part 4:
  - Removed _SEED_DOCUMENTS and the lazy-seed logic from
    _get_or_create_collection. The collection is now managed entirely
    by Django's documents app; this tool just reads from it.
  - Increased n_results from 2 to 3 for slightly better recall.
  - Each result now carries richer metadata (document_id,
    document_title, chunk_index) set by the ingestion pipeline,
    rather than just the "title" the Part 4 placeholder stored.

What did NOT change:
  - The tool's name, description, and schema
    (SearchKnowledgeBaseInput) are identical - the agent graph and the
    system prompt that reference "search_knowledge_base" are untouched.
  - Every result still carries `"source": "internal_document"` in its
    metadata, so the frontend can distinguish it from a web citation
    (project brief 5: "ارجاع به سند داخلی نباید با ارجاع به صفحه وب
    یکسان به نظر برسد").

ChromaDB connection: same HttpClient as Part 4, pointing at the same
`chromadb` service in docker-compose.yml. Both this worker and
Django's ingestion code connect to the same ChromaDB server and use
the same collection name ("company_documents") - that is what makes a
document uploaded through Django immediately searchable here, with no
synchronization mechanism beyond ChromaDB itself.
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

_client: chromadb.HttpClient | None = None


def _get_client() -> chromadb.HttpClient:
    global _client
    if _client is None:
        _client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
    return _client


def _get_collection():
    """
    Fetch the shared collection. Unlike Part 4, this no longer seeds
    anything - the collection is populated by Django's ingestion
    pipeline (documents/services.py) and the seed_documents management
    command. If the collection is empty, the tool simply returns
    "no matching documents found", which is the correct answer.
    """
    client = _get_client()
    return client.get_or_create_collection(name=COLLECTION_NAME)


def make_search_knowledge_base_tool() -> StructuredTool:
    """
    Build the search_knowledge_base tool. No per-user identity needed
    - company documents are shared knowledge, not user-scoped.
    """

    def _search_knowledge_base(query: str) -> str:
        collection = _get_collection()

        # If the collection is empty, return early with a clear message.
        if collection.count() == 0:
            return json.dumps(
                {
                    "source": "internal_document",
                    "matches": [],
                    "message": "No internal documents have been uploaded yet.",
                },
                ensure_ascii=False,
            )

        results = collection.query(query_texts=[query], n_results=3)

        documents = results.get("documents") or [[]]
        metadatas = results.get("metadatas") or [[]]
        distances = results.get("distances") or [[]]

        matches = []
        for text, meta, distance in zip(documents[0], metadatas[0], distances[0]):
            matches.append(
                {
                    "source": meta.get("source", "internal_document"),
                    "document_id": meta.get("document_id", "unknown"),
                    "title": meta.get("document_title", "Untitled document"),
                    "chunk_index": meta.get("chunk_index", 0),
                    "excerpt": text,
                    "relevance_distance": round(float(distance), 4),
                }
            )

        if not matches:
            return json.dumps(
                {
                    "source": "internal_document",
                    "matches": [],
                    "message": "No matching internal documents found.",
                },
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
