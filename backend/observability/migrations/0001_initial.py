"""
Hand-written initial migration for the observability app (part 7).

Written by hand rather than produced by `makemigrations` only because
this repository's tooling runs inside Docker; the field definitions
mirror observability/models.py exactly, including the explicit index
and constraint names chosen there so a later `makemigrations` produces
no spurious rename operations.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("conversations", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentRun",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("correlation_id", models.UUIDField(db_index=True)),
                ("command_message_id", models.UUIDField()),
                (
                    "attempt",
                    models.PositiveSmallIntegerField(
                        default=0,
                        help_text="Envelope retry_count at the time of this execution; 0 is the first try.",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                        ],
                        default="running",
                        max_length=20,
                    ),
                ),
                ("started_at", models.DateTimeField()),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("duration_ms", models.PositiveIntegerField(default=0)),
                ("llm_call_count", models.PositiveIntegerField(default=0)),
                ("tool_call_count", models.PositiveIntegerField(default=0)),
                ("input_tokens", models.PositiveIntegerField(default=0)),
                ("output_tokens", models.PositiveIntegerField(default=0)),
                ("total_tokens", models.PositiveIntegerField(default=0)),
                (
                    "token_source",
                    models.CharField(
                        choices=[
                            ("provider", "Reported by provider"),
                            ("estimated", "Estimated locally"),
                            ("mixed", "Mixed"),
                            ("none", "No model call"),
                        ],
                        default="none",
                        max_length=20,
                    ),
                ),
                ("llm_cost_usd", models.DecimalField(decimal_places=6, default=0, max_digits=12)),
                ("tavily_credits", models.PositiveIntegerField(default=0)),
                ("tavily_cost_usd", models.DecimalField(decimal_places=6, default=0, max_digits=12)),
                ("total_cost_usd", models.DecimalField(decimal_places=6, default=0, max_digits=12)),
                ("trace_url", models.URLField(blank=True, default="", max_length=500)),
                ("error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="agent_runs",
                        to="conversations.conversation",
                    ),
                ),
            ],
            options={
                "ordering": ["started_at"],
            },
        ),
        migrations.CreateModel(
            name="RunSpan",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("sequence", models.PositiveIntegerField(default=0)),
                (
                    "span_type",
                    models.CharField(
                        choices=[("llm", "Model call"), ("tool", "Tool call")], max_length=10
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        help_text="Model name for llm spans, tool name for tool spans.",
                        max_length=200,
                    ),
                ),
                ("started_at", models.DateTimeField()),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("duration_ms", models.PositiveIntegerField(default=0)),
                ("input_tokens", models.PositiveIntegerField(default=0)),
                ("output_tokens", models.PositiveIntegerField(default=0)),
                ("total_tokens", models.PositiveIntegerField(default=0)),
                ("cost_usd", models.DecimalField(decimal_places=6, default=0, max_digits=12)),
                ("status", models.CharField(default="ok", max_length=20)),
                ("error_message", models.TextField(blank=True, default="")),
                ("detail", models.JSONField(blank=True, null=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="spans",
                        to="observability.agentrun",
                    ),
                ),
            ],
            options={
                "ordering": ["sequence"],
            },
        ),
        migrations.AddIndex(
            model_name="agentrun",
            index=models.Index(
                fields=["conversation", "started_at"], name="obs_run_conv_started_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="agentrun",
            constraint=models.UniqueConstraint(
                fields=("command_message_id", "attempt"), name="unique_run_per_command_attempt"
            ),
        ),
        migrations.AddIndex(
            model_name="runspan",
            index=models.Index(fields=["run", "sequence"], name="obs_span_run_seq_idx"),
        ),
    ]
