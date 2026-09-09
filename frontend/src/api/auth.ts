import { apiFetch, setTokens, clearTokens } from "./client";
import type { Me, TokenPair } from "../types/domain";

export async function login(username: string, password: string): Promise<void> {
  const tokens = await apiFetch<TokenPair>("/api/auth/token/", {
    method: "POST",
    body: JSON.stringify({ username, password }),
    _retry: false,
  });
  setTokens(tokens);
}

export function logout(): void {
  clearTokens();
}

export function fetchMe(): Promise<Me> {
  return apiFetch<Me>("/api/auth/me/");
}
