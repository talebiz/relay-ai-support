from django.contrib import admin

from documents.models import Document


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "chunk_count", "uploaded_by", "created_at")
    list_filter = ("status",)
    search_fields = ("title", "original_filename")
    readonly_fields = ("id", "chunk_count", "error_message", "created_at")
