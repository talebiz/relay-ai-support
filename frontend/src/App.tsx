import { useState } from "react";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import LoginPage from "./pages/LoginPage";
import OrdersPage from "./pages/OrdersPage";
import OrderDetailPage from "./pages/OrderDetailPage";
import DocumentsPage from "./pages/DocumentsPage";
import "./App.css";

/**
 * Simple state-based navigation between exactly three authenticated
 * screens plus the login screen, matching the project brief's
 * requirement (section 6): "رابط کاربری فقط سه صفحه لازم دارد: ورود،
 * گفت‌وگو، اسناد" - login, conversation (order detail with chat),
 * and documents.
 *
 * Part 5 adds the DocumentsPage as the third screen.
 */

type Screen = "orders" | "order-detail" | "documents";

function AuthenticatedApp() {
  const [screen, setScreen] = useState<Screen>("orders");
  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null);

  function goToOrder(orderId: string) {
    setSelectedOrderId(orderId);
    setScreen("order-detail");
  }

  function goToOrders() {
    setSelectedOrderId(null);
    setScreen("orders");
  }

  function goToDocuments() {
    setScreen("documents");
  }

  if (screen === "order-detail" && selectedOrderId) {
    return <OrderDetailPage orderId={selectedOrderId} onBack={goToOrders} />;
  }

  if (screen === "documents") {
    return <DocumentsPage onBack={goToOrders} />;
  }

  return <OrdersPage onSelectOrder={goToOrder} onGoToDocuments={goToDocuments} />;
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
