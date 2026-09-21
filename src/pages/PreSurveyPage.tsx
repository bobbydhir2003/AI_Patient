import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ProgressSteps } from "../components/layout/ProgressSteps";
import { AppImage } from "../components/common/AppImage";
import { SurveyLikert } from "../components/survey/SurveyLikert";
import { PRE_LIKERT } from "../services/surveyQuestions";
import { getSurveyStatus, submitPreSurvey } from "../services/surveysApi";
import {
  globalPreGate,
  resolveInterviewDestination,
  destinationToPath,
} from "../services/surveyFlow";
import { ApiError, createSession, fetchSession } from "../services/api";
import { joinQueue } from "../services/queueApi";
import { usePatientCase } from "../services/cases";
import { useAppContext } from "../state/AppContext";
import { useAuth } from "../state/AuthContext";
import { caseHubPath } from "../services/authRouting";
import styles from "./SurveyPage.module.css";

const PROGRESS_STEPS = ["Case Introduction", "Pre Survey", "Interview", "Post Survey", "Assessment Results"];

/** What the Pre-Survey screen renders once status is known. */
type GateMode = "collect" | "continue" | "skip";

export function PreSurveyPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const { user, token } = useAuth();
  const { patientCase } = usePatientCase(caseId);
  const { studentName, studentId, activeInterview, setActiveInterview } = useAppContext();
  const studentHome = caseHubPath(user?.role);

  // RESUME mode: the persisted active session is bound to THIS case with the
  // resume flag (Dashboard "Continue Last Session"). In resume mode we NEVER
  // create a new session and NEVER re-enter the admission queue - we validate
  // and continue the EXISTING session. Survives refresh because the flag is
  // persisted in AppContext/localStorage.
  const resumeMode =
    !!activeInterview && activeInterview.resume === true && activeInterview.caseId === caseId;

  const [answers, setAnswers] = useState<Record<string, number>>({});
  // Read-only identity (NUID + case) resolved server-side from the session for
  // display/confirmation only. Never editable and never sent back on submit.
  const [identity, setIdentity] = useState<{
    nuid: string;
    caseNumber: number | null;
    caseName: string;
  } | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [checkingStatus, setCheckingStatus] = useState(true);
  const [nuidMissing, setNuidMissing] = useState(false);
  // How the screen renders once the GLOBAL survey status is known:
  //   collect  → show Pre questions (this case owns/eligible, Pre not yet done)
  //   continue → owning case, Pre already submitted → continue (no re-ask)
  //   skip     → a DIFFERENT case owns the one global package → skip & continue
  const [gateMode, setGateMode] = useState<GateMode>("collect");
  // Owner-aware context for the skip/continue messaging (owner display name +
  // whether the global package is IN_PROGRESS vs COMPLETED).
  const [ownerName, setOwnerName] = useState<string | null>(null);
  const [globalStatus, setGlobalStatus] = useState<string>("not_started");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const initRef = useRef(false);

  // Advance from the pre-survey into the interview.
  //   NEW flow    → capacity/queue check (joinQueue), then interview or queue.
  //   RESUME flow → NO joinQueue and NO new session: re-validate the existing
  //                 session and route by its CURRENT backend state (a session
  //                 that went stale/locked/completed is sent to Post/Assessment
  //                 instead of blindly entering the interview).
  async function proceedToInterview(id: string) {
    if (!sessionId) return;
    if (resumeMode) {
      try {
        const s = await fetchSession(sessionId);
        const dest = resolveInterviewDestination(
          s.caseId === id ? { status: s.status, locked: s.locked, hasAssessment: false } : null,
        );
        navigate(destinationToPath(dest, { caseId: id, sessionId }), { replace: true });
      } catch {
        // Validation unavailable: fall back to the interview page, which has its
        // own resume/guard logic and never fabricates a session.
        navigate(`/interview/${id}`, { replace: true });
      }
      return;
    }
    try {
      const r = await joinQueue(token, id);
      if (r.admitted || r.state === "admitted") {
        navigate(`/interview/${id}`, { replace: true });
      } else {
        navigate(`/queue/${id}`, { replace: true, state: { entryId: r.entry_id } });
      }
    } catch {
      // Queue check unavailable — don't block; the interview page has its own
      // capacity handling (matches prior Case Introduction behavior).
      navigate(`/interview/${id}`, { replace: true });
    }
  }

  // Establish (or, in resume mode, VALIDATE) the backend session, then read the
  // GLOBAL survey status to decide the gate. The interview page's own resume
  // logic reuses this same session (no duplicate).
  useEffect(() => {
    if (!caseId) return;
    let cancelled = false;

    async function loadGate(sid: string) {
      setSessionId(sid);
      try {
        const status = await getSurveyStatus(sid);
        if (cancelled) return;
        setIdentity({ nuid: status.nuid, caseNumber: status.caseNumber, caseName: status.caseName });
        if (!status.nuidOnFile) setNuidMissing(true);
        setOwnerName(status.surveyOwnerCaseName);
        setGlobalStatus(status.globalSurveyStatus);
        setGateMode(
          globalPreGate(status.globalSurveyStatus, status.isSurveyOwnerCase, status.preSubmitted),
        );
      } catch {
        // Status is best-effort; submission is still guarded server-side. Default
        // to collecting so the student is never wrongly blocked.
        if (!cancelled) setGateMode("collect");
      } finally {
        if (!cancelled) setCheckingStatus(false);
      }
    }

    async function init(id: string) {
      // Yield one tick so React StrictMode's throwaway first mount is cancelled
      // before it can POST a duplicate session.
      await new Promise((resolve) => window.setTimeout(resolve, 0));
      if (cancelled || initRef.current) return;
      initRef.current = true;
      try {
        if (resumeMode && activeInterview) {
          // RESUME: reuse the existing session. Never create one. Validate it is
          // still resumable; if it went stale, route by current backend state.
          const sid = activeInterview.sessionId;
          try {
            const s = await fetchSession(sid);
            if (cancelled) return;
            if (s.caseId !== id || s.locked || s.status !== "active") {
              const dest = resolveInterviewDestination(
                s.caseId === id
                  ? { status: s.status, locked: s.locked, hasAssessment: false }
                  : null,
              );
              navigate(destinationToPath(dest, { caseId: id, sessionId: sid }), { replace: true });
              return;
            }
          } catch {
            /* validation unavailable: still show the Pre step against the stored
               session; proceedToInterview has its own fallback. */
          }
          if (cancelled) return;
          await loadGate(sid);
          return;
        }

        // NEW flow: reuse a matching active session or create a fresh one.
        let sid: string;
        if (activeInterview && activeInterview.caseId === id) {
          sid = activeInterview.sessionId;
        } else {
          const session = await createSession(
            studentName.trim() || "Student",
            studentId.trim(),
            id,
          );
          if (cancelled) return;
          setActiveInterview({
            caseId: id,
            sessionId: session.sessionId,
            startedAt: Date.now(),
            resume: false,
          });
          sid = session.sessionId;
        }
        if (cancelled) return;
        await loadGate(sid);
      } catch {
        if (!cancelled) {
          setCheckingStatus(false);
          setError("We couldn't start your session. Please go back to the case and try again.");
        }
      }
    }

    void init(caseId);
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId]);

  async function handleSubmit() {
    if (gateMode !== "collect" || submitting || nuidMissing || !sessionId || !caseId) return;
    const unanswered = PRE_LIKERT.some((q) => !answers[q.name]);
    if (unanswered) {
      setError("Please answer all questions before continuing.");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await submitPreSurvey(sessionId, answers);
      await proceedToInterview(caseId);
    } catch (err) {
      if (err instanceof ApiError && err.code === "nuid_missing") {
        setNuidMissing(true);
        setError(err.message);
      } else if (
        err instanceof ApiError &&
        (err.code === "survey_already_completed" || err.code === "survey_owned_by_other_case")
      ) {
        // Race: the one global package was established/completed (by this or
        // another case) between load and submit. Flip to the skip state and
        // never retry the survey submission.
        setAnswers({});
        setGateMode("skip");
        setSubmitting(false);
      } else {
        setError(
          err instanceof ApiError ? err.message : "Could not submit the survey. Please try again.",
        );
        setSubmitting(false);
      }
    }
  }

  if (checkingStatus) {
    return (
      <div className="page">
        <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={1} />
        <div className={`card ${styles.card}`} role="status">
          <p>Checking survey status…</p>
        </div>
      </div>
    );
  }

  const isSkip = gateMode === "skip";
  const isContinue = gateMode === "continue";
  const readOnly = isSkip || isContinue;

  // Owner-aware copy for the skip/continue banner. Owner name falls back to a
  // neutral phrase if the backend unexpectedly omitted it; current case name
  // falls back to "this case". Never crashes.
  const ownerLabel = ownerName || "your original survey case";
  const currentCaseName = identity?.caseName || patientCase?.name || "this case";
  const isCompleted = globalStatus === "completed";

  let banner: { title: string; message: string; button: string } | null = null;
  if (isContinue) {
    // Owner case, Pre already submitted.
    banner = isCompleted
      ? {
          // Case D — owner case fully completed.
          title: "Survey already completed",
          message: `You already completed your survey with ${ownerLabel}. No additional survey is required.`,
          button: "Continue to Interview",
        }
      : {
          // Case A — owner case, Pre done, Post still pending.
          title: "Pre-Survey already submitted",
          message: `You already submitted the Pre-Survey for ${ownerLabel}. Complete this interview to finish the Post-Survey afterward.`,
          button: "Continue to Interview",
        };
  } else if (isSkip) {
    // Non-owner case: the one survey belongs to another case.
    banner = isCompleted
      ? {
          // Case C — non-owner, owner survey COMPLETED.
          title: "Survey already completed",
          message: `You already completed your survey with ${ownerLabel}. No additional survey is required for ${currentCaseName}.`,
          button: "Skip Survey & Continue to Interview",
        }
      : {
          // Case B — non-owner, owner survey IN_PROGRESS.
          title: `Survey already assigned to ${ownerLabel}`,
          message: `You started your survey with ${ownerLabel}. Only the ${ownerLabel} case will collect your survey responses. You can continue ${currentCaseName} without completing another survey.`,
          button: "Skip Survey & Continue to Interview",
        };
  }

  return (
    <div className="page">
      <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={1} />

      <div className={styles.headerBlock}>
        <h1 className={styles.title}>Pre-Interview Survey</h1>
        <p className={styles.subtitle}>
          Before you begin, tell us how confident you feel about the upcoming patient interview.
          Your answers are recorded for research and do not affect your assessment.
        </p>
      </div>

      {banner && (
        <div className={styles.completedBanner} role="alert">
          <h2 className={styles.completedTitle}>{banner.title}</h2>
          <p className={styles.completedMessage}>{banner.message}</p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => caseId && void proceedToInterview(caseId)}
            disabled={!sessionId}
          >
            {banner.button}
          </button>
        </div>
      )}

      <div className={styles.layout}>
        <div className={styles.main}>
          {identity && (
            <div className={styles.identityCard} aria-label="Your survey identity">
              <div className={styles.identityItem}>
                <span className={styles.identityLabel}>Student NUID</span>
                <span className={styles.identityValue}>{identity.nuid || "—"}</span>
              </div>
              <div className={styles.identityItem}>
                <span className={styles.identityLabel}>Patient Case</span>
                <span className={styles.identityValue}>
                  {identity.caseNumber != null ? `${identity.caseNumber} - ` : ""}
                  {identity.caseName}
                </span>
              </div>
            </div>
          )}

          {!readOnly && nuidMissing && (
            <div className={styles.banner} role="alert">
              Your student number (NUID) is missing from your profile. Add it to your profile before
              submitting the survey. You can still start the interview from the case page.
            </div>
          )}

          {/* In skip/continue (readOnly) state the survey is already handled
              globally, so the questions are HIDDEN entirely (not rendered
              read-only) — the identity block, banners and navigation stay. */}
          {!readOnly && (
            <div className={`card ${styles.card}`}>
              <p className={styles.scaleLegend}>
                Rate each statement from 1 (Strongly disagree) to 5 (Strongly agree).
              </p>
              <div className={styles.questionList}>
                {PRE_LIKERT.map((q, i) => (
                  <SurveyLikert
                    key={q.name}
                    name={q.name}
                    index={i + 1}
                    prompt={q.prompt}
                    value={answers[q.name]}
                    onChange={(value) => setAnswers((a) => ({ ...a, [q.name]: value }))}
                  />
                ))}
              </div>
            </div>
          )}

          {error && (
            <div className={styles.errorText} role="alert">
              {error}
            </div>
          )}

          <div className={styles.actions}>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => navigate(`/cases/${caseId}`)}
            >
              Back to Case
            </button>
            {!readOnly && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void handleSubmit()}
                disabled={submitting || nuidMissing || !sessionId}
              >
                {submitting ? "Submitting…" : "Submit & Start Interview"}
              </button>
            )}
          </div>
        </div>

        {patientCase && (
          <aside className={`card ${styles.sidebar}`}>
            <div className={styles.sidebarIdentity}>
              <AppImage
                src={patientCase.image}
                alt={`${patientCase.name} patient portrait`}
                className={styles.sidebarPortrait}
              />
              <div>
                <div className={styles.sidebarName}>{patientCase.name}</div>
                <div className={styles.sidebarMeta}>
                  {patientCase.age === 1 ? "1 year" : `${patientCase.age} years`}
                  {patientCase.patientType ? ` · ${patientCase.patientType}` : ""}
                </div>
              </div>
            </div>
            {patientCase.referralReason && (
              <div className={styles.sidebarBlock}>
                <span className={styles.sidebarLabel}>Context</span>
                <p className={styles.sidebarBody}>{patientCase.referralReason}</p>
              </div>
            )}
            {patientCase.task && (
              <div className={styles.sidebarBlock}>
                <span className={styles.sidebarLabel}>Your Task</span>
                <p className={styles.sidebarBody}>{patientCase.task}</p>
              </div>
            )}
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => navigate(studentHome)}
            >
              Exit
            </button>
          </aside>
        )}
      </div>
    </div>
  );
}
