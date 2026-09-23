import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ProgressSteps } from "../components/layout/ProgressSteps";
import { SurveyLikert } from "../components/survey/SurveyLikert";
import { POST_LIKERT, POST_OPEN_ENDED, OPEN_ENDED_MAX_LEN } from "../services/surveyQuestions";
import { getSurveyStatus, submitPostSurvey } from "../services/surveysApi";
import { ApiError, getAssessmentStatus } from "../services/api";
import { postSurveyDestination, globalPostGate, isSessionNotFound } from "../services/surveyFlow";
import { useAppContext } from "../state/AppContext";
import { useAuth } from "../state/AuthContext";
import { caseHubPath } from "../services/authRouting";
import styles from "./SurveyPage.module.css";

const PROGRESS_STEPS = ["Case Introduction", "Pre Survey", "Interview", "Post Survey", "Assessment Results"];

export function PostSurveyPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const { setActiveInterview } = useAppContext();
  const { user } = useAuth();

  const [likert, setLikert] = useState<Record<string, number>>({});
  const [openText, setOpenText] = useState<Record<string, string>>({});
  const [nuidMissing, setNuidMissing] = useState(false);
  const [checkingStatus, setCheckingStatus] = useState(true);
  const [alreadyCompleted, setAlreadyCompleted] = useState(false);
  // The URL session id is dead or not ours (404 session_not_found): never render
  // or submit the post-survey against it; show a safe recovery panel instead.
  const [sessionMissing, setSessionMissing] = useState(false);
  // Owner-aware context for the skip messaging.
  const [ownerName, setOwnerName] = useState<string | null>(null);
  const [globalStatus, setGlobalStatus] = useState<string>("not_started");
  const [currentCaseName, setCurrentCaseName] = useState<string>("this case");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const routedRef = useRef(false);

  // Route to the assessment AFTER the student submits. The assessment has been
  // generating in the background since End Interview; we only look at its status
  // here — we never show progress UI and never auto-redirect mid-survey.
  async function routeToAssessment(id: string) {
    if (routedRef.current) return;
    routedRef.current = true;
    let status: string | null = null;
    try {
      status = (await getAssessmentStatus(id)).status;
    } catch {
      /* fall through: postSurveyDestination sends unknown status to the loading
         page, which polls + owns the failure/retry experience */
    }
    navigate(postSurveyDestination(id, status), { replace: true });
  }

  // Read the authenticated, session-scoped case status once. A fully completed
  // package gets an explicit skip panel; IN_PROGRESS still collects Post.
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    (async () => {
      try {
        const status = await getSurveyStatus(sessionId);
        if (cancelled) return;
        if (!status.nuidOnFile) setNuidMissing(true);
        setOwnerName(status.surveyOwnerCaseName);
        setGlobalStatus(status.globalSurveyStatus);
        if (status.caseName) setCurrentCaseName(status.caseName);
        // GLOBAL gate: only the owning case whose package is not yet completed
        // collects Post. Every other case (and a completed package) shows the
        // "already submitted / skip" state.
        if (globalPostGate(status.globalSurveyStatus, status.isSurveyOwnerCase) === "skip") {
          setAlreadyCompleted(true);
        }
        setCheckingStatus(false);
      } catch (err) {
        if (cancelled) return;
        if (isSessionNotFound(err)) {
          // Dead/stale session id: discard any stale pointer and switch to the
          // recovery panel. Do NOT fall through to rendering a submittable survey
          // bound to the dead id.
          setActiveInterview(null);
          setSessionMissing(true);
          setCheckingStatus(false);
          return;
        }
        /* other errors: best-effort; submission is still guarded server-side */
        setCheckingStatus(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Scroll/focus the FIRST unanswered required question (Likert first, then the
  // open-ended). Best-effort and safe: no-op if the element isn't found.
  function focusFirstMissing() {
    const missLikert = POST_LIKERT.find((q) => !likert[q.name]);
    const missOpen = POST_OPEN_ENDED.find((q) => !(openText[q.name] ?? "").trim());
    const targetId = missLikert ? `${missLikert.name}-1` : missOpen ? missOpen.name : null;
    if (!targetId) return;
    const el = document.getElementById(targetId);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
    el?.focus?.({ preventScroll: true });
  }

  async function handleSubmit() {
    if (alreadyCompleted || submitting || nuidMissing || !sessionId) return;
    const likertMissing = POST_LIKERT.some((q) => !likert[q.name]);
    // Every visible open-ended question is required; whitespace-only counts empty.
    const openMissing = POST_OPEN_ENDED.some((q) => !(openText[q.name] ?? "").trim());
    if (likertMissing || openMissing) {
      // Client-side gate: never call the API for a missing answer, and never show
      // a raw 4xx. Keep the student's entered answers and point them at the gap.
      setError("Please answer all required questions before continuing.");
      focusFirstMissing();
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const answers: Record<string, number | string> = { ...likert };
      for (const q of POST_OPEN_ENDED) answers[q.name] = openText[q.name] ?? "";
      await submitPostSurvey(sessionId, answers);
      await routeToAssessment(sessionId);
    } catch (err) {
      if (err instanceof ApiError && err.code === "nuid_missing") {
        setNuidMissing(true);
        setError(err.message);
        setSubmitting(false);
      } else if (
        err instanceof ApiError &&
        (err.code === "survey_already_completed" || err.code === "survey_owned_by_other_case")
      ) {
        // Race: the one global package was completed / is owned elsewhere. Show
        // the skip state and never retry or call REDCap again.
        setLikert({});
        setOpenText({});
        setAlreadyCompleted(true);
        setSubmitting(false);
      } else if (err instanceof ApiError && err.code === "session_not_found") {
        // The bound (completed-interview) session is gone. A Post-Survey cannot be
        // re-bound to a fresh session, so switch to the safe recovery panel rather
        // than showing the raw "session not found" text or submitting to a 404.
        setActiveInterview(null);
        setSessionMissing(true);
        setSubmitting(false);
      } else if (err instanceof ApiError && err.status === 422) {
        // Backend validation backstop: map to the SAME friendly message instead
        // of the raw "Request failed with status 422".
        setError("Please answer all required questions before continuing.");
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
        <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={3} />
        <div className={`${styles.single}`}>
          <div className={`card ${styles.card}`} role="status">
            <p>Checking survey status…</p>
          </div>
        </div>
      </div>
    );
  }

  // Dead/stale session: render a safe recovery panel (never the survey) and send
  // the student back to their dashboard/case hub instead of submitting to a 404.
  if (sessionMissing) {
    return (
      <div className="page">
        <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={3} />
        <div className={`${styles.single}`}>
          <div className={styles.completedBanner} role="alert">
            <h2 className={styles.completedTitle}>This interview session is no longer available</h2>
            <p className={styles.completedMessage}>
              Your previous session has expired or was removed, so this survey can’t be submitted.
              Please return to your dashboard and start the case again if needed.
            </p>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => navigate(caseHubPath(user?.role), { replace: true })}
            >
              Back to Dashboard
            </button>
          </div>
        </div>
      </div>
    );
  }

  // Owner-aware copy for the skip banner (only shown when alreadyCompleted).
  // Owner name/current case fall back safely; never crashes.
  const ownerLabel = ownerName || "your original survey case";
  const isCompleted = globalStatus === "completed";
  const skipBanner = isCompleted
    ? {
        // Case C / D — global survey COMPLETED (owner or non-owner).
        title: "Survey already completed",
        message: `You already completed your survey with ${ownerLabel}. No additional survey is required for ${currentCaseName}.`,
      }
    : {
        // Case B — non-owner case while the owner's survey is IN_PROGRESS.
        title: "No additional survey required",
        message: `Your survey is being completed with ${ownerLabel}. No survey response is required for ${currentCaseName}.`,
      };

  return (
    <div className="page">
      <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={3} />

      <div className={`${styles.single}`}>
        <div className={styles.headerBlock}>
          <h1 className={styles.title}>Post-Interview Survey</h1>
          <p className={styles.subtitle}>
            Thanks for completing the interview. Please share your experience — your answers are
            recorded for research and do not affect your assessment.
          </p>
        </div>

        <div className={styles.main}>
          {/* Validation/submit errors surface near the TOP of the survey so a
              student never misses why submission was blocked. */}
          {error && (
            <div className={styles.errorText} role="alert">
              {error}
            </div>
          )}
          {alreadyCompleted && (
            <div className={styles.completedBanner} role="alert">
              <h2 className={styles.completedTitle}>{skipBanner.title}</h2>
              <p className={styles.completedMessage}>{skipBanner.message}</p>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => sessionId && void routeToAssessment(sessionId)}
              >
                Skip Survey &amp; Continue to Assessment
              </button>
            </div>
          )}

          {/* Subtle, non-blocking note. No spinner, progress bar, or percentage. */}
          <div className={styles.note} role="status">
            <span className={styles.noteDot} aria-hidden="true" />
            Your assessment is being prepared in the background.
          </div>

          {!alreadyCompleted && nuidMissing && (
            <div className={styles.banner} role="alert">
              Your student number (NUID) is missing from your profile. Add it to your profile before
              submitting the survey.
            </div>
          )}

          {/* In the skip/completed state the survey is already handled globally,
              so the Likert AND open-ended cards are HIDDEN entirely (not rendered
              read-only). The banner, assessment note and navigation stay. */}
          {!alreadyCompleted && (
            <>
              {/* Likert section */}
              <div className={`card ${styles.card}`}>
                <p className={styles.scaleLegend}>
                  Rate each statement from 1 (Strongly disagree) to 5 (Strongly agree).
                </p>
                <div className={styles.questionList}>
                  {POST_LIKERT.map((q, i) => (
                    <SurveyLikert
                      key={q.name}
                      name={q.name}
                      index={i + 1}
                      prompt={q.prompt}
                      value={likert[q.name]}
                      onChange={(value) => setLikert((a) => ({ ...a, [q.name]: value }))}
                    />
                  ))}
                </div>
              </div>

              {/* Open-ended section */}
              <div className={`card ${styles.card}`}>
                <h2 className={styles.groupTitle}>Open-Ended Feedback</h2>
                <div className={styles.questionList}>
                  {POST_OPEN_ENDED.map((q, i) => (
                    <div key={q.name} className={styles.openItem}>
                      <label className={styles.openPrompt} htmlFor={q.name}>
                        <span style={{ color: "var(--color-text-muted)", fontWeight: 700 }}>
                          {POST_LIKERT.length + i + 1}.
                        </span>{" "}
                        {q.prompt}
                      </label>
                      <textarea
                        id={q.name}
                        className={styles.textarea}
                        maxLength={OPEN_ENDED_MAX_LEN}
                        value={openText[q.name] ?? ""}
                        onChange={(e) =>
                          setOpenText((t) => ({ ...t, [q.name]: e.target.value }))
                        }
                      />
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}

          {error && (
            <div className={styles.errorText} role="alert">
              {error}
            </div>
          )}

          {!alreadyCompleted && (
            <div className={styles.actions} style={{ justifyContent: "flex-end" }}>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void handleSubmit()}
                disabled={submitting || nuidMissing || !sessionId}
              >
                {submitting ? "Submitting…" : "Submit & Continue"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
