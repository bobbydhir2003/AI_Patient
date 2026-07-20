import { useEffect, useState } from "react";
import { useAuth } from "../../state/AuthContext";
import { fetchAuditLogs, type AuditLogEntry, type Paginated } from "../../services/authApi";
import { ApiError } from "../../services/api";
import { EmptyState, ErrorState, Spinner } from "../../portal/ui";
import { fmtDateTime } from "../../portal/format";

const ACTION_LABEL: Record<string, string> = {
  student_archived: "Student archived",
  student_reactivated: "Student reactivated",
  student_deleted: "Student deleted",
  session_archived: "Session archived",
  session_deleted: "Session deleted",
  assessment_deleted: "Assessment deleted",
  message_deleted: "Message deleted",
};

export function AdminAuditLogPage() {
  const { token } = useAuth();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Paginated<AuditLogEntry> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    setError(null);
    setData(null);
    fetchAuditLogs(token, { page, pageSize: 25 })
      .then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load the audit log."));
  }, [token, page]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1;

  return (
    <div>
      <div className="pt-page-header">
        <h1 className="pt-h1">Audit log</h1>
      </div>

      {error && <ErrorState message={error} />}
      {!error && data === null && <Spinner label="Loading audit log…" />}
      {!error && data !== null && data.items.length === 0 && (
        <EmptyState title="No actions recorded yet" hint="Administrative actions will appear here." />
      )}
      {!error && data !== null && data.items.length > 0 && (
        <>
          <div className="pt-table-wrap">
            <table className="pt-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Admin</th>
                  <th>Action</th>
                  <th>Record</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((a) => (
                  <tr key={a.id}>
                    <td>{fmtDateTime(a.createdAt)}</td>
                    <td>{a.adminEmail}</td>
                    <td>
                      <span className="pt-badge pt-badge-gray">
                        {ACTION_LABEL[a.actionType] ?? a.actionType}
                      </span>
                    </td>
                    <td>{a.recordType}</td>
                    <td>{a.description}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pt-pagination">
            <span className="pt-muted">
              {data.total} entr{data.total === 1 ? "y" : "ies"} · page {data.page} of {totalPages}
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
    </div>
  );
}
