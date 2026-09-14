import { useEffect, useState, useRef, type FormEvent } from "react";
import { fetchDocuments, uploadDocument, deleteDocument } from "../api/documents";
import { useAuth } from "../auth/AuthContext";
import type { DocumentInfo, DocumentStatus } from "../types/domain";

const STATUS_LABELS: Record<DocumentStatus, string> = {
  pending: "در انتظار ایندکس",
  indexed: "ایندکس‌شده",
  failed: "خطا در ایندکس",
};

interface DocumentsPageProps {
  onBack: () => void;
}

/**
 * The third page the project brief requires (section 6):
 * "رابط کاربری فقط سه صفحه لازم دارد: ورود، گفت‌وگو، اسناد"
 *
 * Authorization in the UI mirrors the backend:
 *   - All authenticated users can VIEW the document list
 *   - Only support agents see the upload form and delete buttons
 *
 * This is a UI convenience only - the backend enforces the real
 * authorization (IsSupportAgent on POST/DELETE). A customer who
 * somehow bypassed the UI would still get a 403 from the API.
 */
export default function DocumentsPage({ onBack }: DocumentsPageProps) {
  const { user } = useAuth();
  const isSupportAgent = user?.role === "support_agent";

  const [documents, setDocuments] = useState<DocumentInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Upload form state (only relevant for support agents)
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadSuccess, setUploadSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Delete state
  const [deleting, setDeleting] = useState<string | null>(null);

  async function loadDocuments() {
    try {
      const data = await fetchDocuments();
      setDocuments(data);
      setError(null);
    } catch {
      setError("بارگذاری لیست اسناد ناموفق بود.");
    }
  }

  useEffect(() => {
    void loadDocuments();
  }, []);

  async function handleUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const file = fileInputRef.current?.files?.[0];
    if (!file) {
      setUploadError("لطفاً یک فایل متنی انتخاب کنید.");
      return;
    }

    setUploading(true);
    setUploadError(null);
    setUploadSuccess(null);

    try {
      const doc = await uploadDocument(file, uploadTitle);
      setUploadSuccess(
        doc.status === "indexed"
          ? `«${doc.title}» با ${doc.chunk_count} بخش ایندکس شد.`
          : `«${doc.title}» آپلود شد اما ایندکس نشد: ${doc.error_message}`
      );
      setUploadTitle("");
      if (fileInputRef.current) fileInputRef.current.value = "";
      await loadDocuments();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "آپلود ناموفق بود.");
    } finally {
      setUploading(false);
    }
  }

  async function handleDelete(documentId: string) {
    if (!confirm("آیا از حذف این سند مطمئن هستید؟")) return;

    setDeleting(documentId);
    try {
      await deleteDocument(documentId);
      await loadDocuments();
    } catch {
      setError("حذف سند ناموفق بود.");
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="page">
      <button type="button" className="secondary back-button" onClick={onBack}>
        ← بازگشت به سفارش‌ها
      </button>

      <header className="page-header">
        <div>
          <h1>اسناد شرکت</h1>
          <p className="page-subtitle">
            اسنادی که عامل هوشمند از آنها برای پاسخ‌دهی استفاده می‌کند
          </p>
        </div>
      </header>

      {/* Upload form — support agents only */}
      {isSupportAgent && (
        <section className="upload-panel">
          <h3>آپلود سند جدید</h3>
          <form className="upload-form" onSubmit={handleUpload}>
            <label className="field">
              <span>عنوان (اختیاری)</span>
              <input
                type="text"
                value={uploadTitle}
                onChange={(e) => setUploadTitle(e.target.value)}
                placeholder="مثلاً: سیاست بازگشت کالا"
                disabled={uploading}
              />
            </label>
            <label className="field">
              <span>فایل متنی (.txt, .md, .csv)</span>
              <input
                type="file"
                ref={fileInputRef}
                accept=".txt,.md,.csv"
                disabled={uploading}
              />
            </label>
            {uploadError && <p className="form-error">{uploadError}</p>}
            {uploadSuccess && <p className="form-success">{uploadSuccess}</p>}
            <button type="submit" disabled={uploading}>
              {uploading ? "در حال آپلود..." : "آپلود و ایندکس"}
            </button>
          </form>
        </section>
      )}

      {/* Document list */}
      {error && <p className="form-error">{error}</p>}
      {documents === null && !error && <p className="muted">در حال بارگذاری...</p>}

      {documents && documents.length === 0 && (
        <p className="muted">
          هنوز سندی آپلود نشده است.
          {isSupportAgent && " برای شروع، یک فایل متنی آپلود کنید."}
        </p>
      )}

      {documents && documents.length > 0 && (
        <table className="orders-table">
          <thead>
            <tr>
              <th>عنوان</th>
              <th>فایل</th>
              <th>وضعیت</th>
              <th>تعداد بخش‌ها</th>
              <th>آپلود‌کننده</th>
              <th>تاریخ</th>
              {/* Delete column only visible to support agents */}
              {isSupportAgent && <th></th>}
            </tr>
          </thead>
          <tbody>
            {documents.map((doc) => (
              <tr key={doc.id}>
                <td>{doc.title}</td>
                <td style={{ fontSize: "12px", fontFamily: "monospace" }}>
                  {doc.original_filename}
                </td>
                <td>
                  <span className={`status-badge status-${doc.status}`}>
                    {STATUS_LABELS[doc.status] ?? doc.status}
                  </span>
                </td>
                <td>{doc.chunk_count}</td>
                <td>{doc.uploaded_by_username ?? "—"}</td>
                <td>{new Date(doc.created_at).toLocaleDateString("fa-IR")}</td>
                {isSupportAgent && (
                  <td>
                    <button
                      className="secondary"
                      style={{ padding: "4px 10px", fontSize: "12px" }}
                      onClick={() => handleDelete(doc.id)}
                      disabled={deleting === doc.id}
                    >
                      {deleting === doc.id ? "..." : "حذف"}
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}