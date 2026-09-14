import { apiFetch } from "./client";
import type { DocumentInfo } from "../types/domain";

export function fetchDocuments(): Promise<DocumentInfo[]> {
  return apiFetch<DocumentInfo[]>("/api/documents/");
}

export async function uploadDocument(file: File, title: string): Promise<DocumentInfo> {
  const formData = new FormData();
  formData.append("file", file);
  if (title.trim()) {
    formData.append("title", title.trim());
  }

  // apiFetch always sets Content-Type to application/json, but for
  // multipart/form-data the browser must set it (with the boundary).
  // So we use a raw fetch here, reusing the token logic from client.ts.
  const { getAccessToken } = await import("./client");
  const { API_BASE } = await import("./client");
  const token = getAccessToken();

  const res = await fetch(`${API_BASE}/api/documents/`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  });

  if (!res.ok) {
    const data = await res.json().catch(() => null);
    const detail = (data as Record<string, string> | null)?.detail ?? "Upload failed.";
    throw new Error(detail);
  }

  return (await res.json()) as DocumentInfo;
}

export async function deleteDocument(documentId: string): Promise<void> {
  await apiFetch(`/api/documents/${documentId}/`, { method: "DELETE" });
}
