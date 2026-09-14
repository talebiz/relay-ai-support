from rest_framework import serializers

from documents.models import Document


class DocumentSerializer(serializers.ModelSerializer):
    uploaded_by_username = serializers.CharField(
        source="uploaded_by.username", read_only=True, default=None
    )

    class Meta:
        model = Document
        fields = [
            "id",
            "title",
            "original_filename",
            "uploaded_by_username",
            "status",
            "chunk_count",
            "error_message",
            "created_at",
        ]
        read_only_fields = fields
