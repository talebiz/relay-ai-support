"""
Views for the documents app - Part 5.

Authorization:
  GET    /api/documents/        - any authenticated user (customers can
                                  see what documents exist, the agent
                                  uses them on their behalf)
  POST   /api/documents/        - support agents only (company documents
                                  are managed by support staff, not
                                  customers)
  DELETE /api/documents/<id>/   - support agents only

This matches the project brief's authorization principle: "مجوزدهی باید
در کد باشد، نه در prompt" - authorization in code, not in the prompt.
A customer seeing the document list does no harm (it's company-wide
knowledge); a customer uploading arbitrary documents into the knowledge
base the agent uses for everyone would be a real problem.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsSupportAgent
from documents.models import Document, DocumentStatus
from documents.serializers import DocumentSerializer
from documents import services

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB
ALLOWED_EXTENSIONS = {".txt", ".md", ".csv"}


class DocumentListCreateView(APIView):
    """
    GET  /api/documents/   - list all documents (any authenticated user)
    POST /api/documents/   - upload and index a new document (support agents only)
    """

    parser_classes = [MultiPartParser]

    def get_permissions(self):
        """
        Different permission per verb:
          GET  → any authenticated user
          POST → support agents only
        """
        if self.request.method == "GET":
            return [IsAuthenticated()]
        return [IsSupportAgent()]

    def get(self, request):
        documents = Document.objects.select_related("uploaded_by").all()
        return Response(DocumentSerializer(documents, many=True).data)

    def post(self, request):
        file = request.FILES.get("file")
        if not file:
            return Response(
                {"detail": "No file provided. Send a text file as 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if file.size > MAX_FILE_SIZE_BYTES:
            return Response(
                {"detail": f"File too large. Maximum size is {MAX_FILE_SIZE_BYTES // 1024}KB."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filename = file.name or "unnamed.txt"
        ext = ""
        if "." in filename:
            ext = "." + filename.rsplit(".", 1)[-1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return Response(
                {"detail": f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            text_content = file.read().decode("utf-8")
        except UnicodeDecodeError:
            return Response(
                {"detail": "File is not valid UTF-8 text."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not text_content.strip():
            return Response(
                {"detail": "File is empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        title = request.data.get("title", "").strip()
        if not title:
            title = filename.rsplit(".", 1)[0]

        document = Document.objects.create(
            title=title,
            original_filename=filename,
            uploaded_by=request.user,
            status=DocumentStatus.PENDING,
        )

        try:
            chunk_count = services.index_document(document, text_content)
            document.status = DocumentStatus.INDEXED
            document.chunk_count = chunk_count
            document.save(update_fields=["status", "chunk_count"])
        except Exception as exc:
            logger.exception("Failed to index document %s", document.id)
            document.status = DocumentStatus.FAILED
            document.error_message = str(exc)[:500]
            document.save(update_fields=["status", "error_message"])

        return Response(
            DocumentSerializer(document).data,
            status=status.HTTP_201_CREATED,
        )


class DocumentDetailView(APIView):
    """
    DELETE /api/documents/<uuid:document_id>/  - support agents only
    """

    permission_classes = [IsSupportAgent]

    def delete(self, request, document_id):
        try:
            document = Document.objects.get(id=document_id)
        except ObjectDoesNotExist as exc:
            raise NotFound("Document not found.") from exc

        services.delete_document_chunks(str(document.id))
        document.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)