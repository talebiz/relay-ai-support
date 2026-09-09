import { AuthProvider, useAuth } from "./auth/AuthContext";
import LoginPage from "./pages/LoginPage";
import OrdersPage from "./pages/OrdersPage";
import "./App.css";

function Gate() {
  const { status } = useAuth();

  if (status === "checking") {
    return (
      <div className="auth-screen">
        <p className="muted">در حال بررسی نشست...</p>
      </div>
    );
  }

  return status === "authed" ? <OrdersPage /> : <LoginPage />;
}

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}
