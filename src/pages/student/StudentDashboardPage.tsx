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
  <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="9" cy="8" r="3.2" /><path d="M3 20c0-3 2.7-5 6-5s6 2 6 5" /><path d="M16 4.5a3 3 0 0 1 0 6M18 20c0-2.4-1-4-3-4.6" /></svg>
);
const IconCheck = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M8.5 12.5l2.5 2.5 4.5-5" /></svg>
);
const IconClipboard = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="6" y="4" width="12" height="17" rx="2" /><path d="M9 4V3h6v1M9 10h6M9 14h4" /></svg>
);
const IconResume = () => (
  <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v4l3-3" /><path d="M20 12a8 8 0 1 1-2.34-5.66L20 9" /></svg>
);
const IconGear = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" /></svg>
);
const IconReferral = () => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" /><path d="M14 3v5h5" /><path d="M9 13h6" /><path d="M9 17h6" /><path d="M9 9h2" /></svg>
);
const IconChevron = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
);

type ActivityKind = "active" | "assessment" | "completed" | "default";

function activityMeta(session: SessionSummary): { text: string; when: string; kind: ActivityKind } {
  if (session.status === "active" && !session.locked) {
    return { text: "Interview in progress", when: timeAgo(session.startedAt), kind: "active" };
  }
  if (session.hasAssessment) {
    return { text: "Assessment available", when: timeAgo(session.completedAt ?? session.startedAt), kind: "assessment" };
  }
  if (session.status === "completed") {
    return { text: "Interview completed", when: timeAgo(session.completedAt ?? session.startedAt), kind: "completed" };
  }
  return { text: "Interview activity", when: timeAgo(session.startedAt), kind: "default" };
}

