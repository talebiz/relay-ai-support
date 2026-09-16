import { useEffect, useState } from "react";
import { fetchOrderRefunds } from "../api/orders";
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
 * Shows this order's refund history. Filing a new refund is done
 * exclusively through the chat — Maya's `create_refund_request` tool
 * (the brief's action-tool requirement in section 2). The manual form
 * that used to live here was removed so refunds have one path, not
 * two: every refund now goes through the agent, which means every
 * refund shows up in the conversation transcript and the token/cost
 * ledger, not just in this list.
 *
 * The backend endpoint (POST /api/refunds/) still exists and is still
 * authorized through the same chokepoint, since the agent's tool uses
 * it internally — the change here is only about the customer-facing UI.
 */
export default function RefundPanel({ orderId }: RefundPanelProps) {
  const [refunds, setRefunds] = useState<RefundRequest[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        const data = await fetchOrderRefunds(orderId);
        setRefunds(data);
        setError(null);
      } catch {
        setError("بارگذاری تاریخچه‌ی بازگشت وجه ناموفق بود.");
      }
    }
    void load();
  }, [orderId]);

  return (
    <section className="refund-panel">
      <h3>درخواست‌های بازگشت وجه</h3>

      {error && <p className="form-error">{error}</p>}
      {refunds === null && !error && <p className="muted">در حال بارگذاری...</p>}
      {refunds && refunds.length === 0 && (
        <p className="muted">
          هنوز درخواست بازگشت وجهی برای این سفارش ثبت نشده است. برای ثبت درخواست جدید،
          در چت پشتیبانی از مایا کمک بگیرید.
        </p>
      )}

      {refunds && refunds.length > 0 && (
        <ul className="refund-list">
          {refunds.map((refund) => (
            <li key={refund.id} className="refund-list-item">
              <div className="refund-list-item-header">
                <span className={`status-badge status-${refund.status}`}>
                  {REFUND_STATUS_LABELS[refund.status]}
                </span>
                <span className="muted">
                  {new Date(refund.created_at).toLocaleDateString("fa-IR")}
                </span>
              </div>
              <p>{refund.reason}</p>
              {refund.resolution_note && (
                <p className="muted refund-resolution">نتیجه: {refund.resolution_note}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
