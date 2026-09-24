import { Fragment, useEffect, useState, type ComponentType, type ReactNode, type SVGProps } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import {
  fetchStudentDataDetail,
  resetStudentSurvey,
  type StudentDataDetail as Detail,
  type StudentDataSession,
  type StudentSurveyState,
  type SurveyResetScope,
  type SurveyStage,
} from "../../services/authApi";
import { ApiError } from "../../services/api";
import {
  ActiveBadge,
  AssessmentLevelBadge,
  ConfirmModal,
  EmptyState,
  ErrorState,
  LoadingState,
  StatusBadge,
  useToast,
} from "../../portal/ui";
import { caseLabel, fmtDateShort, fmtDateTime, fmtDateTimeShort, fmtDuration } from "../../portal/format";
import {
  IconAlert,
  IconAssessments,
  IconCheckCircle,
  IconClipboard,
  IconClock,
  IconEye,
  IconPlay,
  IconRefresh,
  IconReport,
  IconSessions,
  IconTranscript,
} from "../../components/admin/icons";
import styles from "./AdminStudentData.module.css";

type Icon = ComponentType<SVGProps<SVGSVGElement>>;

/** Admin-facing labels for where an assessment visit was opened from. */
const VISIT_SOURCE_LABELS: Record<string, string> = {
  initial_assessment: "Initial after interview",
  student_dashboard: "Student dashboard",
  legacy: "Historical (before visit tracking)",
  unknown: "Unknown",
};

const AVATAR_COLORS = ["#c8102e", "#2f6fed", "#4caf7d", "#e08a2b", "#7b4fd0", "#2fb8a6", "#d0508a"];

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}

export function StudentAvatar({ name, size = "md" }: { name: string; size?: "sm" | "md" }) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return (
    <span
      className={size === "sm" ? styles.avatarSm : styles.avatar}
      style={{ background: AVATAR_COLORS[h % AVATAR_COLORS.length] }}
      aria-hidden="true"
    >
      {initials(name)}
    </span>
  );
}

const RESET_COPY: Record<SurveyResetScope, { title: string; body: string; label: string }> = {
  pre: {
    title: "Reset this student's Pre Survey?",
    body: "They will be allowed to complete the Pre Survey again.",
    label: "Reset Pre",
  },
  post: {
    title: "Reset this student's Post Survey?",
    body: "They will be allowed to complete the Post Survey again.",
    label: "Reset Post",
  },
  both: {
    title: "Reset both surveys for this student?",
    body: "They will be allowed to complete the Pre and Post Surveys again, in any case.",
    label: "Reset both",
  },
};

