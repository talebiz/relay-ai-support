import { useCallback, useEffect, useRef, useState } from "react";
import { buildStreamUrl, fetchMessages } from "../api/conversations";
import type { AgentEvent, ChatMessage, StreamConnectionState } from "../types/domain";

/**
 * One in-flight agent turn's live state, kept separate from the
 * durable ChatMessage list below. This exists purely for the UI to
 * show streaming text and a "live activity panel" (project brief
 * section 6 presentation requirement) WHILE a reply is being
 * generated - it is thrown away the moment the turn finishes and the
 * real Message row is re-fetched from the database.
 */
export interface LiveToolActivity {
  tool_call_id: string;
  tool_name: string;
  phase: "started" | "completed";
  args?: Record<string, unknown>;
  result?: string;
}

export interface LiveTurn {
  correlationId: string;
  streamedText: string;
  toolActivity: LiveToolActivity[];
  isRunning: boolean;
}

const MAX_RECONNECT_DELAY_MS = 10_000;
const BASE_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_ATTEMPTS = 6;

/**
 * How long the UI is willing to wait, after the user's message was
 * successfully queued (202 Accepted from POST /api/support/messages/),
 * before it gives up on hearing anything back over SSE and unlocks the
 * composer again. This is a pure UX safeguard against the backend
 * hanging (e.g. the worker stuck waiting on an LLM provider) - it does
 * NOT cancel the underlying agent run, which may still complete later
 * and simply show up on the next resync. 45s is generous enough for a
 * normal multi-tool agent turn while still bounding how long a user is
 * ever silently stuck.
 */
const AWAITING_RESPONSE_TIMEOUT_MS = 45_000;

/**
 * Owns everything about one conversation's live connection:
 *
 *   - The message list (`messages`), always sourced from the database
 *     via `fetchMessages` - never assembled purely from SSE events.
 *     This is the "database is the system of record, not the stream"
 *     boundary from project brief section 6: if the SSE connection
 *     drops mid-reply, `messages` is what the user sees once it comes
 *     back, re-fetched from Postgres through Django, not reconstructed
 *     from whatever chunks happened to arrive.
 *   - The live, in-progress turn (`liveTurn`), which shows the
 *     streaming text and tool-call activity WHILE the agent is still
 *     working - purely a UI nicety, discarded once the turn completes
 *     and a fresh `fetchMessages` confirms the real, persisted answer.
 *   - `awaitingResponse`: true from the instant the user's message is
 *     queued until either a real reply arrives (agent_run_completed /
 *     agent_run_failed) or the safeguard timeout above fires. This is
 *     what lets the composer show "thinking..." and stay locked even
 *     BEFORE the first SSE event (agent_run_started) arrives - closing
 *     the gap where the worker has accepted the command but hasn't
 *     published anything yet.
 *   - The connection state (`connectionState`), one of exactly the
 *     four values the brief requires to be visible: connecting,
 *     connected, reconnecting, disconnected, failed. Silently going
 *     quiet on a dropped stream is explicitly called out as a mistake
 *     in the brief - this hook never does that.
 *
 * Reconnection strategy: EventSource has its own built-in retry, but
 * it retries forever with a fixed (usually short) delay and gives the
 * caller no visibility into "how many times has this failed" - not
 * enough to honestly render "reconnecting" vs "failed". So this hook
 * manages the EventSource lifecycle itself: on any error it closes
 * the current EventSource, waits an exponentially increasing delay,
 * and opens a fresh one, re-fetching message history each time (since
 * events missed while disconnected are never replayed - see
 * backend/streaming/views.py's own docstring on this). After
 * MAX_RECONNECT_ATTEMPTS consecutive failures, state moves to
 * "failed" and stops retrying automatically; `reconnect()` lets the
 * user retry by hand.
 */
