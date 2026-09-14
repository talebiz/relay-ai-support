import { useEffect, useRef, useState, type FormEvent } from "react";
import { sendMessage } from "../api/conversations";
import { useConversationStream } from "../hooks/useConversationStream";
import type { ChatMessage, StreamConnectionState, ToolCallRecord } from "../types/domain";

interface ChatPanelProps {
  conversationId: string;
  correlationId: string;
}

/**
 * Human-readable label + visual treatment for each of the four
 * connection states the project brief requires to be surfaced
 * (section 6): "وضعیت‌های اتصال (متصل، قطع شده، در حال اتصال مجدد،
 * ناموفق) نمره دارد" - connected, disconnected, reconnecting, failed
 * are all scored. "connecting" (the initial state before the first
 * "stream.connected" frame arrives) is shown the same as
 * "reconnecting" visually, since both mean "not live yet, but
 * trying".
 */
const CONNECTION_LABEL: Record<StreamConnectionState, string> = {
  connecting: "در حال اتصال...",
  connected: "متصل",
  reconnecting: "در حال اتصال مجدد...",
  disconnected: "قطع شده",
  failed: "اتصال ناموفق",
};

function ConnectionBadge({ state, onRetry }: { state: StreamConnectionState; onRetry: () => void }) {
  return (
    <div className={`connection-badge connection-badge--${state}`}>
      <span className="connection-dot" aria-hidden="true" />
      <span>{CONNECTION_LABEL[state]}</span>
      {state === "failed" && (
        <button type="button" className="connection-retry" onClick={onRetry}>
          تلاش مجدد
        </button>
      )}
    </div>
  );
}

/**
 * Renders one tool's result with a clearly labeled source, so an
 * internal-document citation can never be visually confused with a
 * web citation (project brief section 5). Falls back to plain text if
 * the tool result isn't the JSON shape the built-in tools produce
 * (e.g. a future tool with a different payload).
 */
function ToolResultBadge({ call }: { call: ToolCallRecord }) {
  let parsed: Record<string, unknown> | null = null;
  try {
    parsed = JSON.parse(call.result) as Record<string, unknown>;
  } catch {
    parsed = null;
  }

  const matches = (parsed?.matches as Array<Record<string, unknown>> | undefined) ?? [];
  const source = matches[0]?.source as string | undefined;

  let sourceLabel = "نتیجه‌ی ابزار";
  let sourceClass = "source-generic";
  if (source === "internal_document") {
    sourceLabel = "سند داخلی";
    sourceClass = "source-internal";
  } else if (source === "web") {
    sourceLabel = "نتیجه‌ی وب";
    sourceClass = "source-web";
  } else if (call.tool_name === "get_order_info") {
    sourceLabel = "پایگاه داده‌ی سفارش‌ها";
    sourceClass = "source-db";
  } else if (call.tool_name === "create_refund_request") {
    sourceLabel = "اقدام انجام‌شده";
    sourceClass = "source-action";
  }

  return (
    <details className={`tool-result ${sourceClass}`}>
      <summary>
        <span className="tool-result-badge">{sourceLabel}</span>
        <span className="tool-result-name">{call.tool_name ?? "ابزار"}</span>
      </summary>
      {matches.length > 0 ? (
        <ul className="tool-result-matches">
          {matches.map((match, index) => (
            <li key={index}>
              {typeof match.title === "string" && <strong>{match.title}</strong>}
              {typeof match.excerpt === "string" && <p>{match.excerpt}</p>}
              {typeof match.url === "string" && (
                <a href={match.url} target="_blank" rel="noreferrer">
                  {match.url}
                </a>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <pre className="tool-result-raw">{call.result}</pre>
      )}
    </details>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  return (
    <div className={`chat-bubble chat-bubble--${message.role}`}>
      <div className="chat-bubble-content">{message.content}</div>
      {message.tool_calls_json && message.tool_calls_json.length > 0 && (
        <div className="chat-bubble-tools">
          {message.tool_calls_json.map((call) => (
            <ToolResultBadge key={call.tool_call_id} call={call} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function ChatPanel({ conversationId, correlationId }: ChatPanelProps) {
  const {
    messages,
    liveTurn,
    connectionState,
    messagesError,
    reconnect,
    resyncMessages,
    awaitingResponse,
    notifyMessageSent,
  } = useConversationStream(conversationId, correlationId);

  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const scrollAnchorRef = useRef<HTMLDivElement | null>(null);

  // Composer is locked from the instant the message is queued
  // (awaitingResponse) until a real reply lands or the safeguard
  // timeout in the hook fires - not just while the POST itself is
  // in flight (`sending`), which returns almost immediately since the
  // backend responds 202 Accepted before the agent has even started.
  const composerLocked = sending || awaitingResponse;

  useEffect(() => {
    scrollAnchorRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, liveTurn?.streamedText, liveTurn?.toolActivity.length, awaitingResponse]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || composerLocked) return;

    setSending(true);
    setSendError(null);
    try {
      await sendMessage(conversationId, text);
      setDraft("");
      // Flip into "awaiting a reply" immediately - do not wait for the
      // first SSE event (agent_run_started), since there can be a
      // visible gap between the command being queued and the worker
      // actually publishing anything.
      notifyMessageSent();
      // The user's own message was already persisted synchronously by
      // Django (see backend/support/views.py) before this call even
      // returns, so it is safe to resync immediately rather than
      // waiting for any SSE event to render it.
      await resyncMessages();
    } catch {
      setSendError("ارسال پیام ناموفق بود. دوباره تلاش کنید.");
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="chat-panel">
      <div className="chat-panel-header">
        <h3>گفت‌وگو با پشتیبانی</h3>
        <ConnectionBadge state={connectionState} onRetry={reconnect} />
      </div>

      {messagesError && <p className="form-error">{messagesError}</p>}

      <div className="chat-messages">
        {messages.length === 0 && !liveTurn && !awaitingResponse && (
          <p className="muted chat-empty">
            درباره‌ی این سفارش، سیاست‌های ما، یا هر موضوع دیگری بپرسید - مایا آن را بررسی می‌کند.
          </p>
        )}

        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}

        {(liveTurn || awaitingResponse) && (
          <div className="chat-bubble chat-bubble--assistant chat-bubble--live">
            {liveTurn && liveTurn.toolActivity.length > 0 && (
              <div className="live-activity">
                {liveTurn.toolActivity.map((activity) => (
                  <div key={activity.tool_call_id} className="live-activity-item">
                    <span className={`live-activity-dot live-activity-dot--${activity.phase}`} />
                    <span>
                      {activity.phase === "started"
                        ? `در حال فراخوانی ${activity.tool_name}...`
                        : `${activity.tool_name} تمام شد`}
                    </span>
                  </div>
                ))}
              </div>
            )}
            <div className="chat-bubble-content">
              {liveTurn?.streamedText || <span className="muted">در حال فکر کردن...</span>}
            </div>
          </div>
        )}

        <div ref={scrollAnchorRef} />
      </div>

      <form className="chat-composer" onSubmit={handleSubmit}>
        <input
          type="text"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="پیام خود را بنویسید..."
          disabled={composerLocked}
        />
        <button type="submit" disabled={composerLocked || !draft.trim()}>
          {sending ? "در حال ارسال..." : awaitingResponse ? "در انتظار پاسخ..." : "ارسال"}
        </button>
      </form>
      {sendError && <p className="form-error">{sendError}</p>}
    </div>
  );
}
