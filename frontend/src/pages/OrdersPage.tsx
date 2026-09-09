import { useEffect, useState } from "react";
import { fetchOrders } from "../api/orders";
import { useAuth } from "../auth/AuthContext";
import type { Order, OrderStatus } from "../types/domain";

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

export default function OrdersPage() {
  const { user, logout } = useAuth();
  const [orders, setOrders] = useState<Order[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchOrders()
      .then(setOrders)
      .catch(() => setError("بارگذاری سفارش‌ها ناموفق بود."));
  }, []);

  const isSupportAgent = user?.role === "support_agent";

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>سفارش‌ها</h1>
          {user && (
            <p className="page-subtitle">
              {isSupportAgent
                ? `${user.username} (پشتیبان) — مشاهده‌ی همه‌ی سفارش‌ها`
                : `${user.username} — فقط سفارش‌های شما`}
            </p>
          )}
        </div>
        <button className="secondary" onClick={logout}>
          خروج
        </button>
      </header>

      {error && <p className="form-error">{error}</p>}

      {orders === null && !error && <p className="muted">در حال بارگذاری...</p>}

      {orders && orders.length === 0 && <p className="muted">هنوز سفارشی ثبت نشده است.</p>}

      {orders && orders.length > 0 && (
        <table className="orders-table">
          <thead>
            <tr>
              {isSupportAgent && <th>کاربر</th>}
              <th>محصول</th>
              <th>تعداد</th>
              <th>وضعیت</th>
              <th>مبلغ</th>
              <th>تاریخ</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => (
              <tr key={order.id}>
                {isSupportAgent && <td>{order.username}</td>}
                <td>{order.product.name}</td>
                <td>{order.quantity}</td>
                <td>
                  <span className={`status-badge status-${order.status}`}>
                    {STATUS_LABELS[order.status] ?? order.status}
                  </span>
                </td>
                <td>{formatPrice(order.total_cents)}</td>
                <td>{new Date(order.created_at).toLocaleDateString("fa-IR")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
