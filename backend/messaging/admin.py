from django.contrib import admin

from messaging.models import ProcessedMessage


@admin.register(ProcessedMessage)
class ProcessedMessageAdmin(admin.ModelAdmin):
    # Read-only in practice: this table exists to be inspected during
    # the defense (e.g. "show me that message X was only processed
    # once even though it was redelivered"), not edited by hand.
    list_display = ("message_id", "message_type", "correlation_id", "processed_at")
    list_filter = ("message_type",)
    search_fields = ("message_id__exact", "correlation_id__exact")
    ordering = ("-processed_at",)
