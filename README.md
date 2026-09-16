# Relay — Agentic Customer Support Assistant

**Author:** Mohammadparsa Talebizadeh

Relay is a customer-support assistant where the web layer and the AI
execution layer are **two separate processes** that talk only through
a message queue. A user asks about their order, a company policy, or
something in the outside world; the agent decides which knowledge
source to consult (database, internal documents, public web) and can
take one real action (file a refund). A single `correlation_id`
follows one user turn through all four processes.

```
      ┌───── events → RabbitMQ → Django SSE ─────┐
      │                                          │
      ▼                                          │
   React ──► Django + DRF ──► RabbitMQ ──► AI Worker ──► LLM
             (ASGI)                             │
                │                               │
                ▼                               ▼
       PostgreSQL, ChromaDB            LangGraph, tools
                                        RAG, web search
```

---

## 1. Getting started

```bash
git clone <repo>
cd relay-ai-support
cp .env.example .env      # fill in LLM_PRIMARY_API_KEY (and optionally TAVILY_API_KEY)
docker compose up --build
```

That is the whole install. Six services come up: `frontend`,
`backend`, `ai-worker`, `postgres`, `rabbitmq`, `chromadb`.

| Endpoint | URL |
|---|---|
| Web UI | http://localhost:5173 |
| Django API | http://localhost:18080 |
| RabbitMQ management | http://localhost:15672 |

Seed demo data (creates one admin support agent + several customers,
all with password `password123`):

```bash
docker compose exec backend python manage.py seed_demo_data
docker compose exec backend python manage.py seed_documents
```

---

## 2. Infrastructure & Docker Compose

Six services in one `docker-compose.yml`, with `healthcheck` on every
stateful one and `depends_on: condition: service_healthy` on the app
services:

- **`postgres`** — system of record (users, orders, conversations,
  messages, processed-message ledger, cost ledger).
- **`rabbitmq`** — the *only* bridge between backend and ai-worker.
- **`chromadb`** — vector store for RAG.
- **`backend`** — Django + DRF on **ASGI (uvicorn)**. SSE must be
  async, otherwise one long-lived stream would pin a whole worker
  (the reason the reference course's single-process design is
  explicitly rejected by the brief).
- **`ai-worker`** — plain Python process running the LangGraph agent.
- **`frontend`** — Vite/React + TypeScript.

**Why these choices matter:**

- **Service name, not `localhost`.** Every container has its own
  `localhost`, so `localhost:5432` from Django would look for
  Postgres *inside the Django container*. Compose's embedded DNS
  resolves the service name (`postgres`, `rabbitmq`, `chromadb`) to
  the right container on the shared bridge network — stable across
  restarts, unlike the container's IP.

- **`healthcheck`, not just `depends_on`.** `depends_on` only means
  *started*, not *ready to accept connections* — Postgres and
  RabbitMQ take several seconds to initialize. A real readiness
  probe (`pg_isready`, `rabbitmq-diagnostics ping`, a Chroma
  heartbeat) plus `condition: service_healthy` stops `backend` and
  `ai-worker` from racing their dependencies and crashing on first
  connect.

- **Named volumes** hold Postgres/RabbitMQ/Chroma data outside any
  single container's lifetime, so a plain `docker compose down` /
  rebuild does not wipe seeded users, uploaded documents, or the
  RabbitMQ queues. Only `docker compose down -v` clears them.

---

## 3. Domain & Authorization

Authorization is enforced **in code, never in prompts**. Every read or
write of customer data flows through a single choke-point function per
domain:

- `orders/services.py::orders_visible_to(user)`
- `conversations/services.py::conversations_visible_to(user)`
- `documents/views.py` (list = any user, upload/delete = support only)

The AI worker cannot import Django modules (it is not a Django app),
so it re-implements the *same rule* in plain psycopg
(`ai-worker/db.py::fetch_order_for_user` etc.). The identity used is
always `acting_user_id` from the command envelope — resolved by
Django from a verified JWT before the message ever reaches RabbitMQ.
The LLM has **no argument slot** to say "act as user X".

**404, not 403:** a customer requesting another customer's order gets
`404 Not Found`, not `403 Forbidden`, so the endpoint never leaks
"this exists but is not yours".

**Bonus RBAC:** users with role `support_agent` see any customer's
orders and conversations. Same rule, one extra branch, greppable.

