"""
Document model for RAG - Part 5.

Project brief, section 5: "از بارگذاری سند تا ابزار بازیابی، با
ذخیره‌سازی برداری در ChromaDB" - from document upload to the retrieval
tool, with vector storage in ChromaDB.

This model tracks METADATA about uploaded documents (title, original
filename, who uploaded it, when). The actual chunked text and its
embeddings live in ChromaDB (the vector store), not in PostgreSQL -
Postgres is the system of record for "which documents exist and who
uploaded them", ChromaDB is the system of record for "find me the
most relevant passages given a query".

Why a Django model at all, rather than just pushing files straight
into ChromaDB: the brief requires a document UPLOAD flow (an endpoint
the frontend calls), and documents need to be listable, deletable,
and attributable to the user who uploaded them. ChromaDB has no notion
of "users" or "permissions" - that is Django's job, same as with
orders and conversations.
"""

import uuid

from django.conf import settings
from django.db import models


class DocumentStatus(models.TextChoices):
    PENDING = "pending", "Pending"       # uploaded, not yet indexed
    INDEXED = "indexed", "Indexed"       # successfully chunked & stored in ChromaDB
    FAILED = "failed", "Failed"          # indexing failed


class Document(models.Model):
    """
    One uploaded document. Not user-scoped for retrieval (company
    documents are shared knowledge, visible to everyone's agent), but
    user-scoped for management (only a support agent or the uploader
    can delete it).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(
        max_length=300,
        help_text="Human-readable title shown in the documents page and in citations.",
    )
    original_filename = models.CharField(max_length=500)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_documents",
    )
    status = models.CharField(
        max_length=20,
        choices=DocumentStatus.choices,
        default=DocumentStatus.PENDING,
    )
    chunk_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of chunks stored in ChromaDB for this document.",
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.status})"
