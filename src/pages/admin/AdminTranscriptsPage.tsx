import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { fetchAdminSessions, type Paginated, type SessionSummary } from "../../services/authApi";
import { ApiError } from "../../services/api";
import { EmptyState, ErrorState, LoadingState, StatusBadge } from "../../portal/ui";
import { caseLabel, fmtDateTime } from "../../portal/format";
import { IconAssessments, IconClipboard, IconProfile } from "../../components/admin/icons";

const CASES = ["camden", "carly", "sofia", "jayden"];

export function AdminTranscriptsPage() {
  const { token } = useAuth();
  const navigate = useNavigate();
  const [caseId, setCaseId] = useState("");
  const [text, setText] = useState("");
  const [date, setDate] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Paginated<SessionSummary> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    setError(null);
    setData(null);
    fetchAdminSessions(token, { caseId, status: "all", sort: "newest", page, pageSize: 20 })
      .then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load transcripts."));
  }, [token, caseId, page]);

  useEffect(() => setPage(1), [caseId]);

  // Practical client-side filtering over the loaded page (student text + date).
  const rows = useMemo(() => {
    if (!data) return [];
    const q = text.trim().toLowerCase();
    return data.items.filter((s) => {
      const matchesText =
        !q ||
        s.studentName.toLowerCase().includes(q) ||
        caseLabel(s.caseId).toLowerCase().includes(q);
      const matchesDate = !date || (s.startedAt ?? "").slice(0, 10) === date;
      return matchesText && matchesDate;
    });
  }, [data, text, date]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1;

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">Transcripts</h1>
          <p className="pt-page-sub">Open the complete conversation for any interview session.</p>
        </div>
      </div>

      <div className="pt-toolbar">
        <input
          className="pt-input"
          placeholder="Search student or case…"
          aria-label="Search student or case"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <select className="pt-select" value={caseId} onChange={(e) => setCaseId(e.target.value)} aria-label="Filter by case">
          <option value="">All cases</option>
          {CASES.map((c) => (
            <option key={c} value={c}>
              {caseLabel(c)}
            </option>
          ))}
        </select>
        <input
          className="pt-input"
          type="date"
          aria-label="Filter by date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
        />
        {(text || date) && (
          <button
            className="pt-btn pt-btn-secondary pt-btn-sm"
            onClick={() => {
              setText("");
              setDate("");
            }}
          >
            Clear
          </button>
        )}
      </div>

      {error && <ErrorState message={error} />}
      {!error && data === null && <LoadingState label="Loading transcripts…" />}
      {!error && data !== null && rows.length === 0 && (
        <EmptyState title="No transcripts found" hint="Try adjusting your search or filters." />
      )}
      {!error && data !== null && rows.length > 0 && (
        <>
          <div className="pt-table-wrap">
            <table className="pt-table">
              <thead>
                <tr>
                  <th scope="col">Student</th>
                  <th scope="col">ID</th>
                  <th scope="col">Case</th>
                  <th scope="col">Session date</th>
                  <th scope="col">Messages</th>
                  <th scope="col">Status</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((s) => (
                  <tr key={s.sessionId}>
                    <td>{s.studentName}</td>
                    <td className="pt-muted">{s.studentId.slice(0, 8)}</td>
                    <td>{caseLabel(s.caseId)}</td>
                    <td>{fmtDateTime(s.startedAt)}</td>
                    <td>{s.turnCount}</td>
                    <td>
                      <StatusBadge status={s.status} />
                    </td>
                    <td>
                      <div className="pt-actions-cell">
                        <button
                          className="pt-icon-btn"
                          title="Open transcript"
                          aria-label={`Open transcript for ${s.studentName}`}
                          onClick={() => navigate(`/admin/sessions/${s.sessionId}?tab=transcript`)}
                        >
                          <IconClipboard width={16} height={16} />
                        </button>
                        <button
                          className="pt-icon-btn"
                          title="Jump to assessment"
                          aria-label={`Jump to assessment for ${s.studentName}`}
                          disabled={!s.hasAssessment}
                          style={s.hasAssessment ? undefined : { opacity: 0.4, cursor: "not-allowed" }}
                          onClick={() => navigate(`/admin/sessions/${s.sessionId}?tab=assessment`)}
                        >
                          <IconAssessments width={16} height={16} />
                        </button>
                        <button
                          className="pt-icon-btn"
                          title="Jump to student"
                          aria-label={`Open student ${s.studentName}`}
                          onClick={() => navigate(`/admin/students/${s.studentId}`)}
                        >
                          <IconProfile width={16} height={16} />
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
              Showing {rows.length} of {data.total} session{data.total === 1 ? "" : "s"} · page{" "}
              {data.page} of {totalPages}
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