export function StudentDataDetail({ studentId }: { studentId: string }) {
  const { token } = useAuth();
  const [data, setData] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    if (!token) return;
    let stale = false;
    setError(null);
    fetchStudentDataDetail(token, studentId)
      .then((d) => !stale && setData(d))
      .catch((e) => !stale && setError(e instanceof ApiError ? e.message : "Could not load this student."));
    return () => {
      stale = true;
    };
  }, [token, studentId, reload]);

  if (error) return <ErrorState message={error} onRetry={() => setReload((n) => n + 1)} />;
  if (!data) return <LoadingState label="Loading student data…" />;

  const { student, summary, survey } = data;
  return (
    <div className={styles.detail}>
      <header className={`${styles.panel} ${styles.header}`}>
        <StudentAvatar name={student.name} />
        <div className={styles.headerText}>
          <div className={styles.headerTitle}>
            <h2 className={styles.studentName}>{student.name}</h2>
            <ActiveBadge active={student.isActive} />
            {!student.hasAccount && <span className="pt-badge pt-badge-gray">No login account</span>}
          </div>
          <div className={styles.headerMeta}>
            <span>ID: {student.studentNumber || "—"}</span>
            <span>{student.email || "—"}</span>
            <span>Last active: {fmtDateTime(student.lastActivityAt)}</span>
          </div>
        </div>
      </header>

      <div className={styles.cards}>
        <SummaryCard icon={IconSessions} tone="red" value={summary.totalSessions} label="Total sessions" />
        <SummaryCard icon={IconCheckCircle} tone="blue" value={summary.completedSessions} label="Completed sessions" />
        <SummaryCard
          icon={IconAlert}
          tone="orange"
          value={summary.incompleteSessions}
          label="Incomplete sessions"
          hint={
            summary.completedWithoutAssessment > 0
              ? `${summary.completedWithoutAssessment} completed without assessment`
              : undefined
          }
        />
        <SummaryCard icon={IconClock} tone="gray" value={fmtMinutes(summary.totalInterviewSeconds)} label="Interview time" />
        <SummaryCard icon={IconReport} tone="purple" value={summary.totalAssessments} label="AI assessments" />
        <SummaryCard icon={IconEye} tone="teal" value={fmtViewTime(summary.totalViewSeconds)} label="Assessment view time" />
        <SummaryCard icon={IconAssessments} tone="blue" value={summary.totalVisits} label="Assessment visits" />
        <SummaryCard
          icon={IconClipboard}
          tone={survey.globalStatus === "completed" ? "green" : "gray"}
          value={SURVEY_GLOBAL_LABELS[survey.globalStatus]}
          label="Survey status"
          small
        />
      </div>

      <SessionHistory sessions={data.sessions} survey={survey} />

      <div className={styles.bottomGrid}>
        <ActivitySummary data={data} />
        <SurveyControls
          studentId={student.id}
          survey={survey}
          onChanged={() => setReload((n) => n + 1)}
        />
        <Timeline events={data.timeline} />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ helpers
const SURVEY_GLOBAL_LABELS: Record<StudentSurveyState["globalStatus"], string> = {
  not_started: "Not started",
  in_progress: "In progress",
  completed: "Completed",
};

function fmtMinutes(seconds: number): string {
  if (!seconds) return "0 min";
  return seconds < 60 ? `${seconds}s` : `${Math.round(seconds / 60)} min`;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleTimeString(undefined, { timeStyle: "short" });
}

function fmtViewTime(seconds: number | null | undefined): string {
  return seconds == null ? "—" : fmtDuration(seconds);
}

function SummaryCard({
  icon: Icon,
  tone,
  value,
  label,
  hint,
  small = false,
}: {
  icon: Icon;
  tone: "red" | "blue" | "green" | "orange" | "purple" | "gray" | "teal";
  value: ReactNode;
  label: string;
  hint?: string;
  small?: boolean;
}) {
  return (
    <div className={styles.card}>
      <span className={`${styles.cardIcon} ${styles[`tone_${tone}`]}`}>
        <Icon width={18} height={18} />
      </span>
      <div className={styles.cardBody}>
        <div className={small ? styles.cardValueSmall : styles.cardValue}>{value}</div>
        <div className={styles.cardLabel}>{label}</div>
        {hint && <div className={styles.cardHint}>{hint}</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ session table
function sessionSurveyBadge(status: string, ownerCaseName: string | null) {
  switch (status) {
    case "completed":
      return <span className="pt-badge pt-badge-green">Completed</span>;
    case "pre_completed":
      return <span className="pt-badge pt-badge-amber">Pre only</span>;
    case "other_case":
      return (
        <span
          className="pt-badge pt-badge-gray"
          title={`The student's one survey was taken in ${ownerCaseName ?? "another case"}`}
        >
          Other case
        </span>
      );
    default:
      return <span className="pt-badge pt-badge-gray">Not completed</span>;
  }
}

function SessionHistory({ sessions, survey }: { sessions: StudentDataSession[]; survey: StudentSurveyState }) {
  const navigate = useNavigate();
  const [openId, setOpenId] = useState<string | null>(null);

  return (
    <section className={styles.panel} aria-labelledby="sd-history">
      <div className={styles.panelHead}>
        <h3 id="sd-history" className={styles.panelTitle}>Session &amp; Activity History</h3>
      </div>
      {sessions.length === 0 ? (
        <EmptyState title="No interview sessions yet" hint="Sessions appear here once the student starts a case." />
      ) : (
        <div className={styles.tableScroll}>
          <table className={`pt-table ${styles.historyTable}`}>
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">Case</th>
                <th scope="col">Duration</th>
                <th scope="col">Questions</th>
                <th scope="col">Interview</th>
                <th scope="col">AI assessment</th>
                <th scope="col">View time</th>
                <th scope="col">Visits</th>
                <th scope="col">Survey</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => {
                const open = openId === s.sessionId;
                const hasVisits = s.visits.length > 0;
                return (
                  <Fragment key={s.sessionId}>
                    <tr className={open ? styles.expandedRow : undefined}>
                      <td className={styles.nowrap}>
                        {fmtDateShort(s.startedAt)}
                        <div className={styles.subTime}>{fmtTime(s.startedAt)}</div>
                      </td>
                      <td>{caseLabel(s.caseId)}</td>
                      <td className={styles.nowrap}>{fmtDuration(s.durationSeconds)}</td>
                      <td>{s.studentQuestionCount}</td>
                      <td>
                        {s.status === "active" ? (
                          <span className="pt-badge pt-badge-amber">incomplete</span>
                        ) : (
                          <StatusBadge status={s.status} />
                        )}
                      </td>
                      <td>
                        {s.hasAssessment ? (
                          s.overallLevel ? (
                            <AssessmentLevelBadge level={s.overallLevel} />
                          ) : (
                            <span className="pt-badge pt-badge-gray">{(s.assessmentStatus ?? "pending").toLowerCase()}</span>
                          )
                        ) : (
                          <span className="pt-muted">—</span>
                        )}
                      </td>
                      <td className={styles.nowrap}>
                        {hasVisits ? (
                          <button
                            type="button"
                            className={styles.linkButton}
                            onClick={() => setOpenId(open ? null : s.sessionId)}
                            aria-expanded={open}
                            aria-controls={`visits-${s.sessionId}`}
                          >
                            {fmtViewTime(s.activeViewingSeconds)}
                          </button>
                        ) : (
                          <span className="pt-muted">{s.hasAssessment ? "Never viewed" : "—"}</span>
                        )}
                      </td>
                      <td>
                        {hasVisits ? (
                          <button
                            type="button"
                            className={styles.linkButton}
                            onClick={() => setOpenId(open ? null : s.sessionId)}
                            aria-expanded={open}
                            aria-controls={`visits-${s.sessionId}`}
                            aria-label={`${s.viewCount ?? s.visits.length} visits — ${open ? "hide" : "show"} visit history`}
                          >
                            {s.viewCount ?? s.visits.length} {open ? "▴" : "▾"}
                          </button>
                        ) : (
                          <span className="pt-muted">—</span>
                        )}
                      </td>
                      <td>{sessionSurveyBadge(s.surveyStatus, survey.ownerCaseName)}</td>
                      <td>
                        <div className="pt-actions-cell">
                          <button
                            className="pt-icon-btn"
                            title="View transcript"
                            aria-label={`View transcript for ${caseLabel(s.caseId)} session`}
                            onClick={() => navigate(`/admin/sessions/${s.sessionId}?tab=transcript`)}
                          >
                            <IconTranscript width={16} height={16} />
                          </button>
                          <button
                            className="pt-icon-btn"
                            title={s.hasAssessment ? "View assessment" : "No assessment yet"}
                            aria-label={`View assessment for ${caseLabel(s.caseId)} session`}
                            disabled={!s.hasAssessment}
                            onClick={() => navigate(`/admin/sessions/${s.sessionId}?tab=assessment`)}
                          >
                            <IconAssessments width={16} height={16} />
                          </button>
                          <button
                            className="pt-icon-btn"
                            title="View session detail"
                            aria-label={`View session detail for ${caseLabel(s.caseId)} session`}
                            onClick={() => navigate(`/admin/sessions/${s.sessionId}`)}
                          >
                            <IconSessions width={16} height={16} />
                          </button>
                        </div>
                      </td>
                    </tr>
                    {open && (
                      <tr className={styles.visitRow}>
                        <td colSpan={10} id={`visits-${s.sessionId}`}>
                          <VisitHistory session={s} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function VisitHistory({ session }: { session: StudentDataSession }) {
  return (
    <div className={styles.visitPanel}>
      <div className={styles.visitHead}>
        <strong>Assessment visit history — {caseLabel(session.caseId)}</strong>
        <span className="pt-muted">
          {fmtViewTime(session.activeViewingSeconds)} active across {session.viewCount ?? session.visits.length} visit
          {(session.viewCount ?? session.visits.length) === 1 ? "" : "s"} · First viewed{" "}
          {fmtDateTimeShort(session.firstViewedAt)} · Last viewed {fmtDateTimeShort(session.lastViewedAt)}
        </span>
      </div>
      <table className={styles.visitTable}>
        <thead>
          <tr>
            <th scope="col">Visit</th>
            <th scope="col">Source</th>
            <th scope="col">Started</th>
            <th scope="col">Active time</th>
          </tr>
        </thead>
        <tbody>
          {session.visits.map((v) => (
            <tr key={v.visitNumber}>
              <td>Visit {v.visitNumber}</td>
              <td>
                {VISIT_SOURCE_LABELS[v.source] ?? VISIT_SOURCE_LABELS.unknown}
                {v.assessmentDeleted && <span className="pt-muted"> · assessment since deleted</span>}
              </td>
              <td>{fmtDateTimeShort(v.startedAt)}</td>
              <td>{fmtDuration(v.activeSeconds)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {session.visits.some((v) => v.source === "legacy") && (
        <p className={styles.note}>
          Historical time recorded before per-visit tracking is shown as one combined visit.
        </p>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ activity summary
function ActivitySummary({ data }: { data: Detail }) {
  const { summary, survey, student } = data;
  const rows: [string, ReactNode][] = [
    ["Last activity", fmtDateTime(student.lastActivityAt)],
    ["Last login", fmtDateTime(student.lastLoginAt)],
    ["Total interview time", fmtDuration(summary.totalInterviewSeconds)],
    ["Average interview duration", fmtDuration(summary.averageInterviewSeconds)],
    ["Questions asked", summary.totalStudentQuestions],
    ["Assessment visits", summary.totalVisits],
    ["Assessment view time", fmtDuration(summary.totalViewSeconds)],
    ["Survey status", SURVEY_GLOBAL_LABELS[survey.globalStatus]],
    ["Last survey response", fmtDateTime(survey.lastResponseAt)],
  ];
  return (
    <section className={styles.panel} aria-labelledby="sd-summary">
      <div className={styles.panelHead}>
        <h3 id="sd-summary" className={styles.panelTitle}>Student Activity Summary</h3>
      </div>
      <dl className={styles.kv}>
        {rows.map(([k, v]) => (
          <div key={k} className={styles.kvRow}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

// ------------------------------------------------------------------ survey controls
function StageCard({
  label,
  stage,
  canReset,
  onReset,
}: {
  label: string;
  stage: SurveyStage;
  canReset: boolean;
  onReset: () => void;
}) {
  return (
    <div className={styles.stage}>
      <div className={styles.stageTitle}>{label}</div>
      {stage.completed ? (
        <span className="pt-badge pt-badge-green">Completed</span>
      ) : stage.syncStatus === "failed" ? (
        <span className="pt-badge pt-badge-red">Not completed (sync failed)</span>
      ) : (
        <span className="pt-badge pt-badge-gray">Not completed</span>
      )}
      <div className={styles.stageDate}>
        {stage.completed ? fmtDateTime(stage.completedAt) : "—"}
        {stage.syncStatus === "skipped" && (
          <div className="pt-muted">Recorded locally (REDCap not configured)</div>
        )}
      </div>
      <button
        type="button"
        className="pt-btn pt-btn-secondary pt-btn-sm pt-btn-icon"
        disabled={!canReset}
        onClick={onReset}
      >
        <IconRefresh width={14} height={14} /> Reset {label.split(" ")[0]}
      </button>
    </div>
  );
}

function SurveyControls({
  studentId,
  survey,
  onChanged,
}: {
  studentId: string;
  survey: StudentSurveyState;
  onChanged: () => void;
}) {
  const { token } = useAuth();
  const toast = useToast();
  const [confirm, setConfirm] = useState<SurveyResetScope | null>(null);
  const [busy, setBusy] = useState(false);

  async function doReset(scope: SurveyResetScope) {
    if (!token) return;
    setBusy(true);
    try {
      const res = await resetStudentSurvey(token, studentId, scope);
      toast.success(res.message || "Survey reset.");
      setConfirm(null);
      onChanged();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Survey reset failed.");
    } finally {
      setBusy(false);
    }
  }

  const caseName = survey.ownerCaseName ?? survey.caseName;
  return (
    <section className={styles.panel} aria-labelledby="sd-survey">
      <div className={styles.panelHead}>
        <h3 id="sd-survey" className={styles.panelTitle}>Survey Controls</h3>
      </div>
      <p className={styles.surveyScope}>
        {survey.ownerCaseId
          ? `One survey per student — taken in ${caseName}.`
          : "One survey per student — not started yet."}
      </p>
      <div className={styles.stages}>
        <StageCard label="Pre Survey" stage={survey.pre} canReset={survey.canResetPre} onReset={() => setConfirm("pre")} />
        <StageCard label="Post Survey" stage={survey.post} canReset={survey.canResetPost} onReset={() => setConfirm("post")} />
      </div>
      <button
        type="button"
        className={`pt-btn pt-btn-danger pt-btn-sm pt-btn-icon ${styles.resetBoth}`}
        disabled={!survey.canResetBoth}
        onClick={() => setConfirm("both")}
      >
        <IconRefresh width={14} height={14} /> Reset both surveys
      </button>
      <p className={styles.note}>
        This allows the student to complete the survey again. Earlier answers stay in REDCap under
        their original record; the retake is saved as a new record. Sessions, transcripts and
        assessments are not changed.
        {survey.ownerCaseId && ` Pre/Post resets are retaken in ${caseName}; "Reset both" lets the student take the survey in any case.`}
      </p>
      {survey.resetCount > 0 && (
        <p className={styles.note}>
          Reset {survey.resetCount} time{survey.resetCount === 1 ? "" : "s"} · last {fmtDateTime(survey.lastResetAt)}
        </p>
      )}

      {confirm && (
        <ConfirmModal
          title={RESET_COPY[confirm].title}
          body={RESET_COPY[confirm].body}
          confirmLabel={RESET_COPY[confirm].label}
          danger
          busy={busy}
          onConfirm={() => doReset(confirm)}
          onCancel={() => setConfirm(null)}
        />
      )}
    </section>
  );
}

// ------------------------------------------------------------------ timeline
const TIMELINE_ICONS: Record<string, { icon: Icon; tone: string }> = {
  interview_started: { icon: IconPlay, tone: "green" },
  interview_completed: { icon: IconCheckCircle, tone: "green" },
  assessment_generated: { icon: IconReport, tone: "purple" },
  assessment_viewed: { icon: IconEye, tone: "blue" },
  pre_survey_completed: { icon: IconClipboard, tone: "teal" },
  post_survey_completed: { icon: IconClipboard, tone: "teal" },
  survey_reset: { icon: IconRefresh, tone: "orange" },
};
const TIMELINE_PREVIEW = 8;

function Timeline({ events }: { events: Detail["timeline"] }) {
  const [all, setAll] = useState(false);
  const shown = all ? events : events.slice(0, TIMELINE_PREVIEW);
  return (
    <section className={styles.panel} aria-labelledby="sd-timeline">
      <div className={styles.panelHead}>
        <h3 id="sd-timeline" className={styles.panelTitle}>Recent Activity Timeline</h3>
      </div>
      {events.length === 0 ? (
        <p className="pt-muted">No activity recorded yet.</p>
      ) : (
        <ol className={styles.timeline}>
          {shown.map((e, i) => {
            const meta = TIMELINE_ICONS[e.kind] ?? { icon: IconClock, tone: "gray" };
            const Ico = meta.icon;
            return (
              <li key={`${e.kind}-${e.at}-${i}`} className={styles.timelineItem}>
                <span className={`${styles.timelineIcon} ${styles[`tone_${meta.tone}`]}`}>
                  <Ico width={14} height={14} />
                </span>
                <div className={styles.timelineText}>
                  <strong>{e.title}</strong>
                  {e.detail && <span className="pt-muted"> — {e.detail}</span>}
                </div>
                <time className={styles.timelineAt} dateTime={e.at}>
                  {fmtDateTimeShort(e.at)}
                </time>
              </li>
            );
          })}
        </ol>
      )}
      {events.length > TIMELINE_PREVIEW && (
        <button type="button" className={styles.linkButton} onClick={() => setAll((v) => !v)}>
          {all ? "Show less" : `Show all (${events.length})`}
        </button>
      )}
    </section>
  );
}
