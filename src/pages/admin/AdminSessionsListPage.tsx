import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { fetchAdminSessions, type Paginated, type SessionSummary } from "../../services/authApi";
import { ApiError } from "../../services/api";
import { EmptyState, ErrorState, Spinner, StatusBadge } from "../../portal/ui";
import { caseLabel, fmtDateTime, fmtDuration } from "../../portal/format";

const CASES = ["camden", "carly", "sofia", "jayden"];

export function AdminSessionsListPage() {
  const { token } = useAuth();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();

  const [caseId, setCaseId] = useState(params.get("case") ?? "");
  const [status, setStatus] = useState(params.get("status") ?? "all");
  const [sort, setSort] = useState("newest");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Paginated<SessionSummary> | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Extra client-side views driven by dashboard deep-links.
  const staleOnly = params.get("stale") === "1";
  const unassessedOnly = params.get("assessment") === "none";

  useEffect(() => {
    if (!token) return;
    setError(null);
    setData(null);
    fetchAdminSessions(token, { caseId, status, sort, page, pageSize: 20 })
      .then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load sessions."));
  }, [token, caseId, status, sort, page]);

  useEffect(() => setPage(1), [caseId, status, sort]);

  // Keep the URL in sync so filters are shareable and the sidebar stays honest.
  useEffect(() => {
    const next = new URLSearchParams(params);
    if (caseId) next.set("case", caseId);
    else next.delete("case");
    if (status && status !== "all") next.set("status", status);
    else next.delete("status");
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId, status]);

  const rows = useMemo(() => {
    if (!data) return [];
    const dayAgo = Date.now() - 24 * 60 * 60 * 1000;
    return data.items.filter((s) => {
      if (unassessedOnly && (s.hasAssessment || s.status !== "completed")) return false;
      if (staleOnly) {
        const started = new Date(s.startedAt).getTime();
        if (!(s.status === "active" && started < dayAgo)) return false;
      }
      return true;
    });
  }, [data, staleOnly, unassessedOnly]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1;
  const activeExtra = staleOnly || unassessedOnly;

  return (
    <div>
      <div className="pt-page-header">
        <h1 className="pt-h1">Sessions</h1>
      </div>

      <div className="pt-toolbar">
        <select className="pt-select" value={caseId} onChange={(e) => setCaseId(e.target.value)}>
          <option value="">All cases</option>
          {CASES.map((c) => (
            <option key={c} value={c}>{caseLabel(c)}</option>
          ))}
        </select>
        <select className="pt-select" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="all">All statuses</option>
          <option value="completed">Completed</option>
          <option value="active">Active</option>
          <option value="archived">Archived</option>
        </select>
        <select className="pt-select" value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </select>
        {activeExtra && (
          <button
            className="pt-btn pt-btn-secondary pt-btn-sm"
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("stale");
              next.delete("assessment");
              setParams(next, { replace: true });
            }}
          >
            {staleOnly ? "Showing only sessions active > 24h" : "Showing only unassessed"} · Clear
          </button>
        )}
      </div>

      {error && <ErrorState message={error} />}
      {!error && data === null && <Spinner label="Loading sessions…" />}
      {!error && data !== null && rows.length === 0 && (
        <EmptyState title="No sessions found" hint="Try changing the filters." />
      )}
      {!error && data !== null && rows.length > 0 && (
        <>
          <div className="pt-table-wrap">
            <table className="pt-table">
              <thead>
                <tr>
                  <th>Student</th>
                  <th>Case</th>
                  <th>Status</th>
                  <th>Questions</th>
                  <th>Started</th>
                  <th>Duration</th>
                  <th>Assessment</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((s) => (
                  <tr
                    key={s.sessionId}
                    className="clickable"
                    onClick={() => navigate(`/admin/sessions/${s.sessionId}`)}
                  >
                    <td>{s.studentName}</td>
                    <td>{caseLabel(s.caseId)}</td>
                    <td><StatusBadge status={s.status} /></td>
                    <td>{s.studentTurnCount}</td>
                    <td>{fmtDateTime(s.startedAt)}</td>
                    <td>{fmtDuration(s.durationSeconds)}</td>
                    <td>{s.hasAssessment ? "Yes" : <span className="pt-muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pt-pagination">
            <span className="pt-muted">
              {data.total} session{data.total === 1 ? "" : "s"} · page {data.page} of {totalPages}
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