export function useConversationStream(conversationId: string | null, correlationId: string | null) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [liveTurn, setLiveTurn] = useState<LiveTurn | null>(null);
  const [connectionState, setConnectionState] = useState<StreamConnectionState>("connecting");
  const [messagesError, setMessagesError] = useState<string | null>(null);
  const [awaitingResponse, setAwaitingResponse] = useState(false);

  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const awaitingTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const clearAwaitingTimeout = useCallback(() => {
    if (awaitingTimeoutRef.current) {
      clearTimeout(awaitingTimeoutRef.current);
      awaitingTimeoutRef.current = null;
    }
  }, []);

  const resyncMessages = useCallback(async () => {
    if (!conversationId) return;
    try {
      const fresh = await fetchMessages(conversationId);
      if (mountedRef.current) {
        setMessages(fresh);
        setMessagesError(null);
      }
    } catch {
      if (mountedRef.current) {
        setMessagesError("Failed to load message history.");
      }
    }
  }, [conversationId]);

  /**
   * Called by the composer the instant the POST to
   * /api/support/messages/ succeeds - i.e. BEFORE any SSE event for
   * this turn has necessarily arrived. Flips the UI into "awaiting a
   * reply" immediately (so "thinking..." shows and the composer locks
   * with no visible gap) and arms the safeguard timeout described
   * above.
   */
  const notifyMessageSent = useCallback(() => {
    setAwaitingResponse(true);
    setMessagesError(null);
    clearAwaitingTimeout();
    awaitingTimeoutRef.current = setTimeout(() => {
      if (!mountedRef.current) return;
      setAwaitingResponse(false);
      setMessagesError("پاسخ‌دهی بیش از حد معمول طول کشید. دوباره تلاش کنید.");
    }, AWAITING_RESPONSE_TIMEOUT_MS);
  }, [clearAwaitingTimeout]);

  const handleEvent = useCallback(
    (event: AgentEvent) => {
      switch (event.type) {
        case "stream.connected": {
          setConnectionState("connected");
          reconnectAttemptsRef.current = 0;
          break;
        }

        case "event.agent_run_started": {
          setLiveTurn({
            correlationId: event.correlation_id,
            streamedText: "",
            toolActivity: [],
            isRunning: true,
          });
          break;
        }

        case "event.agent_response_chunk": {
          setLiveTurn((prev) =>
            prev
              ? { ...prev, streamedText: prev.streamedText + event.payload.delta }
              : {
                  correlationId: event.correlation_id,
                  streamedText: event.payload.delta,
                  toolActivity: [],
                  isRunning: true,
                }
          );
          break;
        }

        case "event.agent_tool_called": {
          const payload = event.payload;
          setLiveTurn((prev) => {
            const base: LiveTurn = prev ?? {
              correlationId: event.correlation_id,
              streamedText: "",
              toolActivity: [],
              isRunning: true,
            };
            if (payload.phase === "started") {
              return {
                ...base,
                toolActivity: [
                  ...base.toolActivity,
                  {
                    tool_call_id: payload.tool_call_id,
                    tool_name: payload.tool_name,
                    phase: "started",
                    args: payload.tool_args,
                  },
                ],
              };
            }
            // phase === "completed": update the matching started entry in place.
            const completedPayload = payload;
            return {
              ...base,
              toolActivity: base.toolActivity.map((activity) =>
                activity.tool_call_id === completedPayload.tool_call_id
                  ? { ...activity, phase: "completed", result: completedPayload.result }
                  : activity
              ),
            };
          });
          break;
        }

        case "event.agent_run_completed": {
          // The turn is done. Re-fetch from the database rather than
          // trusting the accumulated stream text as final - this is
          // the literal boundary between "the live stream" and "the
          // record of system" the brief asks for a design decision
          // on: the stream told us WHEN it finished, but the DATABASE
          // (via resyncMessages) tells us WHAT was actually said.
          setLiveTurn(null);
          setAwaitingResponse(false);
          clearAwaitingTimeout();
          void resyncMessages();
          break;
        }

        case "event.agent_run_failed": {
          setLiveTurn(null);
          setAwaitingResponse(false);
          clearAwaitingTimeout();
          setMessagesError(`The assistant could not complete this reply: ${event.payload.reason}`);
          void resyncMessages();
          break;
        }
      }
    },
    [resyncMessages, clearAwaitingTimeout]
  );

  const connect = useCallback(() => {
    if (!correlationId) return;

    // Always resync from the database before/around opening a fresh
    // connection - covers both the very first mount and every
    // reconnect, since anything published while disconnected is gone
    // for good from the stream's point of view (see module docstring).
    void resyncMessages();

    const source = new EventSource(buildStreamUrl(correlationId));
    eventSourceRef.current = source;
    setConnectionState((prev) => (prev === "connected" ? "reconnecting" : "connecting"));

    source.onmessage = (message: MessageEvent<string>) => {
      try {
        const parsed = JSON.parse(message.data) as AgentEvent;
        handleEvent(parsed);
      } catch {
        // Malformed frame - ignore it rather than crashing the whole
        // connection; a single bad event should not take down the chat.
      }
    };

    source.onerror = () => {
      source.close();
      if (eventSourceRef.current === source) {
        eventSourceRef.current = null;
      }
      if (!mountedRef.current) return;

      reconnectAttemptsRef.current += 1;
      if (reconnectAttemptsRef.current > MAX_RECONNECT_ATTEMPTS) {
        setConnectionState("failed");
        return;
      }

      setConnectionState("reconnecting");
      const delay = Math.min(
        BASE_RECONNECT_DELAY_MS * 2 ** (reconnectAttemptsRef.current - 1),
        MAX_RECONNECT_DELAY_MS
      );
      reconnectTimerRef.current = setTimeout(() => {
        if (mountedRef.current) connect();
      }, delay);
    };
  }, [correlationId, handleEvent, resyncMessages]);

  useEffect(() => {
    mountedRef.current = true;
    reconnectAttemptsRef.current = 0;
    setConnectionState("connecting");
    setLiveTurn(null);
    setAwaitingResponse(false);
    clearAwaitingTimeout();

    if (correlationId) {
      connect();
    }

    return () => {
      mountedRef.current = false;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      clearAwaitingTimeout();
    };
    // Only re-run this whole lifecycle when the target conversation
    // changes - `connect` itself is stable across chunk/tool events.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [correlationId]);

  /** Manual retry after the connection gives up (`connectionState === "failed"`). */
  const reconnect = useCallback(() => {
    reconnectAttemptsRef.current = 0;
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    connect();
  }, [connect]);

  return {
    messages,
    liveTurn,
    connectionState,
    messagesError,
    reconnect,
    resyncMessages,
    awaitingResponse,
    notifyMessageSent,
  };
}
