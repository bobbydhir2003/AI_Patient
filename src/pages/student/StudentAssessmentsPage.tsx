import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { fetchMySessions, type SessionSummary } from "../../services/authApi";
import { ApiError } from "../../services/api";
import { useCaseCatalog } from "../../services/cases";
import type { PatientCase } from "../../types/case";
import { LevelBadge } from "../../portal/ui";
import { EmptyState, ErrorState, Spinner } from "../../portal/ui";
import styles from "./StudentDashboardPage.module.css";

/** The student's completed assessments (bottom-nav "Assessments"). Real data:
 * sessions with an assessment ready; taps open the existing assessment view. */
export function StudentAssessmentsPage() {
  const { token } = useAuth();
  const navigate = useNavigate();
  const { catalog } = useCaseCatalog();
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    setError(null);
    fetchMySessions(token)
      .then(setSessions)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load your assessments."));
  }, [token]);

  const caseById = useMemo(() => {
    const map = new Map<string, PatientCase>();
    catalog?.sections.forEach((s) => s.cases.forEach((c) => map.set(c.id, c)));
    return map;
  }, [catalog]);

  const withAssessment = useMemo(
    () => (sessions ?? [])
      .filter((s) => s.hasAssessment)
      .sort((a, b) => new Date(b.completedAt ?? b.startedAt).getTime() - new Date(a.completedAt ?? a.startedAt).getTime()),
    [sessions],
  );

  return (
    <div className={styles.wrap}>
      <div className={styles.head}>
        <div>
          <h1 className={styles.welcome}>Assessments</h1>
          <p className={styles.welcomeSub}>Review feedback from your completed interviews.</p>
        </div>
      </div>

      <div className={styles.sideCard}>
        {error ? (
          <ErrorState message={error} />
        ) : sessions === null ? (
          <Spinner label="Loading assessments…" />
        ) : withAssessment.length === 0 ? (
          <EmptyState title="No assessments yet" hint="Complete a patient interview to receive feedback." />
        ) : (
          withAssessment.map((s) => {
            const c = caseById.get(s.caseId);
            return (
              <button key={s.sessionId} type="button" className={styles.activityItem}
                onClick={() => navigate(`/student/sessions/${s.sessionId}/assessment`)}>
                <span className={styles.activityMain}>
                  <strong>{c?.name ?? s.caseId}</strong>
                  <span>{new Date(s.completedAt ?? s.startedAt).toLocaleDateString()}</span>
                </span>
                <LevelBadge level={s.overallLevel} />
                <span className={styles.chevron}>›</span>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
