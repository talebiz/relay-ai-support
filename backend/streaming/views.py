"""
SSE endpoint: bridges RabbitMQ events (published by the AI worker) to
the browser, over one long-lived HTTP connection per client.

Project brief, section 6:

    "worker شما جداست و رویدادها باید از RabbitMQ به نمای SSE روی ASGI
    و از آنجا به React برسند و هر اتصال اشتراک کوتاه‌عمر خودش را داشته
    باشد."

    (your worker is separate, and events must travel from RabbitMQ to
    the SSE view on ASGI and from there to React, and each connection
    must have its own short-lived subscription.)

Why this must be an ASGI async view, not a WSGI one: an SSE connection
is held open for the whole conversation, streaming chunks as they
arrive. A synchronous (WSGI) Django worker process would be pinned to
that one connection until it closes - exactly the "a worker must not be
occupied by response generation" problem the whole RabbitMQ split
exists to avoid (project brief, comparison table, row 4). Under ASGI
(uvicorn - see core/asgi.py, already wired from part 1), one worker
process can hold many such connections concurrently because holding an
SSE connection open is just an idle await, not a blocked OS thread.

"Each connection has its own short-lived subscription": every SSE
request declares its OWN exclusive, auto-delete RabbitMQ queue and
binds it to the fanout events exchange (see messaging/topology.py).
"Exclusive" means only this connection can consume from it and it is
deleted the moment this connection closes; "auto-delete" is the same
guarantee from the broker's side if the client vanishes without a
clean close. No two browser tabs ever share a queue, and nothing
lingers in RabbitMQ after a tab is closed.

Filtering: a client only wants events for the correlation_id of the
conversation it is currently viewing, not everyone's traffic. The
exchange is fanout (every subscriber receives every event - that's
what "an event may have zero or more consumers" means at the transport
level), so filtering by correlation_id happens IN THIS VIEW rather than
in RabbitMQ's routing: simple, and correct at this project's scale. A
busier deployment might switch to a topic exchange keyed by
correlation_id to avoid delivering irrelevant events into every open
connection at all, but the brief scores the design decision, not
throughput, so this tradeoff is stated rather than hidden.

Reconnection semantics ("مرز میان جریان و record of system", brief
section 6): this view is a pure real-time relay of events - it holds
NO memory of what happened before the connection opened. If a browser
tab disconnects mid-stream and reconnects, it will only see events
published AFTER the reconnect; it will not replay anything it missed.
The source of truth for "what did the assistant actually say" is the
database (conversations.Message, part 4), written by the AI worker as
it processes the command - NOT the SSE stream itself. The frontend's
job on reconnect is to re-fetch the conversation's current state from
GET /api/conversations/<id>/messages/ (a regular REST endpoint backed
by the DB) to resync, and then resume listening to the live stream for
anything new. This is why the connection-state values called out in
the brief (connected / disconnected / reconnecting / failed) matter:
the UI must show the user when it might be looking at stale state
versus a live one, rather than silently going quiet.

--- Part 4: authorization before opening the stream ---

Part 3 left this as an explicit TODO: confirm the requesting user
actually owns the conversation behind a given correlation_id before
handing them a live feed of it. That is now enforced here, using the
same conversations/services.py chokepoint every other endpoint in this
project uses - never a bespoke check written only for this view.

EventSource (the browser API behind SSE) cannot attach a custom
Authorization header, so the JWT access token travels as a query
parameter instead: `GET .../stream/<correlation_id>/?token=<jwt>`.
This is the one and only place in the codebase that accepts a JWT from
a query string rather than a header - deliberately narrow, rather than
relaxing JWTAuthentication globally for every endpoint.
"""

from __future__ import annotations

import asyncio
import json
import os

import aio_pika
from asgiref.sync import sync_to_async
from django.core.exceptions import ObjectDoesNotExist
from django.http import JsonResponse, StreamingHttpResponse
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from conversations import services as conversation_services
from messaging.topology import EVENTS_EXCHANGE

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "guest")

# How often to send an SSE comment as a heartbeat, so intermediate
# proxies/load balancers do not time out an apparently-idle connection,
# and so the frontend can distinguish "quiet but alive" from "dead".
HEARTBEAT_SECONDS = 15


