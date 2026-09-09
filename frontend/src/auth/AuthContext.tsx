import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { fetchMe, login as loginRequest, logout as logoutRequest } from "../api/auth";
import { isLoggedIn as hasStoredToken } from "../api/client";
import type { Me } from "../types/domain";

type AuthStatus = "checking" | "authed" | "anonymous";

interface AuthContextValue {
  user: Me | null;
  status: AuthStatus;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * Holds "who is logged in" for the whole app. Deliberately thin: it does
 * not decide what a user is allowed to see (that's the backend's job,
 * enforced in orders/services.py) - it only tracks whether we have a
 * valid session and who it belongs to, so the UI can greet the user and
 * hide the login page once authenticated.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Me | null>(null);
  const [status, setStatus] = useState<AuthStatus>("checking");

  const loadUser = useCallback(async () => {
    if (!hasStoredToken()) {
      setStatus("anonymous");
      return;
    }
    try {
      const me = await fetchMe();
      setUser(me);
      setStatus("authed");
    } catch {
      setUser(null);
      setStatus("anonymous");
    }
  }, []);

  useEffect(() => {
    void loadUser();
  }, [loadUser]);

  const login = useCallback(
    async (username: string, password: string) => {
      await loginRequest(username, password);
      await loadUser();
    },
    [loadUser]
  );

  const logout = useCallback(() => {
    logoutRequest();
    setUser(null);
    setStatus("anonymous");
  }, []);

  return (
    <AuthContext.Provider value={{ user, status, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
