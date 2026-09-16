"""
Serializers for the observability read API.

Money is serialized as a STRING (DRF's default for DecimalField), not
a float: 0.000123 USD cannot be represented exactly in binary floating
point, and a cost ledger that drifts in the last digits because of
JSON round-tripping is worse than no ledger. The frontend formats
these strings for display and never does arithmetic on them - the
backend is the only place costs are summed.
"""

from rest_framework import serializers

from observability.models import AgentRun, RunSpan


class RunSpanSerializer(serializers.ModelSerializer):
    class Meta:
        model = RunSpan
        fields = [
            "id",
            "sequence",
            "span_type",
            "name",
            "started_at",
            "duration_ms",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_usd",
            "status",
            "error_message",
            "detail",
        ]
        read_only_fields = fields


class AgentRunSerializer(serializers.ModelSerializer):
    spans = RunSpanSerializer(many=True, read_only=True)

    class Meta:
        model = AgentRun
        fields = [
            "id",
            "correlation_id",
            "command_message_id",
            "attempt",
            "status",
            "started_at",
            "finished_at",
            "duration_ms",
            "llm_call_count",
            "tool_call_count",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "token_source",
            "llm_cost_usd",
            "tavily_credits",
            "tavily_cost_usd",
            "total_cost_usd",
            "trace_url",
            "error_message",
            "spans",
        ]
        read_only_fields = fields
