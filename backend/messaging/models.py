"""
Database-enforced idempotency.

Project brief, section 3(b):

    "قابلیت اطمینان... و idempotency که در پایگاه داده اعمال شود.
    باید به دو پرسش پاسخ دهید: اگر همان فرمان دو بار برسد چه می‌شود؟"

    (reliability ... and idempotency enforced in the database. You must
    answer: what happens if the same command arrives twice?)

Why this has to live in the database, not in worker memory:

RabbitMQ (with manual ack, see ai-worker/worker.py) only guarantees
AT-LEAST-ONCE delivery, never exactly-once. A message is redelivered
whenever the broker cannot be sure the consumer finished (e.g. the
worker crashed after doing the work but before sending `basic_ack`, or
the TCP connection dropped mid-handshake of the ack itself). If the
worker only tracked "messages I've seen" in a Python set in memory,
that tracking would vanish the instant the worker process restarts -
exactly the crash scenario this exists to protect against. So the
record of "this message_id was already fully processed" must survive
a worker crash, which means: the database.

How it is used (see ai-worker's handler, worker.py):

    1. Before doing any real work for a command, the worker tries to
       INSERT a ProcessedMessage row for this envelope's message_id.
    2. `message_id` has a UNIQUE constraint. If a second delivery of
       the same message arrives (whether concurrently or after a
       crash-and-redeliver), the INSERT hits a UniqueViolation.
    3. On UniqueViolation, the worker knows this exact message was
       already handled (or is being handled right now) and skips the
       side-effecting work entirely, then acks immediately - this is
       what makes redelivery safe (the "answer" to "what happens if
       the same command arrives twice": nothing happens the second
       time; the ack is sent and RabbitMQ stops redelivering it).
    4. The INSERT and the command's actual side effect (e.g. creating
       a RefundRequest) happen in the SAME database transaction, so a
       crash between "recorded as processed" and "side effect applied"
       cannot happen - either both commit or neither does.

This model is intentionally tiny: it does not need columns beyond what
answers "have we already done this exact message" and basic
observability (when, and what type).
"""

from django.db import models


class ProcessedMessage(models.Model):
    """
    One row per successfully processed message (command). Existence of
    a row = "do not process this message_id again."
    """

    message_id = models.UUIDField(unique=True)
    correlation_id = models.UUIDField()
    message_type = models.CharField(max_length=100)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Explicit name, matching migrations/0001_initial.py and the
        # real index in PostgreSQL - see conversations/models.py for
        # the full explanation of why an unnamed index here produced a
        # permanent "messaging has changes not yet reflected in a
        # migration" warning. The name looks auto-generated because the
        # hand-written migration imitated Django's format; it is kept
        # verbatim so no index actually has to be renamed.
        indexes = [models.Index(fields=["correlation_id"], name="messaging_p_correla_1e5f4a_idx")]

    def __str__(self):
        return f"ProcessedMessage({self.message_id})"