---

## 4. Event-driven architecture

The course runs the agent inside a Django request. Relay does not:
the web process publishes a **command** to RabbitMQ and returns
`202 Accepted` immediately; the AI worker consumes it and publishes
**events** back on a fanout exchange.

### Command vs Event

| | Command | Event |
|---|---|---|
| Meaning | "do this" | "this happened" |
| Consumers | exactly one (ai-worker) | zero or more |
| Route | queue `agent_commands` | fanout exchange `agent_events_exchange` |
| Failure | retried with backoff, then DLQ | not retried; a missed UI update ≠ dropped work |

**Commands:** `command.process_user_message`.

**Events:** `agent_run_started`, `agent_response_chunk`,
`agent_tool_called`, `agent_run_completed`, `agent_run_failed`,
`agent_run_usage`.

### One versioned envelope for both

Defined in `messaging/envelope.py` (Django) and `ai-worker/envelope.py`
(duplicated on purpose — two separate images, no shared package):

```json
{
  "message_id":     "UUID (idempotency key)",
  "correlation_id": "UUID (one per user interaction)",
  "kind":           "command | event",
  "message_type":   "e.g. command.process_user_message",
  "schema_version": 1,
  "occurred_at":    "ISO-8601 UTC",
  "producer":       "backend | ai-worker",
  "retry_count":    0,
  "payload":        { "...": "..." }
}
```

### Reliability

- **Manual ack** with `basic_qos(prefetch_count=1)` — no auto-ack.
- **Exponential backoff** using RabbitMQ's built-in Dead Letter
  Exchange + per-queue TTL: three staged retry queues (1 s, 4 s,
  16 s) that dead-letter back to `agent_commands` on expiry. After
  the retry budget is spent, the message goes to `agent_commands.dlq`.
- **Idempotency** in PostgreSQL: table `messaging_processedmessage`
  with a `UNIQUE` on `message_id`. Two-layer check in the worker: a
  cheap early `SELECT` short-circuits the common redelivery case; the
  final `INSERT` closes the race between two concurrent deliveries.

**Q: What if the same command arrives twice?**
The early check makes redelivery a cheap no-op; if two deliveries
race past it, the `UNIQUE` constraint on the final `INSERT` picks a
winner and the loser acks without re-doing side effects.

**Q: What if the worker crashes before ack?**
Because ack is manual and sent last, RabbitMQ redelivers the message
to another consumer (or to this one after restart). That redelivery
is the "same command twice" case above.

**Known limitation (explicit, not hidden):** RabbitMQ event publish
and the idempotency-row `INSERT` are two separate systems and cannot
be committed atomically without a transactional outbox (out of scope
here). A crash *between* publishing events and writing the ledger
row can cause a redelivery to re-run the agent and re-emit those
events. The order (agent work first, `INSERT` last) shrinks — but
does not eliminate — this window.

### One correlation_id, six places

`Conversation.correlation_id` is minted once when the conversation is
first created and reused forever after. It appears — verbatim — in:
the HTTP request, the RabbitMQ envelope, every SSE frame the browser
receives, the worker's `[trace]` stdout lines, `AgentRun.correlation_id`
in Postgres, and the Langfuse session tag.

---

## 5. The agent (LangGraph)

One small `StateGraph`, two nodes in a loop:

```
    agent_node  ─┬─► tools_node ──► agent_node
                 └─► END
```

Deliberately **one** well-built agent, not three cooperating agents:
the brief scores depth, not agent count, and the "risk/fraud" split
from the reference course does not fit this domain.

### Four tools, three knowledge sources + one action

| Tool | Kind | Input schema |
|---|---|---|
| `get_order_info` | structured internal (DB) | `order_id` (UUID) |
| `search_knowledge_base` | unstructured internal (RAG) | `query` (str, 3–500) |
| `web_search` | external (Tavily) | `query` (str, 3–500) |
| `create_refund_request` | **action** (writes) | `order_id`, `reason` |

**Guardrails required by the brief:**

- Every tool has a **narrow Pydantic input schema**
  (`tools/schemas.py`) — the model can never build an arbitrary SQL
  query; the only "search" tools take a string because a search
  genuinely is one.
