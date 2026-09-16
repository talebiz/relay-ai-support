from django.contrib import admin

from observability.models import AgentRun, RunSpan


class RunSpanInline(admin.TabularInline):
    model = RunSpan
    extra = 0
    readonly_fields = [
        "sequence",
        "span_type",
        "name",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "status",
        "detail",
    ]
    can_delete = False


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    """
    Read-only on purpose: these rows are a ledger written by the AI
    worker, not editable records. Being able to hand-edit a recorded
    cost would make the whole table untrustworthy.
    """

    list_display = [
        "id",
        "correlation_id",
        "status",
        "started_at",
        "duration_ms",
        "total_tokens",
        "tavily_credits",
        "total_cost_usd",
    ]
    list_filter = ["status", "token_source"]
    search_fields = ["correlation_id", "command_message_id", "id"]
    inlines = [RunSpanInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
