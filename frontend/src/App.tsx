import { useState } from "react";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import LoginPage from "./pages/LoginPage";
import OrdersPage from "./pages/OrdersPage";
import OrderDetailPage from "./pages/OrderDetailPage";
import "./App.css";

/**
 * Simple state-based navigation between exactly two authenticated
 * screens (orders list, order detail with chat) plus the login
 * screen - no router library needed for three screens total, and the
 * project brief only asks for a handful of pages ("ورود، گفت‌وگو،
 * اسناد" - login, conversation, documents), not a full routing stack.
 */
function AuthenticatedApp() {
  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null);

  if (selectedOrderId) {
    return (
      <OrderDetailPage orderId={selectedOrderId} onBack={() => setSelectedOrderId(null)} />
    );
  }

  return <OrdersPage onSelectOrder={setSelectedOrderId} />;
}

function Gate() {
  const { status } = useAuth();

  if (status === "checking") {
    return (
      <div className="auth-screen">
        <p className="muted">در حال بررسی نشست...</p>
      </div>
    );
  }

  return status === "authed" ? <AuthenticatedApp /> : <LoginPage />;
}

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}