- Tools that touch customer data (`get_order_info`,
  `create_refund_request`) are **closures over `acting_user_id`**
  built fresh per run, so the identity comes from the verified JWT,
  never from an LLM-supplied argument.
- The graph runs with `recursion_limit=8`, so one confused run
  cannot burn the LLM budget.
- Checkpointer is `MemorySaver`, not Postgres — conversation history
  is reconstructed from `conversations.Message` before every run; the
  graph state itself only needs to live for one `graph.stream()` call.

### Timeout backstop

`worker.py` runs the graph on a background thread with a 45 s
wall-clock ceiling. A provider SDK that ignores its own timeout (yes,
this has been observed) would otherwise block the single-prefetch
worker forever — a silent full outage instead of an `AGENT_RUN_FAILED`
routed through the normal retry/DLQ path.

### Bonus: automatic fallback to a second model

`llm/factory.py::bind_tools_with_fallback` binds the tools to
**both** the primary and fallback models and only then wraps them
with `.with_fallbacks()`. This order is not decorative:
`RunnableWithFallbacks` has no `bind_tools`, so wrapping first would
crash — but only once a fallback is actually configured, which is
the worst kind of latency. Binding to both models also prevents a
failover from silently answering from memory without tools (which
the brief calls "worse than a wrong answer").

---

## 6. RAG

Full pipeline from upload to retrieval:

1. `POST /api/documents/` (support agent only) uploads a text file.
2. `documents/services.py` chunks it (800 chars, 100 overlap),
   embeds each chunk with ChromaDB's default embedding function,
   stores it in collection `company_documents` with metadata:
   `document_id`, `document_title`, `chunk_index`, `source =
   "internal_document"`.
3. The worker's `search_knowledge_base` tool queries the same
   collection and returns the matching chunks.

Every RAG result carries `"source": "internal_document"` and every
web-search result carries `"source": "web"` — the frontend renders
them with different labels and colors so an internal-document
citation can **never** look like a web citation (a specific
requirement of the brief).

---

## 7. Live streaming (SSE)

The SSE view (`streaming/views.py::conversation_stream`) is an async
ASGI endpoint. Each connection:

- **Authenticates first** — the JWT travels as a query parameter
  (`?token=...`) because `EventSource` cannot set headers. This is
  the *only* endpoint that accepts JWTs from a query string.
- Declares its **own** `exclusive`, `auto_delete` queue bound to the
  events exchange — "each connection has its own short-lived
  subscription" enforced at the broker, not by convention.
- **Filters by `correlation_id`** in the view (the exchange is
  fanout, so every subscriber receives every event).
- Sends a heartbeat every 15 s so proxies do not close an idle
  connection and the frontend can tell "quiet but alive" from "dead".

**Delta buffering:** `agent_response_chunk` events carry ~40 chars
at a time, not one event per token — direct requirement of the
brief.

### The stream ↔ system-of-record boundary

*If the stream drops mid-generation, what does the user ultimately
see?* — **the database, not the stream.** On every reconnect the
frontend's `useConversationStream` hook re-fetches
`GET /api/conversations/<id>/messages/` and only then resumes
listening. The final assistant text (plus its `tool_calls_json`
trace) is written by the worker to `conversations_message` before
`agent_run_completed` fires; the SSE stream is a live relay, not the
truth.

### Four connection states, always visible

`connecting | connected | reconnecting | failed` are derived from
real `EventSource` events (`stream.connected` frame, `onerror`) and
shown as a badge in the chat panel. On any drop, the hook manages
its own reconnect with exponential backoff (1, 2, 4, 8 s, cap 10 s;
6 attempts, then `failed` with a manual **Retry** button). Silent
stalls after a broken stream would be a failure by the brief's
definition.

---

## 8. Observability & cost

Two destinations, on purpose:

| | Langfuse | PostgreSQL (`observability_*`) |
|---|---|---|
| Purpose | full timeline per run, for debugging | ledger the UI reads |
| Read by | developer, in the browser | the product itself (UsagePanel) |
| Optional? | yes (no keys = disabled) | no |

Both are keyed by the same `correlation_id`. The UI must work with
zero SaaS dependencies (e.g. on a laptop with no internet), so the
cost display is backed by Postgres, not Langfuse.

### Execution trace

