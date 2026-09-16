import { apiFetch } from "./client";
import type { ConversationUsage } from "../types/domain";

/**
 * GET /api/conversations/<id>/usage/ - the cost ledger and execution
 * trace for one conversation (backend/observability/views.py).
 *
 * This is the AUTHORITATIVE source for what a conversation has cost.
 * The `event.agent_run_usage` SSE frame carries the same numbers for
 * the run that just finished, but the UI uses that only as a signal
 * to call this endpoint again - the same "the stream says WHEN, the
 * database says WHAT" boundary the message list already follows
 * (project brief section 6). A browser that was disconnected while
 * three turns ran still sees the correct total the moment it calls
 * this.
 */
export function fetchConversationUsage(conversationId: string): Promise<ConversationUsage> {
  return apiFetch<ConversationUsage>(`/api/conversations/${conversationId}/usage/`);
}
