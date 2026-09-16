import { useEffect, useState } from "react";
import { fetchOrderDetail } from "../api/orders";
import { fetchOrCreateConversation } from "../api/conversations";
import { useAuth } from "../auth/AuthContext";
import ChatPanel from "../components/ChatPanel";
import RefundPanel from "../components/RefundPanel";
import type { Conversation, Order, OrderStatus } from "../types/domain";

const STATUS_LABELS: Record<OrderStatus, string> = {
  pending: "در انتظار",
  processing: "در حال پردازش",
  shipped: "ارسال‌شده",
  delivered: "تحویل‌شده",
  cancelled: "لغوشده",
};

function formatPrice(cents: number): string {
  return (cents / 100).toLocaleString("fa-IR", { style: "currency", currency: "USD" });
}

interface OrderDetailPageProps {
  orderId: string;
  onBack: () => void;
}

/**
 * The order detail page: order info at the top, refund history below
 * it, and the support chat - the layout the reference course itself
 * uses (chat mounted next to one order's detail view), rebuilt here
 * in React/TypeScript instead of Django templates, and backed by the
 * event-driven pipeline (Parts 3-4) instead of an in-process agent
 * call.
 *
 * The conversation is fetched (or created, on first visit) once, up
 * front - `ChatPanel` is only ever mounted once we know the
 * conversation's id AND its correlation_id, since the SSE connection
 * is keyed on the latter.
 */
export default function OrderDetailPage({ orderId, onBack }: OrderDetailPageProps) {
  const { user } = useAuth();
  const isSupportAgent = user?.role === "support_agent";
  const [order, setOrder] = useState<Order | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [orderData, conversationData] = await Promise.all([
          fetchOrderDetail(orderId),
          fetchOrCreateConversation(orderId),
        ]);
        if (!cancelled) {
          setOrder(orderData);
          setConversation(conversationData);
        }
      } catch {
        if (!cancelled) setError("بارگذاری این سفارش ناموفق بود.");
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [orderId]);

  return (
    <div className="page">
      <button type="button" className="secondary back-button" onClick={onBack}>
        ← بازگشت به سفارش‌ها
      </button>

      {error && <p className="form-error">{error}</p>}

      {!order && !error && <p className="muted">در حال بارگذاری...</p>}

      {order && (
        <div className="order-detail-layout">
          <div className="order-detail-main">
            <header className="page-header">
              <div>
                <h1>{order.product.name}</h1>
                <p className="page-subtitle">سفارش #{order.id.slice(0, 8)}</p>
              </div>
              <span className={`status-badge status-${order.status}`}>
                {STATUS_LABELS[order.status]}
              </span>
            </header>

            <dl className="order-facts">
              <div>
                <dt>تعداد</dt>
                <dd>{order.quantity}</dd>
              </div>
              <div>
                <dt>مبلغ کل</dt>
                <dd>{formatPrice(order.total_cents)}</dd>
              </div>
              <div>
                <dt>کد رهگیری</dt>
                <dd>{order.tracking_number || "هنوز ارسال نشده"}</dd>
              </div>
              <div>
                <dt>تاریخ ثبت</dt>
                <dd>{new Date(order.created_at).toLocaleDateString("fa-IR")}</dd>
              </div>
            </dl>

            <RefundPanel orderId={order.id} />
          </div>

          <div className="order-detail-chat">
            {conversation ? (
              <ChatPanel
                conversationId={conversation.id}
                correlationId={conversation.correlation_id}
                readOnly={isSupportAgent}
                showToolDetails={isSupportAgent}
              />
            ) : (
              !error && <p className="muted">در حال آماده‌سازی گفت‌وگو...</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
