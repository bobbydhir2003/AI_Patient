import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { fetchMySessions, type SessionSummary } from "../../services/authApi";
import { ApiError } from "../../services/api";
import { useCaseCatalog } from "../../services/cases";
import type { PatientCase } from "../../types/case";
import { EmptyState, ErrorState, Spinner } from "../../portal/ui";
import styles from "./StudentDashboardPage.module.css";

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  const m = Math.floor(s / 60);
  if (s < 60) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} hr${h > 1 ? "s" : ""} ago`;
  return `${Math.floor(h / 24)} day${Math.floor(h / 24) > 1 ? "s" : ""} ago`;
}

/** Full activity history for the student (bottom-nav "Activity"). Real sessions
 * from GET /students/me/sessions; taps open the existing session detail page. */
export function StudentActivityPage() {
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
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load your activity."));
  }, [token]);

  const caseById = useMemo(() => {
    const map = new Map<string, PatientCase>();
    catalog?.sections.forEach((s) => s.cases.forEach((c) => map.set(c.id, c)));
    return map;
  }, [catalog]);

  const sorted = useMemo(
    () => (sessions ? [...sessions].sort((a, b) => new Date(b.completedAt ?? b.startedAt).getTime() - new Date(a.completedAt ?? a.startedAt).getTime()) : []),
    [sessions],
  );

  function label(s: SessionSummary): string {
    if (s.status === "active" && !s.locked) return "Interview in progress";
    if (s.hasAssessment) return "Assessment available";
    if (s.status === "completed") return "Interview completed";
    return "Interview";
  }

  return (
    <div className={styles.wrap}>
      <div className={styles.head}>
        <div>
          <h1 className={styles.welcome}>Activity</h1>
          <p className={styles.welcomeSub}>Your interview sessions and assessments.</p>
        </div>
      </div>

      <div className={styles.sideCard}>
        {error ? (
          <ErrorState message={error} />
        ) : sessions === null ? (
          <Spinner label="Loading activity…" />
        ) : sorted.length === 0 ? (
          <EmptyState title="No activity yet" hint="Start a patient case to see it here." />
        ) : (
          sorted.map((s) => {
            const c = caseById.get(s.caseId);
            return (
              <button key={s.sessionId} type="button" className={styles.activityItem}
                onClick={() => navigate(`/student/sessions/${s.sessionId}`)}>
                <span className={styles.activityMain}>
                  <strong>{(c?.name ?? s.caseId)} — {label(s)}</strong>
                  <span>{timeAgo(s.completedAt ?? s.startedAt)}</span>
                </span>
                <span className={styles.chevron}>›</span>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