def _sse_format(event_dict: dict) -> bytes:
    """Format one event as a standard `data: <json>\n\n` SSE frame."""
    return f"data: {json.dumps(event_dict)}\n\n".encode("utf-8")


@sync_to_async
def _authenticate_from_token(token: str):
    """
    Resolve a Django user from a raw JWT access token string, the same
    class of token issued by TokenObtainPairView and normally carried
    in an `Authorization: Bearer <token>` header. Returns None (rather
    than raising) on any validation failure - invalid, expired, or
    malformed - so the caller can respond with a plain 401.
    """
    try:
        validated = JWTAuthentication().get_validated_token(token)
        return JWTAuthentication().get_user(validated)
    except (InvalidToken, TokenError):
        return None


@sync_to_async
def _authorize_conversation(user, correlation_id: str):
    """
    Confirms `user` may see the conversation behind this
    correlation_id, via the exact same authorization function the REST
    endpoints use (conversations.services). Returns None if the
    conversation does not exist OR is not visible to this user -
    deliberately indistinguishable, same "404 not 403" contract as the
    rest of the app.
    """
    try:
        return conversation_services.get_conversation_for_user_by_correlation_id(
            user, correlation_id
        )
    except ObjectDoesNotExist:
        return None


async def _event_stream(correlation_id: str):
    """
    Async generator: connects to RabbitMQ, declares this connection's
    own exclusive/auto-delete queue bound to the fanout events
    exchange, and yields SSE-formatted bytes for every event that
    matches `correlation_id`, until the client disconnects.
    """
    connection = await aio_pika.connect_robust(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        login=RABBITMQ_USER,
        password=RABBITMQ_PASSWORD,
    )

    # Tell the browser the stream is live before anything else arrives -
    # this is also the first thing that lets the frontend flip its
    # connection-status indicator from "connecting" to "connected"
    # (see the brief's requirement to surface real connection states).
    yield _sse_format({"type": "stream.connected", "correlation_id": correlation_id})

    async with connection:
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            EVENTS_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
        )

        # exclusive=True, auto_delete=True: this queue belongs to THIS
        # connection alone and disappears the moment it closes - the
        # "each connection has its own short-lived subscription"
        # requirement, enforced at the broker level rather than just
        # by convention in application code.
        queue = await channel.declare_queue(exclusive=True, auto_delete=True)
        await queue.bind(exchange)

        try:
            while True:
                try:
                    message = await asyncio.wait_for(
                        queue.get(fail=False), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # Nothing arrived within the heartbeat window - send
                    # a comment line (ignored by EventSource as data,
                    # but keeps the connection demonstrably alive).
                    yield b": heartbeat\n\n"
                    continue

                if message is None:
                    continue

                async with message.process():
                    try:
                        event = json.loads(message.body.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue

                # Fanout delivers every event to every subscriber; each
                # connection filters down to the conversation it cares
                # about (see module docstring for why this filtering
                # happens here rather than in RabbitMQ routing).
                if event.get("correlation_id") == correlation_id:
                    yield _sse_format(event)
        finally:
            # aio_pika's connect_robust context manager closes the
            # connection (and therefore this exclusive queue) on the
            # way out, whether we get here via client disconnect or
            # any other exit path.
            pass


async def conversation_stream(request, correlation_id):
    """
    GET /api/stream/<uuid:correlation_id>/?token=<jwt access token>

    Authorization happens BEFORE the SSE connection is ever opened:
    resolve the user from the query-param token, then confirm they own
    the conversation with this correlation_id (or are a support
    agent). Unauthorized or unauthenticated requests get a normal
    401/404 JSON response, never a stream - so a client can tell "you
    are not allowed to see this" apart from "the connection dropped".
    """
    token = request.GET.get("token")
    if not token:
        return JsonResponse({"detail": "Authentication token required."}, status=401)

    user = await _authenticate_from_token(token)
    if user is None:
        return JsonResponse({"detail": "Invalid or expired token."}, status=401)

    conversation = await _authorize_conversation(user, str(correlation_id))
    if conversation is None:
        return JsonResponse({"detail": "Conversation not found."}, status=404)

    response = StreamingHttpResponse(
        _event_stream(str(correlation_id)),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    # Disable buffering in common reverse proxies (nginx) so events are
    # flushed to the client as soon as they're written, not batched.
    response["X-Accel-Buffering"] = "no"
    return response
