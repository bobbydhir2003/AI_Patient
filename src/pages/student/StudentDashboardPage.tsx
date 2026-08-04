import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { useAppContext } from "../../state/AppContext";
import { fetchMySessions, type SessionSummary } from "../../services/authApi";
import { ApiError } from "../../services/api";
import { useCaseCatalog } from "../../services/cases";
import type { PatientCase } from "../../types/case";
import { CaseCard } from "../../components/cases/CaseCard";
import { AppImage } from "../../components/common/AppImage";
import { ErrorState, Spinner } from "../../portal/ui";
import styles from "./StudentDashboardPage.module.css";

function initials(name: string | undefined, email: string | undefined): string {
  const src = (name || email || "?").trim();
  const parts = src.split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || src[0]?.toUpperCase() || "?";
}

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "just now";
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} hr${h > 1 ? "s" : ""} ago`;
  const d = Math.floor(h / 24);
  return `${d} day${d > 1 ? "s" : ""} ago`;
}

const IconUsers = () => (
  <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="9" cy="8" r="3.2" /><path d="M3 20c0-3 2.7-5 6-5s6 2 6 5" /><path d="M16 4.5a3 3 0 0 1 0 6M18 20c0-2.4-1-4-3-4.6" /></svg>
);
const IconCheck = () => (
  <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M8.5 12.5l2.5 2.5 4.5-5" /></svg>
);
const IconClipboard = () => (
  <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="6" y="4" width="12" height="17" rx="2" /><path d="M9 4V3h6v1M9 10h6M9 14h4" /></svg>
);
const IconGear = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" /></svg>
);

export function StudentDashboardPage() {
  const { token, user, logout, isAdmin } = useAuth();
  const navigate = useNavigate();
  const { setStudentName, setStudentId, setActiveInterview } = useAppContext();
  const { catalog, loading: catalogLoading, error: catalogError, retry } = useCaseCatalog();
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    if (!token) return;
    setSessionsError(null);
    fetchMySessions(token)
      .then(setSessions)
      .catch((e) => setSessionsError(e instanceof ApiError ? e.message : "Could not load your sessions."));
  }, [token]);

  const standard = catalog?.sections.find((s) => s.id === "standard");
  const referral = catalog?.sections.find((s) => s.id === "referral");
  const caseById = useMemo(() => {
    const map = new Map<string, PatientCase>();
    catalog?.sections.forEach((s) => s.cases.forEach((c) => map.set(c.id, c)));
    return map;
  }, [catalog]);

  // Real counts (0 when the student has none).
  const total = sessions?.length ?? null;
  const completed = sessions?.filter((s) => s.status === "completed").length ?? null;
  const assessments = sessions?.filter((s) => s.hasAssessment).length ?? null;

  // Sorted most-recent-first by last activity.
  const sorted = useMemo(
    () => (sessions ? [...sessions].sort((a, b) => new Date(b.completedAt ?? b.startedAt).getTime() - new Date(a.completedAt ?? a.startedAt).getTime()) : []),
    [sessions],
  );
  const resumable = sorted.find((s) => s.status === "active" && !s.locked) ?? null;
  const activity = showAll ? sorted : sorted.slice(0, 4);

  function resume(s: SessionSummary) {
    // Reuse the existing interview resume flow (ownership enforced backend-side).
    setStudentName(user?.fullName ?? "");
    setStudentId(user?.studentNumber ?? "");
    setActiveInterview({ caseId: s.caseId, sessionId: s.sessionId, startedAt: Date.now() });
    navigate(`/interview/${s.caseId}`);
  }

  function activityLabel(s: SessionSummary): { text: string; when: string } {
    if (s.status === "active" && !s.locked) return { text: "Interview in progress", when: timeAgo(s.startedAt) };
    if (s.hasAssessment) return { text: "Assessment available", when: timeAgo(s.completedAt ?? s.startedAt) };
    if (s.status === "completed") return { text: "Interview completed", when: timeAgo(s.completedAt ?? s.startedAt) };
    return { text: "Interview", when: timeAgo(s.startedAt) };
  }

  const firstName = (user?.fullName || "").trim().split(/\s+/)[0] || "Student";

  return (
    <div className={styles.wrap}>
      <div className={styles.head}>
        <div>
          <h1 className={styles.welcome}>Welcome back, {firstName}</h1>
          <p className={styles.welcomeSub}>Continue your interview practice and clinical reasoning journey.</p>
        </div>
        <div className={styles.headRight}>
          {isAdmin && (
            <button
              type="button"
              className={styles.adminMgmt}
              onClick={() => navigate("/admin")}
              title="Open Admin Management"
              aria-label="Open Admin Management"
            >
              <IconGear />
              <span className={styles.adminMgmtFull}>Admin Management</span>
              <span className={styles.adminMgmtShort} aria-hidden="true">Admin</span>
            </button>
          )}
          <details className={styles.account}>
            <summary className={styles.accountSummary}>
              <span className={styles.accountName}>
                <strong>{user?.fullName || "Student"}</strong>
                <span>{isAdmin ? "PT Student • Admin" : "PT Student"}</span>
              </span>
              <span className={styles.avatar}>{initials(user?.fullName, user?.email)}</span>
            </summary>
            <div className={styles.accountMenu}>
              <button type="button" onClick={() => setShowAll(true)}>My interviews</button>
              <button type="button" onClick={logout}>Log out</button>
            </div>
          </details>
        </div>
      </div>

      <div className={styles.grid}>
        {/* -------- MAIN -------- */}
        <div>
          <div className={styles.stats}>
            <div className={styles.stat}>
              <span className={styles.statIcon}><IconUsers /></span>
              <div><div className={styles.statNum}>{total ?? "—"}</div><div className={styles.statLbl}>Total Sessions</div><div className={styles.statHint}>All time</div></div>
            </div>
            <div className={styles.stat}>
              <span className={styles.statIcon}><IconCheck /></span>
              <div><div className={styles.statNum}>{completed ?? "—"}</div><div className={styles.statLbl}>Completed Sessions</div><div className={styles.statHint}>All time</div></div>
            </div>
            <div className={styles.stat}>
              <span className={styles.statIcon}><IconClipboard /></span>
              <div><div className={styles.statNum}>{assessments ?? "—"}</div><div className={styles.statLbl}>Assessments</div><div className={styles.statHint}>Ready to review</div></div>
            </div>
            <div className={`${styles.stat} ${styles.resumeStat}`}>
              <span className={styles.statIcon}><IconCheck /></span>
              <div>
                <div className={styles.statNum}>{resumable ? "Resume" : "Ready"}</div>
                <div className={styles.statLbl}>Continue Last Session</div>
                <div className={styles.statHint}>
                  {resumable ? `${caseById.get(resumable.caseId)?.name ?? "Interview"} · ${timeAgo(resumable.startedAt)}` : "No active interview to resume."}
                </div>
              </div>
            </div>
          </div>

          <div className={styles.sectionHead}>
            <div>
              <h2 className={styles.sectionTitle}>Available Patient Cases</h2>
              <p className={styles.sectionSub}>Practice real-world scenarios and build your clinical communication skills.</p>
            </div>
          </div>

          {catalogLoading && <Spinner label="Loading patient cases…" />}
          {catalogError && <ErrorState message={catalogError} onRetry={retry} />}
          {standard && (
            <div className={styles.caseGrid}>
              {standard.cases.map((c) => <CaseCard key={c.id} patientCase={c} />)}
            </div>
          )}

          {referral && referral.cases.length > 0 && (
            <div style={{ marginTop: 28 }}>
              <div className={styles.sectionHead}>
                <div>
                  <h2 className={styles.sectionTitle}>
                    Referral &amp; Interprofessional Cases <span className={styles.advanced}>ADVANCED</span>
                  </h2>
                  <p className={styles.sectionSub}>{referral.description}</p>
                </div>
              </div>
              <div className={styles.referralGrid}>
                {referral.cases.map((c) => (
                  <button key={c.id} type="button" className={styles.referralRow} onClick={() => navigate(`/cases/${c.id}`)}>
                    <AppImage src={c.image} alt={`${c.name} patient portrait`} className={styles.referralThumb} />
                    <span>
                      <span className={styles.referralName}>{c.name}</span>
                      <span className={styles.referralMeta}>{c.setting || "Referral case"}</span>
                    </span>
                    <span className={styles.chevron}>›</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* -------- SIDE -------- */}
        <aside>
          <div className={styles.sideCard}>
            <div className={styles.sideTitle}>Continue Last Session</div>
            {sessionsError ? (
              <div className={styles.emptyBox}>{sessionsError}</div>
            ) : sessions === null ? (
              <Spinner label="Loading…" />
            ) : resumable ? (
              (() => {
                const c = caseById.get(resumable.caseId);
                return (
                  <>
                    <div className={styles.resume}>
                      {c && <AppImage src={c.image} alt={`${c.name} patient portrait`} className={styles.resumeImg} />}
                      <div>
                        <p className={styles.resumeName}>{c?.name ?? resumable.caseId}</p>
                        {c?.shortDescription && <p className={styles.resumeDesc}>{c.shortDescription}</p>}
                        <p className={styles.resumeMeta}>Last active {timeAgo(resumable.startedAt)}</p>
                      </div>
                    </div>
                    <button className="pt-btn pt-btn-block" onClick={() => resume(resumable)}>▶ Resume Interview</button>
                    <button className="pt-btn pt-btn-secondary pt-btn-block" style={{ marginTop: 8 }}
                      onClick={() => navigate(`/student/sessions/${resumable.sessionId}`)}>View Session Details</button>
                  </>
                );
              })()
            ) : (
              <div className={styles.emptyBox}>No active interview to resume.</div>
            )}
          </div>

          <div className={styles.sideCard}>
            <div className={styles.sideTitle}>
              Recent Activity
              {sorted.length > 4 && (
                <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => setShowAll((v) => !v)}>
                  {showAll ? "Show less" : "View all"}
                </button>
              )}
            </div>
            {sessions === null && !sessionsError ? (
              <Spinner label="Loading…" />
            ) : activity.length === 0 ? (
              <div className={styles.emptyBox}>No recent activity yet.</div>
            ) : (
              activity.map((s) => {
                const c = caseById.get(s.caseId);
                const { text, when } = activityLabel(s);
                return (
                  <button key={s.sessionId} type="button" className={styles.activityItem}
                    onClick={() => navigate(`/student/sessions/${s.sessionId}`)}>
                    <span className={styles.activityIcon}><IconClipboard /></span>
                    <span className={styles.activityMain}>
                      <strong>{(c?.name ?? s.caseId)} — {text}</strong>
                      <span>{when}</span>
                    </span>
                    <span className={styles.chevron}>›</span>
                  </button>
                );
              })
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
