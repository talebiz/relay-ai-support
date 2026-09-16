"""
Wipe the chat side of the system back to empty, without touching the
domain data around it.

    python manage.py reset_chat_data

Why this is a command and not a page of DELETE statements: "reset the
chat" touches four tables owned by three different apps, in an order
that matters, plus RabbitMQ - and getting the order or the scope wrong
leaves the system in a state that looks clean but is not. The two
traps, spelled out:

  1. Deleting a Conversation does NOT delete its cost ledger.
     `AgentRun.conversation` is `on_delete=SET_NULL` on purpose (see
     observability/models.py: a deleted conversation must not erase
     the record of money already spent). So a naive
     `Conversation.objects.all().delete()` leaves every AgentRun and
     RunSpan row behind, orphaned and invisible in the UI - the cost
     is still in the database, just unreachable. This command deletes
     the runs explicitly, first.

  2. Deleting `messaging_processedmessage` without emptying RabbitMQ
     re-arms every command still sitting in a queue. Those rows ARE
     the idempotency guarantee (worker.py's answer to "what if the
     same command arrives twice"). Drop them while an old command is
     still parked in a retry queue, and when it is redelivered the
     worker will happily run the agent again - against a conversation
     that no longer exists. That is why the queues are purged by
     default, and why `--keep-queues` prints a warning.

WHAT IS DELETED (this is the whole list):
    conversations_message        every chat message
    conversations_conversation   the threads themselves (and their
                                 correlation_ids)
    observability_runspan        every model/tool call record
    observability_agentrun       every run + its cost
    messaging_processedmessage   the idempotency ledger
    RabbitMQ: agent_commands, agent_commands.retry.*, agent_commands.dlq

WHAT IS NOT TOUCHED:
    users, orders, products, refund requests (unless --with-refunds),
    uploaded documents, and the ChromaDB vector store. Re-seeding demo
    data is a separate concern - see orders/management/commands/
    seed_demo_data.py and documents/management/commands/seed_documents.py.

It lives in `conversations` because that app owns the system of record
for the chat; the rows it deletes in `observability` and `messaging`
only exist as a consequence of a conversation having happened.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from conversations.models import Conversation, Message
from messaging.models import ProcessedMessage
from observability.models import AgentRun, RunSpan


class Command(BaseCommand):
    help = "Delete chat messages, conversations, agent runs/costs and the idempotency ledger."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt. Required when running with `docker compose exec -T`.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would be deleted and stop without deleting anything.",
        )
        parser.add_argument(
            "--user",
            type=str,
            default=None,
            help=(
                "Limit the reset to one username's conversations. Without it, every "
                "conversation in the database is reset."
            ),
        )
        parser.add_argument(
            "--keep-conversations",
            action="store_true",
            help=(
                "Delete messages and cost records but keep the conversation rows, so every "
                "correlation_id stays valid and an already-open browser tab's SSE stream "
                "does not have to be re-established."
            ),
        )
        parser.add_argument(
            "--with-refunds",
            action="store_true",
            help=(
                "Also delete refund requests. Off by default: a refund is real domain data "
                "that happens to be creatable from chat, not chat data."
            ),
        )
        parser.add_argument(
            "--keep-queues",
            action="store_true",
            help=(
                "Do not purge the RabbitMQ command/retry/DLQ queues. Unsafe together with "
                "clearing the idempotency ledger - see this module's docstring."
            ),
        )

    def handle(self, *args, **options):
        scope_username = options["user"]

        conversations = Conversation.objects.all()
        if scope_username:
            conversations = conversations.filter(user__username=scope_username)
            if not conversations.exists():
                raise CommandError(
                    f"No conversations found for user {scope_username!r}. "
                    "Check the username, or omit --user to reset everything."
                )

        # Captured BEFORE anything is deleted: ProcessedMessage has no
        # foreign key to a conversation, so correlation_id is the only
        # thing that ties an idempotency row back to the chat it came
        # from. Once the Conversation rows are gone, that link is gone.
        correlation_ids = list(conversations.values_list("correlation_id", flat=True))

        messages = Message.objects.filter(conversation__in=conversations)
        runs = AgentRun.objects.filter(conversation__in=conversations)
        spans = RunSpan.objects.filter(run__in=runs)
        processed = ProcessedMessage.objects.filter(correlation_id__in=correlation_ids)

        counts = {
            "conversations_message": messages.count(),
            "conversations_conversation": 0 if options["keep_conversations"] else conversations.count(),
            "observability_runspan": spans.count(),
            "observability_agentrun": runs.count(),
            "messaging_processedmessage": processed.count(),
        }

        refunds = None
        if options["with_refunds"]:
            from orders.models import RefundRequest

            refunds = RefundRequest.objects.all()
            if scope_username:
                refunds = refunds.filter(user__username=scope_username)
            counts["orders_refundrequest"] = refunds.count()

        scope_label = f"user {scope_username!r}" if scope_username else "ALL users"
        self.stdout.write(self.style.MIGRATE_HEADING(f"Chat reset scope: {scope_label}"))
        for table, count in counts.items():
            self.stdout.write(f"  {table:<30} {count:>7}")

        if options["keep_conversations"]:
            self.stdout.write(
                "  (conversation rows kept: --keep-conversations, correlation_ids stay valid)"
            )

        if sum(counts.values()) == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to delete - already clean."))
            return

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("--dry-run: nothing was deleted."))
            return

        if not options["yes"]:
            answer = input("\nDelete these rows? This cannot be undone. Type 'yes' to confirm: ")
            if answer.strip().lower() != "yes":
                self.stdout.write(self.style.WARNING("Aborted - nothing was deleted."))
                return

        # One transaction: either the whole chat history is gone or
        # none of it is. A half-reset - messages deleted but their cost
        # ledger still present, say - is worse than either outcome,
        # because the UI would then show a cost for a conversation with
        # no messages in it.
        with transaction.atomic():
            # Spans before runs, runs before conversations. Spans would
            # cascade from runs anyway, but deleting them explicitly is
            # what makes the printed count above a real count rather
            # than a guess about what CASCADE will do.
            spans.delete()
            runs.delete()
            messages.delete()
            if refunds is not None:
                refunds.delete()
            processed.delete()
            if not options["keep_conversations"]:
                conversations.delete()

        self.stdout.write(self.style.SUCCESS("Database rows deleted."))

        if options["keep_queues"]:
            self.stdout.write(
                self.style.WARNING(
                    "--keep-queues: RabbitMQ was NOT purged. Any command still parked in "
                    "agent_commands / a retry queue / the DLQ can still be delivered, and "
                    "the idempotency rows that would have stopped it are now gone."
                )
            )
        elif scope_username:
            self.stdout.write(
                self.style.WARNING(
                    "Scoped to one user, so RabbitMQ was NOT purged: the queues are shared "
                    "and purging them would drop other users' pending commands too."
                )
            )
        else:
            self._purge_queues()

        self.stdout.write(
            self.style.SUCCESS(
                "Done. Reload the order page - a fresh conversation (and a fresh "
                "correlation_id) is created on first open."
            )
        )

    def _purge_queues(self) -> None:
        """
        Empty the command, retry and DLQ queues.

        Best-effort: a reachable broker is not required for the
        database reset above to have been correct, so a connection
        failure here is reported as a warning with the manual
        alternative, not raised as an error that would make the caller
        think the whole command failed.

        The events exchange is deliberately not touched - it is a
        fanout with no durable queue bound to it (each SSE connection
        declares its own exclusive, auto-delete queue), so there is
        nothing there to purge.
        """
        import pika

        from messaging.publisher import _connection
        from messaging.topology import (
            COMMAND_QUEUE,
            DLQ_QUEUE,
            RETRY_STAGES_MS,
            declare_topology,
            retry_queue_for_attempt,
        )

        queue_names = [COMMAND_QUEUE, DLQ_QUEUE] + [
            retry_queue_for_attempt(stage) for stage in range(len(RETRY_STAGES_MS))
        ]

        try:
            connection = _connection()
        except pika.exceptions.AMQPError as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"Could not reach RabbitMQ to purge queues ({exc}). "
                    "Purge them by hand at http://localhost:15672 if any command is pending."
                )
            )
            return

        try:
            channel = connection.channel()
            # The queues may not exist yet on a fresh volume; declaring
            # first (idempotent) means a purge right after `up` does
            # not fail on an unknown queue.
            declare_topology(channel)
            for name in queue_names:
                result = channel.queue_purge(queue=name)
                purged = getattr(getattr(result, "method", None), "message_count", None)
                suffix = f" ({purged} message(s))" if purged else ""
                self.stdout.write(f"  purged {name}{suffix}")
        finally:
            connection.close()

        self.stdout.write(self.style.SUCCESS("RabbitMQ queues purged."))