function displayRole(isAdmin: boolean, userName: string | undefined): string {
  if (isAdmin) return "Administrator";
  const firstName = (userName || "").trim().split(/\s+/)[0];
  return firstName || "Student";
}

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
    catalog?.sections.forEach((section) => section.cases.forEach((item) => map.set(item.id, item)));
    return map;
  }, [catalog]);

  const total = sessions?.length ?? null;
  const completed = sessions?.filter((s) => s.status === "completed").length ?? null;
  const assessments = sessions?.filter((s) => s.hasAssessment).length ?? null;

  const sorted = useMemo(
    () => (sessions
      ? [...sessions].sort(
          (a, b) =>
            new Date(b.completedAt ?? b.startedAt).getTime() -
            new Date(a.completedAt ?? a.startedAt).getTime(),
        )
      : []),
    [sessions],
  );

  const resumable = sorted.find((s) => s.status === "active" && !s.locked) ?? null;
  const activity = showAll ? sorted : sorted.slice(0, 4);
  const welcomeName = displayRole(isAdmin, user?.fullName);

  function resume(session: SessionSummary) {
    setStudentName(user?.fullName ?? "");
    setStudentId(user?.studentNumber ?? "");
    setActiveInterview({ caseId: session.caseId, sessionId: session.sessionId, startedAt: Date.now() });
    navigate(`/interview/${session.caseId}`);
  }

  const statItems = [
    {
      key: "total",
      tone: styles.statToneRed,
      icon: <IconUsers />,
      value: total ?? "—",
      title: "Total Sessions",
      hint: "All time",
    },
    {
      key: "completed",
      tone: styles.statToneGreen,
      icon: <IconCheck />,
      value: completed ?? "—",
      title: "Completed Sessions",
      hint: "All time",
    },
    {
      key: "assessments",
      tone: styles.statToneAmber,
      icon: <IconClipboard />,
      value: assessments ?? "—",
      title: "Assessments",
      hint: "Ready to review",
    },
    {
      key: "resume",
      tone: styles.statTonePurple,
      icon: <IconResume />,
      value: resumable ? "Resume" : "No Active Session",
      title: "Continue Last Session",
      hint: resumable ? `${caseById.get(resumable.caseId)?.name ?? "Interview"} • ${timeAgo(resumable.startedAt)}` : "No active session",
    },
  ];

  return (
    <div className={styles.page}>
      <div className={styles.wrap}>
        <div className={styles.head}>
          <div className={styles.headText}>
            <h1 className={styles.welcome}>Welcome back, {welcomeName} <span aria-hidden="true">👋</span></h1>
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
          <div className={styles.mainColumn}>
            <div className={styles.stats}>
              {statItems.map((item) => (
                <div key={item.key} className={`${styles.stat} ${item.tone}`}>
                  <span className={styles.statIcon}>{item.icon}</span>
                  <div className={styles.statCopy}>
                    <div className={styles.statValue}>{item.value}</div>
                    <div className={styles.statLabel}>{item.title}</div>
                    <div className={styles.statHint}>{item.hint}</div>
                  </div>
                </div>
              ))}
            </div>

            <section className={styles.panel}>
              <div className={styles.sectionHead}>
                <div>
                  <h2 className={styles.sectionTitle}>Patient Cases</h2>
                  <p className={styles.sectionSub}>Choose a case to start an interview and build your clinical communication skills.</p>
                </div>
              </div>

              {catalogLoading && <Spinner label="Loading patient cases…" />}
              {catalogError && <ErrorState message={catalogError} onRetry={retry} />}
              {standard && (
                <div className={styles.caseGrid}>
                  {standard.cases.map((item) => <CaseCard key={item.id} patientCase={item} />)}
                </div>
              )}
            </section>

            {referral && referral.cases.length > 0 && (
              <section className={styles.panel}>
                <div className={styles.sectionHead}>
                  <div>
                    <h2 className={styles.sectionTitle}>
                      Referral &amp; Interprofessional Cases
                      <span className={styles.advanced}>ADVANCED</span>
                    </h2>
                    <p className={styles.sectionSub}>{referral.description}</p>
                  </div>
                </div>
                <div className={styles.referralGrid}>
                  {referral.cases.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      className={styles.referralRow}
                      onClick={() => navigate(`/cases/${item.id}`)}
                    >
                      <span className={styles.referralIcon}>
                        <IconReferral />
                      </span>
                      <span className={styles.referralCopy}>
                        <span className={styles.referralName}>{item.name}</span>
                        <span className={styles.referralMeta}>{item.setting || "Referral case"}</span>
                      </span>
                      <span className={styles.referralChevron}>
                        <IconChevron />
                      </span>
                    </button>
                  ))}
                </div>
              </section>
            )}
          </div>

          <aside className={styles.sideColumn}>
            <section className={`${styles.sideCard} ${styles.resumeCard}`}>
              <div className={styles.sideTitle}>Continue Last Session</div>
              {sessionsError ? (
                <div className={styles.emptyBox}>{sessionsError}</div>
              ) : sessions === null ? (
                <Spinner label="Loading…" />
              ) : resumable ? (
                (() => {
                  const currentCase = caseById.get(resumable.caseId);
                  return (
                    <div className={styles.resumeShell}>
                      <div className={styles.resumeInner}>
                        <div className={styles.resumeHeader}>
                          {currentCase && (
                            <AppImage
                              src={currentCase.image}
                              alt={`${currentCase.name} patient portrait`}
                              className={styles.resumeImg}
                            />
                          )}
                          <div className={styles.resumeCopy}>
                            <div className={styles.resumeTitleRow}>
                              <p className={styles.resumeName}>{currentCase?.name ?? resumable.caseId}</p>
                              <span className={styles.statusBadge}>In Progress</span>
                            </div>
                            <p className={styles.resumeCategory}>{currentCase?.setting || resumable.caseCategory}</p>
                            {currentCase?.shortDescription && <p className={styles.resumeDesc}>{currentCase.shortDescription}</p>}
                          </div>
                        </div>

                        <div className={styles.resumeFacts}>
                          <div>
                            <span className={styles.factLabel}>Status</span>
                            <strong className={styles.factValue}>Active interview</strong>
                          </div>
                          <div>
                            <span className={styles.factLabel}>Turns recorded</span>
                            <strong className={styles.factValue}>{resumable.turnCount}</strong>
                          </div>
                          <div>
                            <span className={styles.factLabel}>Last active</span>
                            <strong className={styles.factValue}>{timeAgo(resumable.startedAt)}</strong>
                          </div>
                        </div>

                        <button className={styles.primaryAction} onClick={() => resume(resumable)}>
                          <span>Resume Interview</span>
                          <span className={styles.buttonArrow} aria-hidden="true">›</span>
                        </button>
                        <button
                          className={styles.secondaryAction}
                          onClick={() => navigate(`/student/sessions/${resumable.sessionId}`)}
                        >
                          View Session Details
                        </button>
                      </div>
                    </div>
                  );
                })()
              ) : (
                <div className={styles.emptyBox}>No active session.</div>
              )}
            </section>

            <section className={styles.sideCard}>
              <div className={styles.sideTitle}>
                <span>Recent Activity</span>
                {sorted.length > 4 && (
                  <button className={styles.inlineToggle} onClick={() => setShowAll((value) => !value)}>
                    {showAll ? "Show less" : "View all"}
                  </button>
                )}
              </div>
              {sessions === null && !sessionsError ? (
                <Spinner label="Loading…" />
              ) : activity.length === 0 ? (
                <div className={styles.emptyBox}>No recent activity yet.</div>
              ) : (
                <div className={styles.activityList}>
                  {activity.map((session) => {
                    const patientCase = caseById.get(session.caseId);
                    const meta = activityMeta(session);
                    return (
                      <button
                        key={session.sessionId}
                        type="button"
                        className={styles.activityItem}
                        onClick={() => navigate(`/student/sessions/${session.sessionId}`)}
                      >
                        <span className={`${styles.activityIcon} ${styles[`activity${meta.kind[0].toUpperCase()}${meta.kind.slice(1)}`]}`}>
                          {meta.kind === "active" ? <IconResume /> : meta.kind === "completed" ? <IconCheck /> : <IconClipboard />}
                        </span>
                        <span className={styles.activityMain}>
                          <strong>{patientCase?.name ?? session.caseId} — {meta.text}</strong>
                          <span>{meta.when}</span>
                        </span>
                        <span className={styles.activityChevron}>
                          <IconChevron />
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </section>
          </aside>
        </div>
      </div>
    </div>
  );
}
