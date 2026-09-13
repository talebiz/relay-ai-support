from django.contrib import admin

from conversations.models import Conversation, Message


class MessageInline(admin.TabularInline):
    # Read-only in practice - useful during the defense to show a full
    # transcript for one conversation without leaving the admin.
    model = Message
    extra = 0
    fields = ("role", "content", "created_at")
    readonly_fields = ("role", "content", "created_at")
    can_delete = False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "user", "correlation_id", "created_at")
    search_fields = ("id__exact", "correlation_id__exact", "user__username")
    inlines = [MessageInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "conversation", "role", "created_at")
    list_filter = ("role",)
