"""
Document ingestion pipeline - Part 5.

Project brief, section 5: "از بارگذاری سند تا ابزار بازیابی، با
ذخیره‌سازی برداری در ChromaDB. پاسخ باید ارجاع به منبع را حفظ کند"
- from document upload to the retrieval tool, with vector storage in
ChromaDB. The answer must preserve a source reference.

This module handles the INGESTION side: given a document's raw text
and metadata, chunk it, embed the chunks via ChromaDB's built-in
default embedding function, and store them so the ai-worker's
search_knowledge_base tool can retrieve them later.

Chunking strategy
-----------------
A simple, fixed-size character window with overlap. Not the most
sophisticated approach (no sentence-boundary detection, no semantic
splitting), but honest, explainable line by line in the defense, and
good enough for the short policy documents this project's domain
involves. The chunk size (800 chars) and overlap (100 chars) are
chosen so that:
  - Each chunk fits well within the context window of even the
    smallest embedding models ChromaDB uses by default.
  - Overlap ensures a sentence that straddles a boundary is not lost
    entirely from either chunk.
  - The parameters are constants at the top of this file, not buried
    in a config object, so they are immediately visible and tunable.

Source metadata on every chunk
------------------------------
Every chunk stored in ChromaDB carries metadata:
  - document_id: the UUID of the Document row in Postgres
  - document_title: the human-readable title (shown in citations)
  - chunk_index: position within the document (for ordering)
  - source: always "internal_document" - the deliberate marker that
    lets the frontend distinguish an internal-document citation from
    a web citation (project brief section 5: "ارجاع به سند داخلی
    نباید با ارجاع به صفحه وب یکسان به نظر برسد").
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import chromadb

if TYPE_CHECKING:
    from documents.models import Document

logger = logging.getLogger(__name__)

CHROMADB_HOST = os.getenv("CHROMADB_HOST", "chromadb")
CHROMADB_PORT = int(os.getenv("CHROMADB_PORT", "8000"))

COLLECTION_NAME = "company_documents"

# Chunking parameters - see module docstring for rationale.
CHUNK_SIZE = 800      # characters per chunk
CHUNK_OVERLAP = 100   # overlap between consecutive chunks

_client: chromadb.HttpClient | None = None


def _get_client() -> chromadb.HttpClient:
    global _client
    if _client is None:
        _client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
    return _client


def get_collection():
    """
    Get or create the shared ChromaDB collection. Both this Django
    process (for ingestion) and the ai-worker process (for retrieval)
    connect to the same ChromaDB server and use the same collection
    name - this is what makes documents uploaded here immediately
    searchable by the agent, without any synchronization mechanism
    beyond ChromaDB itself being the shared store.
    """
    client = _get_client()
    return client.get_or_create_collection(name=COLLECTION_NAME)


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split `text` into overlapping chunks of roughly `chunk_size`
    characters. Returns an empty list if the text is empty or
    whitespace-only.

    This is intentionally the simplest correct chunking: a sliding
    window. The course (section "Building the chunk_text Function")
    builds a similar function; this one differs only in being a pure
    function with no side effects, no file I/O, and explicit
    parameters instead of global config - easier to test and to
    explain in the defense.
    """
    text = text.strip()
    if not text:
        return []

    # If the whole document fits in one chunk, don't split at all.
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        # Advance by (chunk_size - overlap) so consecutive chunks
        # share `overlap` characters at their boundary.
        start += chunk_size - overlap

    return chunks


def index_document(document: "Document", text_content: str) -> int:
    """
    Chunk `text_content`, embed the chunks via ChromaDB's default
    embedding function, and store them in the shared collection with
    full source metadata. Returns the number of chunks stored.

    Called synchronously from the upload view (documents/views.py)
    after the file has been read. For this project's scale (a handful
    of short policy documents), synchronous indexing is fine; a
    production system would push this into a background task or the
    existing RabbitMQ worker.

    If anything goes wrong, the caller (views.py) catches the
    exception and marks the Document as FAILED with the error message.
    """
    chunks = chunk_text(text_content)
    if not chunks:
        return 0

    collection = get_collection()
    doc_id = str(document.id)

    # Build the three parallel lists ChromaDB.add() expects:
    ids: list[str] = []
    documents_list: list[str] = []
    metadatas: list[dict] = []

    for index, chunk in enumerate(chunks):
        chunk_id = f"{doc_id}__chunk_{index}"
        ids.append(chunk_id)
        documents_list.append(chunk)
        metadatas.append({
            "document_id": doc_id,
            "document_title": document.title,
            "chunk_index": index,
            "source": "internal_document",
        })

    collection.add(
        ids=ids,
        documents=documents_list,
        metadatas=metadatas,
    )

    logger.info(
        "Indexed document %s (%s) — %d chunks stored in ChromaDB.",
        doc_id, document.title, len(chunks),
    )
    return len(chunks)


def delete_document_chunks(document_id: str) -> None:
    """
    Remove all chunks belonging to one document from ChromaDB. Called
    when a document is deleted through the API, so stale passages
    don't keep showing up in search results.
    """
    collection = get_collection()
    # ChromaDB's where filter on metadata lets us target exactly the
    # chunks for this document without knowing how many there are.
    try:
        collection.delete(where={"document_id": document_id})
    except Exception:
        # If the collection doesn't exist yet or has no matching docs,
        # that's fine - there's nothing to delete.
        logger.warning(
            "Could not delete chunks for document %s (collection may be empty).",
            document_id,
        )


def seed_default_documents() -> list[dict]:
    """
    Seed the three built-in company policy documents if the collection
    is empty. Returns a list of dicts describing what was seeded (for
    the management command's output).

    This replaces the hard-coded _SEED_DOCUMENTS that lived in
    ai-worker/tools/knowledge_base_tools.py in Part 4. The documents
    now live in ChromaDB via the same ingestion path as user-uploaded
    documents, with the same metadata shape, so the retrieval tool
    treats them identically.
    """
    collection = get_collection()
    if collection.count() > 0:
        return []

    default_docs = [
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

    ids = []
    documents_list = []
    metadatas = []
    seeded = []

    for doc in default_docs:
        chunks = chunk_text(doc["text"])
        for i, chunk in enumerate(chunks):
            chunk_id = f"{doc['id']}__chunk_{i}"
            ids.append(chunk_id)
            documents_list.append(chunk)
            metadatas.append({
                "document_id": doc["id"],
                "document_title": doc["title"],
                "chunk_index": i,
                "source": "internal_document",
            })
        seeded.append({"id": doc["id"], "title": doc["title"], "chunks": len(chunks)})

    if ids:
        collection.add(ids=ids, documents=documents_list, metadatas=metadatas)

    return seeded
