/**
 * These types mirror the DRF serializers on the backend field-for-field
 * (accounts/serializers.py::MeSerializer, orders/serializers.py::*,
 * conversations/serializers.py::*). There is no code generation step
 * wiring them together - if a backend serializer field is renamed,
 * this file must be updated by hand. That's an accepted tradeoff for
 * this project's size; a larger project would reach for the
 * "Automatic OpenAPI generation from DRF" bonus item (project brief,
 * section 5) and generate these instead.
 */

export type UserRole = "customer" | "support_agent";

export interface Me {
  id: number;
  username: string;
  email: string;
  role: UserRole;
}

export interface Product {
  id: number;
  name: string;
  sku: string;
  description: string;
  price_cents: number;
}

export type OrderStatus =
  | "pending"
  | "processing"
  | "shipped"
  | "delivered"
  | "cancelled";

export interface Order {
  id: string; // UUID
  username: string;
  product: Product;
  quantity: number;
  status: OrderStatus;
  tracking_number: string;
  total_cents: number;
  created_at: string; // ISO datetime
  updated_at: string;
}

export type RefundStatus = "requested" | "approved" | "denied" | "refunded";

export interface RefundRequest {
  id: string; // UUID
  order_id: string; // UUID
  reason: string;
  status: RefundStatus;
  resolution_note: string;
  created_at: string;
}

export interface TokenPair {
  access: string;
  refresh: string;
}

/**
 * Part 4 commit 3: conversation/message types, mirroring
 * backend/conversations/serializers.py.
 */

export interface Conversation {
  id: string; // UUID
  order_id: string; // UUID
  correlation_id: string; // UUID - the id the SSE stream is keyed on
  created_at: string;
}

export type MessageRole = "user" | "assistant";

/**
 * One tool call as recorded on Message.tool_calls_json
 * (ai-worker/db.py::save_assistant_message_with_tool_calls). Each
 * entry is the tool's raw JSON string result - the "matches" /
 * "source" shape it carries is tool-specific (see
 * ai-worker/tools/*.py), so we keep `result` as an opaque string here
 * and parse it lazily only where the UI actually renders a citation.
 */
export interface ToolCallRecord {
  tool_call_id: string;
  tool_name: string | null;
  result: string;
}

export interface ChatMessage {
  id: string; // UUID
  role: MessageRole;
  content: string;
  tool_calls_json: ToolCallRecord[] | null;
  created_at: string;
}

/**
 * SSE connection state, per project brief section 6: "وضعیت‌های
 * اتصال (متصل، قطع شده، در حال اتصال مجدد، ناموفق)" - connected,
 * disconnected, reconnecting, failed. These four values are exactly
 * what useConversationStream.ts exposes and ChatPanel.tsx renders;
 * "silently going quiet on a dropped stream is an error" per the
 * brief, so every one of these must be visible in the UI.
 */
export type StreamConnectionState =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected"
  | "failed";

/**
 * The discriminated union of every event.* MessageType the ai-worker
 * publishes (see ai-worker/envelope.py::MessageType and
 * backend/streaming/views.py, which relays them verbatim as SSE
 * `data:` frames). `type` here is the raw `message_type` string off
 * the envelope, e.g. "event.agent_run_started" - not renamed, so a
 * person tracing a message in the defense sees the exact same string
 * in the browser DevTools network tab and in the RabbitMQ management
 * UI.
 */
export type AgentEvent =
  | { type: "stream.connected"; correlation_id: string }
  | { type: "event.agent_run_started"; correlation_id: string; payload: { acting_user_id: number | null } }
  | { type: "event.agent_response_chunk"; correlation_id: string; payload: { delta: string } }
  | {
      type: "event.agent_tool_called";
      correlation_id: string;
      payload:
        | { tool_call_id: string; tool_name: string; tool_args: Record<string, unknown>; phase: "started" }
        | { tool_call_id: string; tool_name: string | null; result: string; phase: "completed" };
    }
  | {
      type: "event.agent_run_completed";
      correlation_id: string;
      payload: { final_text: string; tool_calls: ToolCallRecord[] };
    }
  | { type: "event.agent_run_failed"; correlation_id: string; payload: { reason: string; retry_count: number } }
  | {
      /**
       * Part 7: published once per agent run - success OR failure -
       * with that run's real token counts, Tavily credits and
       * reference cost (ai-worker/observability/tracing.py::
       * RunSnapshot.to_event_payload). The UI treats it as a "the
       * ledger changed, go re-read it" signal rather than as the
       * number itself, for the same reason it re-fetches messages
       * instead of trusting streamed text: the database is the system
       * of record, the stream only says when.
       */
      type: "event.agent_run_usage";
      correlation_id: string;
      payload: RunUsageEventPayload;
    };


export type DocumentStatus = "pending" | "indexed" | "failed";

export interface DocumentInfo {
  id: string;
  title: string;
  original_filename: string;
  uploaded_by_username: string | null;
  status: DocumentStatus;
  chunk_count: number;
  error_message: string;
  created_at: string;
}

/**
 * --- Part 7: observability & cost ---
 *
 * These mirror backend/observability/serializers.py field for field.
 *
 * Every money value is a STRING, not a number, on purpose: the
 * backend stores costs as fixed-point decimals with 6 places (a
 * single cheap model call really can cost 0.000042 USD) and DRF
 * serializes DecimalField as a string so nothing is lost to binary
 * floating point on the way here. The UI formats these for display
 * and never sums them - all arithmetic happens in the backend, which
 * is the only place that can do it exactly.
 */

export type SpanType = "llm" | "tool";

/** How the token numbers on a run/conversation were obtained. */
export type TokenSource = "provider" | "estimated" | "mixed" | "none";

export type RunStatus = "running" | "completed" | "failed";

export interface RunSpan {
  id: string;
  sequence: number;
  span_type: SpanType;
  name: string;
  started_at: string;
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: string;
  status: string;
  error_message: string;
  detail: Record<string, unknown> | null;
}

export interface AgentRun {
  id: string;
  correlation_id: string;
  command_message_id: string;
  attempt: number;
  status: RunStatus;
  started_at: string;
  finished_at: string | null;
  duration_ms: number;
  llm_call_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  token_source: TokenSource;
  llm_cost_usd: string;
  tavily_credits: number;
  tavily_cost_usd: string;
  total_cost_usd: string;
  trace_url: string;
  error_message: string;
  spans: RunSpan[];
}

export interface UsageTotals {
  run_count: number;
  failed_run_count: number;
  llm_call_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  tavily_credits: number;
  llm_cost_usd: string;
  tavily_cost_usd: string;
  total_cost_usd: string;
  total_duration_ms: number;
  token_source: TokenSource;
}

export interface ConversationUsage {
  conversation_id: string;
  correlation_id: string;
  totals: UsageTotals;
  runs: AgentRun[];
}

/** Payload of the `event.agent_run_usage` SSE frame. */
export interface RunUsageEventPayload {
  correlation_id: string;
  conversation_id: string | null;
  status: RunStatus;
  duration_ms: number;
  llm_call_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  token_source: TokenSource;
  llm_cost_usd: string;
  tavily_credits: number;
  tavily_cost_usd: string;
  total_cost_usd: string;
  trace_url: string;
  spans: Array<Omit<RunSpan, "id" | "started_at" | "error_message">>;
}
