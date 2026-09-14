import { useEffect, useState, type FormEvent } from "react";
import { createRefundRequest, fetchOrderRefunds } from "../api/orders";
import type { RefundRequest, RefundStatus } from "../types/domain";

const REFUND_STATUS_LABELS: Record<RefundStatus, string> = {
  requested: "درخواست‌شده",
  approved: "تأیید‌شده",
  denied: "رد‌شده",
  refunded: "بازگردانده‌شده",
};

interface RefundPanelProps {
  orderId: string;
}

/**
 * Shows this order's refund history and a plain HTML form to file a
 * new one directly (POST /api/refunds/, backend/orders/views.py).
 *
 * This form exists SEPARATELY from the chat's create_refund_request
 * tool - the brief's action-tool requirement is about giving the
 * AGENT a real write capability, not about removing the ordinary REST
 * path a human would use directly. Both paths land in the exact same
 * orders/services.py::create_refund_request authorization chokepoint
 * on the backend, so filing a refund here or asking Maya to file it
 * in chat has identical authorization guarantees.
 */
export default function RefundPanel({ orderId }: RefundPanelProps) {
  const [refunds, setRefunds] = useState<RefundRequest[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function loadRefunds() {
    try {
      const data = await fetchOrderRefunds(orderId);
      setRefunds(data);
      setError(null);
    } catch {
      setError("بارگذاری تاریخچه‌ی بازگشت وجه ناموفق بود.");
    }
  }

  useEffect(() => {
    void loadRefunds();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderId]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = reason.trim();
    if (trimmed.length < 5) {
      setFormError("لطفاً دلیل را کمی مفصل‌تر بنویسید.");
      return;
    }

    setSubmitting(true);
    setFormError(null);
    try {
      await createRefundRequest(orderId, trimmed);
      setReason("");
      await loadRefunds();
    } catch {
      setFormError("ثبت درخواست بازگشت وجه ناموفق بود. دوباره تلاش کنید.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="refund-panel">
      <h3>درخواست‌های بازگشت وجه</h3>

      {error && <p className="form-error">{error}</p>}
      {refunds === null && !error && <p className="muted">در حال بارگذاری...</p>}
      {refunds && refunds.length === 0 && (
        <p className="muted">هنوز درخواست بازگشت وجهی برای این سفارش ثبت نشده است.</p>
      )}

      {refunds && refunds.length > 0 && (
        <ul className="refund-list">
          {refunds.map((refund) => (
            <li key={refund.id} className="refund-list-item">
              <div className="refund-list-item-header">
                <span className={`status-badge status-${refund.status}`}>
                  {REFUND_STATUS_LABELS[refund.status]}
                </span>
                <span className="muted">{new Date(refund.created_at).toLocaleDateString("fa-IR")}</span>
              </div>
              <p>{refund.reason}</p>
              {refund.resolution_note && (
                <p className="muted refund-resolution">نتیجه: {refund.resolution_note}</p>
              )}
            </li>
          ))}
        </ul>
      )}

      <form className="refund-form" onSubmit={handleSubmit}>
        <label className="field">
          <span>ثبت درخواست بازگشت وجه جدید</span>
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="چرا درخواست بازگشت وجه دارید؟"
            rows={3}
            disabled={submitting}
          />
        </label>
        {formError && <p className="form-error">{formError}</p>}
        <button type="submit" disabled={submitting}>
          {submitting ? "در حال ارسال..." : "ثبت درخواست"}
        </button>
      </form>
    </section>
  );
}
