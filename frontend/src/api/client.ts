/**
 * Single place that knows how to talk to the Django backend.
 *
 * Why this file exists as a chokepoint (mirrors backend/orders/services.py
 * on the server side): every authenticated request in the app goes
 * through `apiFetch`, which attaches the access token and transparently
 * refreshes it on a 401. No component should ever build its own fetch()
 * with a manually copied Authorization header - if token storage or
 * refresh logic changes, it changes here once.
 */

import type { TokenPair } from "../types/domain";

const API_BASE: string =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:18080";

const ACCESS_KEY = "relay_access_token";
const REFRESH_KEY = "relay_refresh_token";

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY);
}

function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}

export function setTokens(tokens: TokenPair): void {
  localStorage.setItem(ACCESS_KEY, tokens.access);
  localStorage.setItem(REFRESH_KEY, tokens.refresh);
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

export function isLoggedIn(): boolean {
  return Boolean(getAccessToken());
}

export class ApiError extends Error {
  status: number;
  data: unknown;

  constructor(message: string, status: number, data: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

async function refreshAccessToken(): Promise<boolean> {
  const refresh = getRefreshToken();
  if (!refresh) return false;

  const res = await fetch(`${API_BASE}/api/auth/token/refresh/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh }),
  });
  if (!res.ok) return false;

  const data = (await res.json()) as { access: string; refresh?: string };
  setTokens({ access: data.access, refresh: data.refresh ?? refresh });
  return true;
}

interface ApiFetchOptions extends RequestInit {
  /** Internal flag: false on the retry attempt so we never loop forever. */
  _retry?: boolean;
}

/**
 * Fetch wrapper used by every page/component. Attaches the bearer token,
 * retries exactly once after a silent token refresh on 401, and throws
 * ApiError with the parsed body on any other failure so callers can show
 * a real message instead of a generic "something went wrong".
 *
 * `T` is the shape of the parsed JSON response, supplied by the caller
 * (see api/auth.ts, api/orders.ts) - this file has no opinion on what
 * any particular endpoint returns.
 */
export async function apiFetch<T>(
  path: string,
  options: ApiFetchOptions = {}
): Promise<T> {
  const { _retry = true, headers, ...rest } = options;
  const token = getAccessToken();

  const res = await fetch(`${API_BASE}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...headers,
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });

  if (res.status === 401 && _retry) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      return apiFetch<T>(path, { ...options, _retry: false });
    }
    clearTokens();
    throw new ApiError("Session expired.", 401, null);
  }

  if (!res.ok) {
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      // response had no JSON body; that's fine, data stays null
    }
    throw new ApiError(`Request to ${path} failed (${res.status}).`, res.status, data);
  }

  if (res.status === 204) return null as T;
  return (await res.json()) as T;
}

export { API_BASE };
