import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { useAppContext } from "../../state/AppContext";
import { fetchMySessions, type SessionSummary } from "../../services/authApi";
import { ApiError } from "../../services/api";
import { EmptyState, ErrorState, Spinner, StatusBadge } from "../../portal/ui";
import { caseLabel, fmtDateTime, fmtDuration } from "../../portal/format";

export function StudentDashboardPage() {
  const { token, user, logout } = useAuth();
  const navigate = useNavigate();
  const { setStudentName, setStudentId, setActiveInterview } = useAppContext();
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  function load() {
    if (!token) return;
    setError(null);
    setSessions(null);
    fetchMySessions(token)
      .then(setSessions)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load your sessions."));
  }

  useEffect(load, [token]);

  function continueSession(s: SessionSummary) {
    // Re-bind the existing interview workflow to this session and resume it.
    setStudentName(user?.fullName ?? "");
    setStudentId(user?.studentNumber ?? "");
    setActiveInterview({ caseId: s.caseId, sessionId: s.sessionId, startedAt: Date.now() });
    navigate(`/interview/${s.caseId}`);
  }

  return (
    <div className="pt-portal">
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">My dashboard</h1>
          <p className="pt-muted" style={{ margin: 0 }}>
            {user?.fullName} · {user?.email}
            {user?.studentNumber ? ` · #${user.studentNumber}` : ""}
          </p>
        </div>
        <div className="pt-row">
          <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => navigate("/cases")}>
            Start a new interview
          </button>
          <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={logout}>
            Log out
          </button>
        </div>
      </div>

      <div className="pt-cards">
        <div className="pt-stat">
          <div className="num">{sessions?.length ?? "—"}</div>
          <div className="lbl">Total sessions</div>
        </div>
        <div className="pt-stat">
          <div className="num">{sessions?.filter((s) => s.status === "completed").length ?? "—"}</div>
          <div className="lbl">Completed</div>
        </div>
        <div className="pt-stat">
          <div className="num">{sessions?.filter((s) => s.hasAssessment).length ?? "—"}</div>
          <div className="lbl">Assessments</div>
        </div>
      </div>

      <h2 className="pt-h2">My interview sessions</h2>
      {error && <ErrorState message={error} onRetry={load} />}
      {!error && sessions === null && <Spinner label="Loading your sessions…" />}
      {!error && sessions !== null && sessions.length === 0 && (
        <EmptyState title="No sessions yet" hint="Start a new patient interview to see it here." />
      )}
      {!error && sessions !== null && sessions.length > 0 && (
        <div className="pt-table-wrap">
          <table className="pt-table">
            <thead>
              <tr>
                <th>Patient case</th>
                <th>Status</th>
                <th>Date</th>
                <th>Duration</th>
                <th>Assessment</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.sessionId}>
                  <td>{caseLabel(s.caseId)}</td>
                  <td><StatusBadge status={s.status} /></td>
                  <td>{fmtDateTime(s.startedAt)}</td>
                  <td>{fmtDuration(s.durationSeconds)}</td>
                  <td>
                    {s.hasAssessment ? (
                      <span className="pt-badge pt-badge-green">Available</span>
                    ) : (
                      <span className="pt-muted">—</span>
                    )}
                  </td>
                  <td>
                    <div className="pt-row">
                      <button
                        className="pt-btn pt-btn-secondary pt-btn-sm"
                        onClick={() => navigate(`/student/sessions/${s.sessionId}`)}
                      >
                        Open
                      </button>
                      {s.status === "active" && !s.locked && (
                        <button className="pt-btn pt-btn-sm" onClick={() => continueSession(s)}>
                          Continue
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
