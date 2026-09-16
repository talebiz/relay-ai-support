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

    1. Before doing any real work for a command, the worker does a
       cheap SELECT via `already_processed(message_id)`. If a previous
       delivery already finished, the worker skips the agent entirely
       and acks - the common case for a late redelivery.
    2. If the early check does not short-circuit, the worker runs the
       agent (publishing events and executing tool side effects), and
       ONLY THEN INSERTs the ProcessedMessage row for this envelope's
       message_id as the last step before ack.
    3. `message_id` has a UNIQUE constraint. Two deliveries that raced
       past the early check at nearly the same moment will both run
       the agent, but only one INSERT wins; the other raises
       UniqueViolation and is treated as "already handled" - ack, no
       further side effects.

Known limitation (documented in worker.py's module docstring):

    The INSERT and the command's side effects (publishing events on
    RabbitMQ, executing action tools like create_refund_request) are
    NOT in one atomic transaction - they cannot be, because RabbitMQ
    and PostgreSQL are two separate systems that this project does not
    bridge with a transactional outbox. Ordering the INSERT LAST
    minimizes the risk window (a crash between the side effect and the
    INSERT means a redelivery will re-run the agent), but does not
    eliminate it. This is called out explicitly rather than hidden;
    closing it fully is out of scope for this project's size.

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
