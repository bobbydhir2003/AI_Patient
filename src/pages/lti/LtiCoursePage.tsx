import { useCallback, useEffect, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { CaseCard } from "../../components/cases/CaseCard";
import { completeSession, createSession } from "../../services/api";
import { fetchMySessions, type SessionSummary } from "../../services/authApi";
import { fetchAssignedCases, simulateInterview } from "../../services/ltiApi";
import { useAuth } from "../../state/AuthContext";
import { EmptyState, ErrorState, LevelBadge, Spinner, StatusBadge } from "../../portal/ui";
import { caseLabel, fmtDateTime } from "../../portal/format";
import type { PatientCase } from "../../types/case";
import styles from "./lti.module.css";

/** Course home for a launched student: only the cases this course offering
 * assigns, and only this course's interviews. Starting a case uses the normal
 * AI SP flow (introduction, survey, interview, assessment). */
export function LtiCoursePage() {
  const { lti } = useAuth();
  const navigate = useNavigate();
  const [cases, setCases] = useState<PatientCase[] | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [simulating, setSimulating] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!lti) return;
    setError(null);
    try {
      const [assigned, mine] = await Promise.all([
        fetchAssignedCases(lti.token),
        fetchMySessions(lti.token),
      ]);
      setCases(assigned);
      setSessions(mine);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load your course.");
    }
  }, [lti]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!lti) return <Navigate to="/lti/ended?reason=expired" replace />;
  if (lti.role !== "learner") return <Navigate to="/course/instructor" replace />;

  async function runSimulated(caseId: string) {
    if (!lti) return;
    setSimulating(caseId);
    try {
      const session = await createSession(lti.user.fullName, "", caseId);
      await simulateInterview(lti.token, session.sessionId);
      await completeSession(session.sessionId);
      navigate(`/assessment/${session.sessionId}/loading`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The simulated interview failed.");
      setSimulating(null);
    }
  }

  return (
    <div className="pt-portal">
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">{lti.context.title || "Your course"}</h1>
          <p className="pt-muted">Patient cases assigned by your course instructor.</p>
        </div>
      </div>

      {error && <ErrorState message={error} onRetry={load} />}
      {!cases && !error && <Spinner label="Loading your course…" />}
      {cases && cases.length === 0 && (
        <EmptyState
          title="No patient cases are assigned yet"
          hint="Your instructor has not assigned cases to this course. Check back later."
        />
      )}
      {cases && cases.length > 0 && (
        <div className={styles.caseList}>
          {cases.map((c) => (
            <div key={c.id}>
              <CaseCard patientCase={c} />
              {lti.devFakeAi && (
                <div className={styles.devBox}>
                  <strong>Development only:</strong> no voice service is used.{" "}
                  <button
                    type="button"
                    className="pt-btn pt-btn-secondary pt-btn-sm"
                    disabled={simulating !== null}
                    aria-label={`Run simulated interview for ${c.name}`}
                    onClick={() => void runSimulated(c.id)}
                  >
                    {simulating === c.id ? "Simulating…" : "Run simulated interview"}
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <section className={styles.section} aria-labelledby="course-history">
        <h2 id="course-history" className="pt-h2">Your interviews in this course</h2>
        {sessions.length === 0 ? (
          <p className="pt-muted">You have not started an interview in this course yet.</p>
        ) : (
          <div className="pt-table-wrap">
            <table className="pt-table">
              <thead>
                <tr>
                  <th>Patient</th>
                  <th>Status</th>
                  <th>Assessment</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => (
                  <tr key={s.sessionId}>
                    <td>
                      <Link to={`/student/sessions/${s.sessionId}`}>{caseLabel(s.caseId)}</Link>
                    </td>
                    <td><StatusBadge status={s.status} /></td>
                    <td>{s.overallLevel ? <LevelBadge level={s.overallLevel} /> : "—"}</td>
                    <td>{fmtDateTime(s.startedAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
