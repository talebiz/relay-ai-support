import { useCallback, useEffect, useState } from "react";
import { fetchConversationUsage } from "../api/observability";
import type { AgentRun, ConversationUsage, RunSpan, TokenSource } from "../types/domain";

interface UsagePanelProps {
  conversationId: string;
  /**
   * Bumped by `useConversationStream` whenever a run's ledger changes
   * (see its `usageVersion`). Any change re-fetches from the database
   * rather than patching a local total from the event - same rule as
   * the message list.
   */
  refreshToken: number;
  /**
   * When true (admin/support agent), the panel is expandable and the
   * per-run trace, token breakdown, span list and Langfuse link are
   * all reachable. When false (customer), only the collapsed headline
   * remains: turn count, total tokens, total cost - the one thing
   * section 7 of the brief requires be visible, and nothing more.
   * The body is never mounted in that mode, so operator-side data
   * (correlation ids, model names, per-span JSON) can't leak.
   */
  showDetails?: boolean;
}

/**
 * "هزینه هر گفت‌وگو را در رابط کاربری نشان دهید" - project brief,
 * section 7. This panel is that requirement, and it is deliberately
 * not just one number.
 *
 * The brief's next sentence is the reason: «API رایگان است، اما
 * «رایگان بود» پاسخ این پرسش نیست: token ها را واقعی بشمارید،
 * اعتبارهای Tavily را اضافه کنید». So the panel shows, in order:
 *
 *   1. the reference cost in USD (what this traffic would cost at
 *      list prices - labeled as a reference, never as an invoice),
 *   2. the real token counts behind that number, with an explicit
 *      badge when any of them was estimated locally instead of
 *      reported by the provider,
 *   3. the Tavily credits consumed, as their own count - they are not
 *      tokens and folding them into the token math would hide them,
 *   4. and the per-run execution trace: every model call and every
 *      tool call, in order, with its duration, tokens and cost.
 *
 * Point 4 is the other half of section 7 ("ردهای اجرا برای کل اجرای
 * عامل، هر فراخوانی مدل و ابزار") rendered where a person can
 * actually see it, with the run's correlation_id shown next to it -
 * the same id that appears in the RabbitMQ envelope, the worker's
 * logs, and (when configured) the Langfuse trace this panel links to.
 *
 * --- Why the whole panel collapses ---
 *
 * `.chat-panel` has a fixed height, so anything mounted under the
 * composer is taken directly out of the message list's share of it.
 * The full trace is something you open when you are asking "what did
 * the agent actually do?", not something you need in view while
 * chatting - so the default is COLLAPSED, and collapsed still shows
 * the one number section 7 actually requires ("هزینه هر گفت‌وگو را
 * نشان دهید"). Expanding it is what reveals the trace, and even then
 * the body is height-capped and scrolls on its own so it can never
 * squeeze the conversation out of the panel.
 */

const TOKEN_SOURCE_LABEL: Record<TokenSource, string> = {
  provider: "شمارش دقیق ارائه‌دهنده",
  estimated: "تخمین محلی",
  mixed: "ترکیبی (بخشی تخمینی)",
  none: "بدون فراخوانی مدل",
};

/**
 * Costs arrive as strings (see types/domain.ts on why) and are only
 * ever formatted here, never summed. Very small amounts would round
 * to "$0.00" and read as "free" - exactly the impression the brief
 * says not to give - so anything under a cent is shown with enough
 * digits to be a real number.
 */
function formatUsd(value: string): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return value;
  if (amount === 0) return "$0";
  if (amount < 0.01) return `$${amount.toFixed(6)}`;
  return `$${amount.toFixed(4)}`;
}

