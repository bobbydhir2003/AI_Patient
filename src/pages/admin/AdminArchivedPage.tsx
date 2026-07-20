import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import {
  deleteSession,
  fetchAdminSessions,
  type Paginated,
  type SessionSummary,
} from "../../services/authApi";
import { ApiError } from "../../services/api";
import {
  ConfirmDeleteModal,
  EmptyState,
  ErrorState,
  LoadingState,
  StatusBadge,
  ToastProvider,
  useToast,
} from "../../portal/ui";
import { caseLabel, fmtDateTime, fmtDuration } from "../../portal/format";
import { IconEye } from "../../components/admin/icons";

function ArchivedInner() {
  const { token } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Paginated<SessionSummary> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [target, setTarget] = useState<SessionSummary | null>(null);
  const [busy, setBusy] = useState(false);

  function load() {
    if (!token) return;
    setError(null);
    setData(null);
    fetchAdminSessions(token, { status: "archived", sort: "newest", page, pageSize: 20 })
      .then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load archived sessions."));
  }
  useEffect(load, [token, page]);

  async function confirmDelete() {
    if (!token || !target) return;
    setBusy(true);
    try {
      await deleteSession(token, target.sessionId);
      toast.success("Session deleted.");
      setTarget(null);
      load();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Delete failed.");
    } finally {
      setBusy(false);
    }
  }

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1;

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">Archived</h1>
          <p className="pt-page-sub">Sessions that have been archived. Their data is preserved.</p>
        </div>
      </div>

      {error && <ErrorState message={error} onRetry={load} />}
      {!error && data === null && <LoadingState label="Loading archived sessions…" />}
      {!error && data !== null && data.items.length === 0 && (
        <EmptyState title="Nothing archived" hint="Archived sessions will appear here." />
      )}
      {!error && data !== null && data.items.length > 0 && (
        <>
          <div className="pt-table-wrap">
            <table className="pt-table">
              <thead>
                <tr>
                  <th scope="col">Student</th>
                  <th scope="col">Case</th>
                  <th scope="col">Status</th>
                  <th scope="col">Started</th>
                  <th scope="col">Duration</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((s) => (
                  <tr key={s.sessionId}>
                    <td>{s.studentName}</td>
                    <td>{caseLabel(s.caseId)}</td>
                    <td>
                      <StatusBadge status={s.status} />
                    </td>
                    <td>{fmtDateTime(s.startedAt)}</td>
                    <td>{fmtDuration(s.durationSeconds)}</td>
                    <td>
                      <div className="pt-actions-cell">
                        <button
                          className="pt-icon-btn"
                          title="View session"
                          aria-label={`View session for ${s.studentName}`}
                          onClick={() => navigate(`/admin/sessions/${s.sessionId}`)}
                        >
                          <IconEye width={16} height={16} />
                        </button>
                        <button
                          className="pt-btn pt-btn-danger pt-btn-sm"
                          onClick={() => setTarget(s)}
                          aria-label={`Delete session for ${s.studentName}`}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pt-pagination">
            <span className="pt-muted">
              {data.total} archived session{data.total === 1 ? "" : "s"} · page {data.page} of{" "}
              {totalPages}
            </span>
            <button className="pt-btn pt-btn-secondary pt-btn-sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              Previous
            </button>
            <button className="pt-btn pt-btn-secondary pt-btn-sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
              Next
            </button>
          </div>
        </>
      )}

      {target && (
        <ConfirmDeleteModal
          title="Permanently delete this session?"
          body={
            <>
              This will <strong>permanently delete</strong> {target.studentName}'s archived session,
              its transcript, and any assessment connected to it. This cannot be undone.
            </>
          }
          busy={busy}
          onConfirm={confirmDelete}
          onCancel={() => setTarget(null)}
        />
      )}
    </div>
  );
}

export function AdminArchivedPage() {
  // Self-contained ToastProvider so this page's toasts work even if rendered
  // outside the layout's provider tree in the future.
  return (
    <ToastProvider>
      <ArchivedInner />
    </ToastProvider>
  );
}
