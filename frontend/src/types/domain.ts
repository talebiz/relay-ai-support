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
  | { type: "event.agent_run_failed"; correlation_id: string; payload: { reason: string; retry_count: number } };