`ai-worker/observability/tracing.py::RunTracer` is a LangChain
`BaseCallbackHandler` passed to the graph via
`graph.stream(..., config={"callbacks": [...]})`. This means every
model call and every tool call is observed **from inside LangChain**,
not guessed from our event loop — including a model call whose only
output is a `tool_call` (which streams no user-visible text) and a
tool that raises rather than returns an error-shaped string.

Output: one `AgentRun` row per run (also for failed and timed-out
runs — they spent tokens too) and one `RunSpan` row per model/tool
call, replaying the agent's decision path
(`llm → tool → llm → tool → llm`).

### Real token counting, honestly labeled

Priority in `on_llm_end`:

1. `usage_metadata` on the returned message — provider-reported. **Exact.**
2. `llm_output["token_usage"]` — same thing, OpenAI shape. **Exact.**
3. Local byte-length estimate — **labeled `estimated`**, all the way
   to the UI badge.

`llm/factory.py` sends `stream_options: {"include_usage": true}`
so step 1 fires even on streaming responses. Providers that reject
that field can turn it off with `LLM_STREAM_USAGE=0` — the ledger
still works, only the precision label changes.

### Cost figure

The number the UI shows is a **reference cost**: real token counts
priced against the model's published list price. This is what the
same traffic would cost outside the free tier — the honest answer
the brief demands instead of "it was free". Prices are a small table
in `observability/pricing.py`, fully overridable from `.env`.

**Tavily is priced in credits**, not tokens (1 per basic search,
2 per advanced), so `tavily_credits` is a separate column and a
separate line item in the UI — a search that never reached Tavily
(missing key, HTTP failure) records zero credits, not one.

### One endpoint, RBAC-shaped

`GET /api/conversations/<uuid>/usage/` — owner (or any support
agent). A support agent sees the full ledger; a customer sees only
the three headline totals (`run_count`, `total_tokens`,
`total_cost_usd`), redacted server-side before the response leaves
Django.

---

## 9. Frontend

Three pages, as the brief specifies (Login, Chat, Documents), plus a
list of orders. Visual polish is not scored; connection states and
citation clarity are. No routing library — one `selectedOrderId` in
`App.tsx` is enough for three pages.

- `ChatPanel.tsx` — messages (from DB) + live turn (from stream),
  connection badge, live tool-activity panel, source-labeled result
  badges (internal doc = blue, web = orange, DB = green, action = purple).
- `RefundPanel.tsx` — direct REST path, independent of the chat
  agent. Both paths land on the same `orders/services.py`
  authorization check.
- `UsagePanel.tsx` — reference cost with "based on list prices" label,
  provider vs estimated badge, Tavily credit count, per-run span
  breakdown.
- `useConversationStream.ts` — owns the boundary described in §7.

---

## 10. Layout

```
backend/
  accounts/         # custom user model + roles + JWT permissions
  orders/           # Order, Product, RefundRequest + services.py (auth choke-point)
  conversations/    # Conversation, Message + services.py
  documents/        # upload/indexing pipeline for RAG
  messaging/        # envelope, topology, publisher, ProcessedMessage
  streaming/        # ASGI SSE view
  support/          # POST /api/support/messages/ (publish command)
  observability/    # AgentRun/RunSpan models + read-only endpoints

ai-worker/
  worker.py                    # command handler, retry/DLQ, timeout backstop
  envelope.py, topology.py     # duplicated on purpose (two images)
  events.py                    # publish events to the fanout exchange
  db.py                        # plain psycopg — idempotency + auth-scoped reads
  graph/agent_graph.py         # the LangGraph agent (2 nodes, 4 tools)
  tools/                       # get_order_info, search_knowledge_base,
                               # web_search, create_refund_request
  llm/factory.py               # provider-agnostic model + fallback chain
  observability/               # RunTracer, pricing table, Langfuse handler

frontend/src/
  pages/             # Login, Orders, OrderDetail, Documents
  components/        # ChatPanel, RefundPanel, UsagePanel
  hooks/             # useConversationStream (SSE lifecycle)
  api/               # thin fetch wrappers
```

---

## 11. Notable non-goals

The following bonus items are **not implemented**:

- Human confirmation before a destructive tool call.
- Hybrid full-text + vector search.
- Feedback loop back into an evaluation set.
- OpenAPI auto-generation from DRF.
- LLM-as-judge evaluation.
