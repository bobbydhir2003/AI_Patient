import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ProgressSteps } from "../components/layout/ProgressSteps";
import { AppImage } from "../components/common/AppImage";
import { SurveyLikert } from "../components/survey/SurveyLikert";
import { PRE_LIKERT } from "../services/surveyQuestions";
import { getSurveyStatus, submitPreSurvey } from "../services/surveysApi";
import { preSurveyGate } from "../services/surveyFlow";
import { ApiError, createSession } from "../services/api";
import { joinQueue } from "../services/queueApi";
import { usePatientCase } from "../services/cases";
import { useAppContext } from "../state/AppContext";
import { useAuth } from "../state/AuthContext";
import { caseHubPath } from "../services/authRouting";
import styles from "./SurveyPage.module.css";

const PROGRESS_STEPS = ["Case Introduction", "Pre-Survey", "Interview", "Assessment", "Complete"];

export function PreSurveyPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const { user, token } = useAuth();
  const { patientCase } = usePatientCase(caseId);
  const { studentName, studentId, activeInterview, setActiveInterview } = useAppContext();
  const studentHome = caseHubPath(user?.role);

  const [answers, setAnswers] = useState<Record<string, number>>({});
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [checkingStatus, setCheckingStatus] = useState(true);
  const [nuidMissing, setNuidMissing] = useState(false);
  // The one survey package for this case is already fully completed (Pre+Post).
  // Only the survey is skipped; the new/repeated interview remains available.
  const [alreadyCompleted, setAlreadyCompleted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const initRef = useRef(false);

  // Advance from the pre-survey into the interview, preserving the EXISTING
  // capacity/queue behavior (previously run on the Case Introduction page).
  async function proceedToInterview(id: string) {
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

  // Establish (or resume) the backend session so the pre-survey can be linked to
  // it, then read survey status to handle already-submitted / missing-NUID. The
  // interview page's own resume logic reuses this same session (no duplicate).
  useEffect(() => {
    if (!caseId) return;
    let cancelled = false;

    async function init(id: string) {
      // Yield one tick so React StrictMode's throwaway first mount is cancelled
      // before it can POST a duplicate session.
      await new Promise((resolve) => window.setTimeout(resolve, 0));
      if (cancelled || initRef.current) return;
      initRef.current = true;
      try {
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
          setActiveInterview({ caseId: id, sessionId: session.sessionId, startedAt: Date.now() });
          sid = session.sessionId;
        }
        if (cancelled) return;
        setSessionId(sid);
        try {
          const status = await getSurveyStatus(sid);
          if (cancelled) return;
          if (!status.nuidOnFile) setNuidMissing(true);
          const gate = preSurveyGate(status.overallStatus, status.preSubmitted);
          if (gate === "completed") {
            setAlreadyCompleted(true);
            setCheckingStatus(false);
          } else if (gate === "resume") {
            // Pre already synced but the package is still in progress — do NOT
            // re-ask Pre; continue straight to the interview/resume flow.
            void proceedToInterview(id);
          } else {
            setCheckingStatus(false);
          }
          // gate === "collect": no receipt yet, or Pre not done → show normally.
        } catch {
          /* status is best-effort; submission is still guarded server-side */
          if (!cancelled) setCheckingStatus(false);
        }
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
    if (alreadyCompleted || submitting || nuidMissing || !sessionId || !caseId) return;
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
      } else if (err instanceof ApiError && err.code === "survey_already_completed") {
        // Race: the package completed between load and submit. Show the same
        // read-only survey and never retry the survey submission.
        setAnswers({});
        setAlreadyCompleted(true);
      } else {
        setError(
          err instanceof ApiError ? err.message : "Could not submit the survey. Please try again.",
        );
      }
      setSubmitting(false);
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

      {alreadyCompleted && (
        <div className={styles.completedBanner} role="alert">
          <h2 className={styles.completedTitle}>Survey already completed</h2>
          <p className={styles.completedMessage}>
            You have already completed the survey for {patientCase?.name ?? "this case"}. You do
            not need to complete it again.
          </p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => caseId && void proceedToInterview(caseId)}
            disabled={!sessionId}
          >
            Skip Survey &amp; Continue to Interview
          </button>
        </div>
      )}

      <div className={styles.layout}>
        <div className={styles.main}>
          {!alreadyCompleted && nuidMissing && (
            <div className={styles.banner} role="alert">
              Your student number (NUID) is missing from your profile. Add it to your profile before
              submitting the survey. You can still start the interview from the case page.
            </div>
          )}

          <div className={`card ${styles.card} ${alreadyCompleted ? styles.readOnlyCard : ""}`}>
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
                  disabled={alreadyCompleted}
                />
              ))}
            </div>
          </div>

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
            {!alreadyCompleted && (
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
                <span className={styles.sidebarLabel}>Referral Reason</span>
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
