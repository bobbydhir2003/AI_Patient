import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../state/AuthContext";
import { ApiError } from "../../services/api";
import { ErrorState, LoadingState } from "../../portal/ui";
import {
  approveAccessRequest,
  fetchAccessRequests,
  rejectAccessRequest,
  type AccessRequest,
} from "../../services/accessApi";

const FILTERS = ["ALL", "PENDING", "APPROVED", "REJECTED"] as const;
type Filter = (typeof FILTERS)[number];

function statusBadge(status: string) {
  const cls = { PENDING: "pt-badge-amber", APPROVED: "pt-badge-green", REJECTED: "pt-badge-red" }[status] ?? "pt-badge-gray";
  return <span className={`pt-badge ${cls}`}>{status}</span>;
}

export function AdminAccessRequestsPage() {
  const { token } = useAuth();
  const [rows, setRows] = useState<AccessRequest[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("PENDING");
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!token) return;
    setLoading(true);
    fetchAccessRequests(token, filter)
      .then((r) => { setRows(r); setError(null); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load access requests."))
      .finally(() => setLoading(false));
  }, [token, filter]);

  useEffect(() => { load(); }, [load]);

  async function act(id: string, action: "approve" | "reject") {
    setBusyId(id);
    try {
      if (action === "approve") await approveAccessRequest(token, id);
      else await rejectAccessRequest(token, id);
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Action failed.");
    } finally {
      setBusyId(null);
    }
  }

  const fmt = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>Access Requests</h1>
          <p className="pt-page-sub">Review and approve email access requests before registration.</p>
        </div>
        <div className="pt-header-actions">
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              className={`pt-btn pt-btn-sm ${filter === f ? "" : "pt-btn-secondary"}`}
              onClick={() => setFilter(f)}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {error && <p className="pt-error-text" role="alert" style={{ marginBottom: "var(--space-4)" }}>{error}</p>}

      {loading && !rows ? (
        <LoadingState label="Loading access requests…" />
      ) : error && !rows ? (
        <ErrorState message={error} onRetry={load} />
      ) : (
        <div className="pt-card">
          <div className="pt-table-wrap">
            <table className="pt-table">
              <thead>
                <tr>
                  <th>Email</th><th>Status</th><th>Requested</th>
                  <th>Reviewed By</th><th>Reviewed</th><th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows && rows.length === 0 && (
                  <tr><td colSpan={6} className="pt-muted">No {filter === "ALL" ? "" : filter.toLowerCase()} requests.</td></tr>
                )}
                {rows?.map((r) => (
                  <tr key={r.id}>
                    <td>{r.email}</td>
                    <td>{statusBadge(r.status)}</td>
                    <td>{fmt(r.requestedAt)}</td>
                    <td>{r.reviewedBy ?? "—"}</td>
                    <td>{fmt(r.reviewedAt)}</td>
                    <td>
                      {r.status !== "APPROVED" && (
                        <button type="button" className="pt-btn pt-btn-sm" disabled={busyId === r.id}
                          onClick={() => act(r.id, "approve")}>Approve</button>
                      )}{" "}
                      {r.status !== "REJECTED" && (
                        <button type="button" className="pt-btn pt-btn-sm pt-btn-danger" disabled={busyId === r.id}
                          onClick={() => act(r.id, "reject")}>Reject</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
