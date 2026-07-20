import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { fetchAdminSessions, type Paginated, type SessionSummary } from "../../services/authApi";
import { ApiError } from "../../services/api";
import { AssessmentLevelBadge, EmptyState, ErrorState, LoadingState } from "../../portal/ui";
import { caseLabel, fmtDateTime } from "../../portal/format";
import { IconAssessments, IconClipboard, IconProfile } from "../../components/admin/icons";

const CASES = ["camden", "carly", "sofia", "jayden"];
const LEVELS = [
  "Advanced",
  "Proficient",
  "Developing",
  "Needs Improvement",
  "Insufficient Evidence",
];

export function AdminAssessmentsPage() {
  const { token } = useAuth();
  const navigate = useNavigate();
  const [caseId, setCaseId] = useState("");
  const [level, setLevel] = useState("");
  const [category, setCategory] = useState("");
  const [text, setText] = useState("");
  const [date, setDate] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Paginated<SessionSummary> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    setError(null);
    setData(null);
    fetchAdminSessions(token, { caseId, status: "all", sort: "newest", page, pageSize: 40 })
      .then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load assessments."));
  }, [token, caseId, page]);

  useEffect(() => setPage(1), [caseId]);

  const rows = useMemo(() => {
    if (!data) return [];
    const q = text.trim().toLowerCase();
    return data.items
      .filter((s) => s.hasAssessment)
      .filter((s) => (!level ? true : s.overallLevel === level))
      .filter((s) => (!category ? true : s.caseCategory === category))
      .filter((s) => (!q ? true : s.studentName.toLowerCase().includes(q)))
      .filter((s) => (!date ? true : (s.completedAt ?? s.startedAt ?? "").slice(0, 10) === date));
  }, [data, level, category, text, date]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1;

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">Assessments</h1>
          <p className="pt-page-sub">Review AI assessment outcomes by qualitative level.</p>
        </div>
      </div>

      <div className="pt-toolbar">
        <input
          className="pt-input"
          placeholder="Search student…"
          aria-label="Search student"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <select className="pt-select" value={level} onChange={(e) => setLevel(e.target.value)} aria-label="Filter by level">
          <option value="">All levels</option>
          {LEVELS.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
        <select className="pt-select" value={caseId} onChange={(e) => setCaseId(e.target.value)} aria-label="Filter by case">
          <option value="">All cases</option>
          {CASES.map((c) => (
            <option key={c} value={c}>
              {caseLabel(c)}
            </option>
          ))}
        </select>
        <select className="pt-select" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Filter by case type">
          <option value="">All types</option>
          <option value="standard">Standard</option>
          <option value="referral">Referral</option>
        </select>
        <input
          className="pt-input"
          type="date"
          aria-label="Filter by date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
        />
      </div>

      {error && <ErrorState message={error} />}
      {!error && data === null && <LoadingState label="Loading assessments…" />}
      {!error && data !== null && rows.length === 0 && (
        <EmptyState title="No assessments found" hint="Try adjusting your filters." />
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
                  <th scope="col">Overall level</th>
                  <th scope="col">Assessment date</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((s) => (
                  <tr key={s.sessionId}>
                    <td>{s.studentName}</td>
                    <td className="pt-muted">{s.studentId.slice(0, 8)}</td>
                    <td>{caseLabel(s.caseId)}</td>
                    <td>
                      <AssessmentLevelBadge level={s.overallLevel} />
                    </td>
                    <td>{fmtDateTime(s.completedAt ?? s.startedAt)}</td>
                    <td>
                      <div className="pt-actions-cell">
                        <button
                          className="pt-icon-btn"
                          title="View assessment"
                          aria-label={`View assessment for ${s.studentName}`}
                          onClick={() => navigate(`/admin/sessions/${s.sessionId}?tab=assessment`)}
                        >
                          <IconAssessments width={16} height={16} />
                        </button>
                        <button
                          className="pt-icon-btn"
                          title="View transcript"
                          aria-label={`View transcript for ${s.studentName}`}
                          onClick={() => navigate(`/admin/sessions/${s.sessionId}?tab=transcript`)}
                        >
                          <IconClipboard width={16} height={16} />
                        </button>
                        <button
                          className="pt-icon-btn"
                          title="View student"
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
              Showing {rows.length} assessed session{rows.length === 1 ? "" : "s"} on page {data.page}{" "}
              of {totalPages}
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