function formatTokens(value: number): string {
  return value.toLocaleString("fa-IR");
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} میلی‌ثانیه`;
  return `${(ms / 1000).toFixed(1)} ثانیه`;
}

function SpanRow({ span }: { span: RunSpan }) {
  const isLlm = span.span_type === "llm";
  const credits = Number(span.detail?.tavily_credits ?? 0);

  return (
    <li className={`usage-span usage-span--${span.span_type} usage-span--${span.status}`}>
      <span className="usage-span-seq">{span.sequence}</span>
      <span className="usage-span-kind">{isLlm ? "مدل" : "ابزار"}</span>
      <span className="usage-span-name">{span.name}</span>
      <span className="usage-span-meta">
        {formatDuration(span.duration_ms)}
        {isLlm && (
          <>
            {" · "}
            {formatTokens(span.input_tokens)}↓ / {formatTokens(span.output_tokens)}↑ توکن
          </>
        )}
        {credits > 0 && <> {" · "}{credits} اعتبار Tavily</>}
        {span.status !== "ok" && <> {" · "}<strong>{span.status}</strong></>}
      </span>
      <span className="usage-span-cost">{formatUsd(span.cost_usd)}</span>
    </li>
  );
}

function RunBlock({ run, index }: { run: AgentRun; index: number }) {
  return (
    <details className={`usage-run usage-run--${run.status}`}>
      <summary>
        <span className="usage-run-title">
          نوبت {index + 1}
          {run.attempt > 0 && <span className="usage-run-attempt"> (تلاش {run.attempt + 1})</span>}
        </span>
        <span className="usage-run-summary">
          {run.llm_call_count} فراخوانی مدل · {run.tool_call_count} ابزار ·{" "}
          {formatTokens(run.total_tokens)} توکن
        </span>
        <span className="usage-run-cost">{formatUsd(run.total_cost_usd)}</span>
      </summary>

      <div className="usage-run-body">
        <p className="usage-run-ids">
          <span>correlation_id: </span>
          <code>{run.correlation_id}</code>
        </p>
        {run.status === "failed" && run.error_message && (
          <p className="usage-run-error">{run.error_message}</p>
        )}

        <ul className="usage-spans">
          {run.spans.map((span) => (
            <SpanRow key={span.id} span={span} />
          ))}
          {run.spans.length === 0 && (
            <li className="muted usage-span-empty">این اجرا پیش از اولین فراخوانی متوقف شد.</li>
          )}
        </ul>

        {run.trace_url && (
          <a className="usage-trace-link" href={run.trace_url} target="_blank" rel="noreferrer">
            مشاهده‌ی رد کامل در Langfuse ↗
          </a>
        )}
      </div>
    </details>
  );
}

function UsageBody({ usage }: { usage: ConversationUsage }) {
  const { totals, runs } = usage;

  if (totals.run_count === 0) {
    return <p className="muted">هنوز اجرایی برای این گفت‌وگو ثبت نشده است.</p>;
  }

  return (
    <>
      <dl className="usage-stats">
        <div>
          <dt>توکن ورودی</dt>
          <dd>{formatTokens(totals.input_tokens)}</dd>
        </div>
        <div>
          <dt>توکن خروجی</dt>
          <dd>{formatTokens(totals.output_tokens)}</dd>
        </div>
        <div>
          <dt>فراخوانی مدل</dt>
          <dd>{totals.llm_call_count}</dd>
        </div>
        <div>
          <dt>فراخوانی ابزار</dt>
          <dd>{totals.tool_call_count}</dd>
        </div>
        <div>
          <dt>اعتبار Tavily</dt>
          <dd>
            {totals.tavily_credits}
            {totals.tavily_credits > 0 && (
              <span className="usage-subvalue"> ({formatUsd(totals.tavily_cost_usd)})</span>
            )}
          </dd>
        </div>
        <div>
          <dt>زمان اجرا</dt>
          <dd>{formatDuration(totals.total_duration_ms)}</dd>
        </div>
      </dl>

      <p className={`usage-source usage-source--${totals.token_source}`}>
        <span className="usage-source-badge">{TOKEN_SOURCE_LABEL[totals.token_source]}</span>
        {/* The brief rejects "it was free" as an answer, so the UI
            states plainly what this number is: list-price cost of the
            tokens actually consumed, not an amount billed. */}
        <span className="usage-source-note">
          هزینه بر پایه‌ی نرخ‌نامه‌ی مدل محاسبه شده است؛ روی پلن رایگان مبلغی دریافت
          نمی‌شود، اما مصرف واقعی همین است.
        </span>
      </p>

      {totals.failed_run_count > 0 && (
        <p className="usage-failed-note">
          {totals.failed_run_count} اجرای ناموفق هم در این مجموع حساب شده است — توکن
          مصرف‌شده‌ی آن‌ها هم واقعی بوده است.
        </p>
      )}

      <div className="usage-runs">
        {runs.map((run, index) => (
          <RunBlock key={run.id} run={run} index={index} />
        ))}
      </div>
    </>
  );
}

export default function UsagePanel({
  conversationId,
  refreshToken,
  showDetails = false,
}: UsagePanelProps) {
  const [usage, setUsage] = useState<ConversationUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetchConversationUsage(conversationId);
      setUsage(data);
      setError(null);
    } catch {
      setError("بارگذاری گزارش هزینه ناموفق بود.");
    }
  }, [conversationId]);

  useEffect(() => {
    void load();
  }, [load, refreshToken]);

  // Collapse again when the panel is pointed at a different
  // conversation, so opening another order's chat never starts with a
  // half-height message list.
  useEffect(() => {
    setExpanded(false);
  }, [conversationId]);

  // The headline is the one thing section 7 requires to be visible, so
  // it stays in the header whether the panel is open or closed.
  let headline = "…";
  if (error) headline = "—";
  else if (usage) headline = formatUsd(usage.totals.total_cost_usd);

  const isOpen = showDetails && expanded;

  return (
    <section className={`usage-panel ${isOpen ? "usage-panel--open" : ""}`}>
      <button
        type="button"
        className="usage-panel-toggle"
        onClick={showDetails ? () => setExpanded((open) => !open) : undefined}
        aria-expanded={showDetails ? isOpen : undefined}
        aria-controls={showDetails ? "usage-panel-body" : undefined}
        disabled={!showDetails}
      >
        {showDetails && (
          <span className="usage-caret" aria-hidden="true">
            ▾
          </span>
        )}
        <span className="usage-panel-title">هزینه و رد اجرا</span>
        {usage && usage.totals.run_count > 0 && (
          <span className="usage-panel-hint">
            {usage.totals.run_count} نوبت · {formatTokens(usage.totals.total_tokens)} توکن
          </span>
        )}
        <span className="usage-total">{headline}</span>
      </button>

      {isOpen && (
        <div className="usage-panel-body" id="usage-panel-body">
          {error && <p className="form-error">{error}</p>}
          {!error && !usage && <p className="muted">در حال بارگذاری...</p>}
          {!error && usage && <UsageBody usage={usage} />}
        </div>
      )}
    </section>
  );
}
