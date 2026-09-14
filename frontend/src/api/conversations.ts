import { apiFetch, API_BASE, getAccessToken } from "./client";
import type { ChatMessage, Conversation } from "../types/domain";

/**
 * GET/POST /api/orders/<order_id>/conversation/ - fetch-or-create the
 * one conversation for this order (backend/conversations/views.py).
 * Both verbs are idempotent on the backend, so a plain GET is enough
 * here; the frontend never needs to distinguish "opening chat for the
 * first time" from "reopening an existing chat".
 */
export function fetchOrCreateConversation(orderId: string): Promise<Conversation> {
  return apiFetch<Conversation>(`/api/orders/${orderId}/conversation/`);
}

/**
 * GET /api/conversations/<id>/messages/ - full message history. This
 * is the RESYNC endpoint (project brief section 6: the database, not
 * the SSE stream, is the source of truth). Called once when the chat
 * panel mounts, and again every time the SSE connection reconnects
 * after a drop, so the UI never trusts "whatever I remember from the
 * stream" as the final state of the conversation.
 */
export function fetchMessages(conversationId: string): Promise<ChatMessage[]> {
  return apiFetch<ChatMessage[]>(`/api/conversations/${conversationId}/messages/`);
}

/**
 * POST /api/support/messages/ - send a new chat message. Returns
 * immediately (202 Accepted) with the conversation's correlation_id;
 * the actual reply arrives later over the SSE stream and/or a later
 * call to fetchMessages.
 */
export interface SendMessageResult {
  conversation_id: string;
  correlation_id: string;
  message_id: string;
  user_message_id: string;
  status: string;
}

export function sendMessage(conversationId: string, text: string): Promise<SendMessageResult> {
  return apiFetch<SendMessageResult>("/api/support/messages/", {
    method: "POST",
    body: JSON.stringify({ conversation_id: conversationId, text }),
  });
}

/**
 * Builds the SSE URL for one conversation's correlation_id.
 *
 * EventSource (the browser's SSE client) cannot attach a custom
 * Authorization header, so the access token travels as a query
 * parameter instead - the one deliberate exception to "always send
 * the JWT as a header", matching backend/streaming/views.py's own
 * docstring on this exact point.
 */
export function buildStreamUrl(correlationId: string): string {
  const token = getAccessToken() ?? "";
  return `${API_BASE}/api/stream/${correlationId}/?token=${encodeURIComponent(token)}`;
}
