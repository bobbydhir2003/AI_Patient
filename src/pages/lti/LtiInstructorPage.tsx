import { useCallback, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import {
  fetchCaseAssignment,
  fetchCourseSession,
  fetchCourseSessions,
  fetchCourseTranscript,
  fetchRoster,
  saveCaseAssignment,
  type CaseAssignment,
  type InstructorSessionDetail,
  type RosterMember,
} from "../../services/ltiApi";
import type { SessionSummary, TranscriptMessage } from "../../services/authApi";
import { useAuth } from "../../state/AuthContext";
import { AssessmentDisplay } from "../../portal/AssessmentDisplay";
import { TranscriptView } from "../../portal/TranscriptView";
import { ErrorState, LevelBadge, Spinner, StatusBadge } from "../../portal/ui";
import { caseLabel, fmtDateTime } from "../../portal/format";
import styles from "./lti.module.css";

type Tab = "interviews" | "roster" | "cases";

/** Course-scoped instructor workspace: this course offering only, enforced by
 * the backend (never AI SP administrator access). */
export function LtiInstructorPage() {
  const { lti } = useAuth();
  const [tab, setTab] = useState<Tab>("interviews");
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [roster, setRoster] = useState<RosterMember[] | null>(null);
  const [assignment, setAssignment] = useState<CaseAssignment | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [detail, setDetail] = useState<InstructorSessionDetail | null>(null);
  const [transcript, setTranscript] = useState<TranscriptMessage[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    if (!lti) return;
    setError(null);
    try {
      const [s, r, a] = await Promise.all([
        fetchCourseSessions(lti.token),
        fetchRoster(lti.token),
        fetchCaseAssignment(lti.token),
      ]);
      setSessions(s);
      setRoster(r);
      setAssignment(a);
      setSelected(a.assignedCaseIds);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the course.");
    }
  }, [lti]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!lti) return <Navigate to="/lti/ended?reason=expired" replace />;
  if (lti.role !== "instructor") return <Navigate to="/course" replace />;
  const token = lti.token;

  async function openSession(sessionId: string) {
    setTranscript(null);
    setError(null);
    try {
      setDetail(await fetchCourseSession(token, sessionId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the interview.");
    }
  }

  async function openTranscript(sessionId: string) {
    try {
      setTranscript(await fetchCourseTranscript(token, sessionId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the transcript.");
    }
  }

  async function saveCases() {
    setSaving(true);
    setSaved(null);
    try {
      const next = await saveCaseAssignment(token, selected);
      setAssignment(next);
      setSelected(next.assignedCaseIds);
      setSaved("Case assignment saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the case assignment.");
    } finally {
      setSaving(false);
    }
  }

  const toggle = (caseId: string) =>
    setSelected((cur) => (cur.includes(caseId) ? cur.filter((c) => c !== caseId) : [...cur, caseId]));

  return (
    <div className="pt-portal">
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">{lti.context.title || "Course"}</h1>
          <p className="pt-muted">
            Instructor view for this course offering only. Students see only the cases assigned here.
          </p>
        </div>
      </div>

      <div className="pt-cards">
        <div className="pt-stat"><div className="num">{roster?.length ?? "—"}</div><div className="lbl">Students launched</div></div>
        <div className="pt-stat"><div className="num">{sessions?.length ?? "—"}</div><div className="lbl">Interviews</div></div>
        <div className="pt-stat">
          <div className="num">{sessions ? sessions.filter((s) => s.status === "completed").length : "—"}</div>
          <div className="lbl">Completed</div>
        </div>
        <div className="pt-stat"><div className="num">{assignment?.assignedCaseIds.length ?? "—"}</div><div className="lbl">Assigned cases</div></div>
      </div>

      <div className="pt-tabs" role="tablist">
        {(["interviews", "roster", "cases"] as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            className={`pt-tab${tab === t ? " active" : ""}`}
            onClick={() => setTab(t)}
          >
            {t === "interviews" ? "Interviews" : t === "roster" ? "Roster" : "Case assignment"}
          </button>
        ))}
      </div>

      {error && <ErrorState message={error} onRetry={load} />}
      {!sessions && !error && <Spinner label="Loading course…" />}

      {tab === "interviews" && sessions && (
        <>
          {sessions.length === 0 ? (
            <p className="pt-muted">No student has started an interview in this course yet.</p>
          ) : (
            <div className="pt-table-wrap">
              <table className="pt-table">
                <thead>
                  <tr><th>Student</th><th>Patient</th><th>Status</th><th>Assessment</th><th>Started</th></tr>
                </thead>
                <tbody>
                  {sessions.map((s) => (
                    <tr key={s.sessionId}>
                      <td>
                        <button type="button" className={styles.rowButton} onClick={() => void openSession(s.sessionId)}>
                          {s.studentName}
                        </button>
                      </td>
                      <td>{caseLabel(s.caseId)}</td>
                      <td><StatusBadge status={s.status} /></td>
                      <td>{s.overallLevel ? <LevelBadge level={s.overallLevel} /> : "—"}</td>
                      <td>{fmtDateTime(s.startedAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {detail && (
            <section className={`pt-card ${styles.section}`} style={{ marginTop: 24 }} aria-label="Interview detail">
              <h2 className="pt-h2">
                {detail.session.studentName} · {caseLabel(detail.session.caseId)}
              </h2>
              <AssessmentDisplay assessment={detail.assessment} />
              {detail.transcriptsEnabled ? (
                transcript ? (
                  <TranscriptView messages={transcript} />
                ) : (
                  <button type="button" className="pt-btn pt-btn-secondary" onClick={() => void openTranscript(detail.session.sessionId)}>
                    Show transcript
                  </button>
                )
              ) : (
                <p className="pt-muted">Transcript access for course instructors is not enabled.</p>
              )}
            </section>
          )}
        </>
      )}

      {tab === "roster" && roster && (
        roster.length === 0 ? (
          <p className="pt-muted">No student has opened this course yet.</p>
        ) : (
          <div className="pt-table-wrap">
            <table className="pt-table">
              <thead>
                <tr><th>Student</th><th>Interviews</th><th>Completed</th><th>Last opened</th></tr>
              </thead>
              <tbody>
                {roster.map((m) => (
                  <tr key={m.userId}>
                    <td>{m.fullName}</td>
                    <td>{m.sessionCount}</td>
                    <td>{m.completedCount}</td>
                    <td>{fmtDateTime(m.lastLaunchAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {tab === "cases" && assignment && (
        <section className="pt-card">
          <h2 className="pt-h2">Patient cases for this course</h2>
          <fieldset style={{ border: "none", padding: 0, margin: 0 }}>
            <legend className="pt-muted">Students in this course can start only the selected cases.</legend>
            <div className={styles.checks}>
              {assignment.assignableCases.map((c) => (
                <label key={c.id} className={styles.check}>
                  <input type="checkbox" checked={selected.includes(c.id)} onChange={() => toggle(c.id)} />
                  <span>{c.name}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <button type="button" className="pt-btn" disabled={saving} onClick={() => void saveCases()}>
            {saving ? "Saving…" : "Save assignment"}
          </button>
          {saved && <span className="pt-muted" role="status" style={{ marginLeft: 12 }}>{saved}</span>}
        </section>
      )}
    </div>
  );
}
