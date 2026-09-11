import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ProgressSteps } from "../components/layout/ProgressSteps";
import { SurveyLikert } from "../components/survey/SurveyLikert";
import { POST_LIKERT, POST_OPEN_ENDED, OPEN_ENDED_MAX_LEN } from "../services/surveyQuestions";
import { getSurveyStatus, submitPostSurvey } from "../services/surveysApi";
import { ApiError, getAssessmentStatus } from "../services/api";
import { postSurveyDestination, postSurveyGate } from "../services/surveyFlow";
import styles from "./SurveyPage.module.css";

const PROGRESS_STEPS = ["Case Introduction", "Interview", "Post-Survey", "Assessment", "Complete"];

export function PostSurveyPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();

  const [likert, setLikert] = useState<Record<string, number>>({});
  const [openText, setOpenText] = useState<Record<string, string>>({});
  const [nuidMissing, setNuidMissing] = useState(false);
  const [checkingStatus, setCheckingStatus] = useState(true);
  const [alreadyCompleted, setAlreadyCompleted] = useState(false);
  const [caseName, setCaseName] = useState("this case");
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
        setCaseName(status.caseName || "this case");
        if (!status.nuidOnFile) setNuidMissing(true);
        if (postSurveyGate(status.overallStatus) === "completed") {
          setAlreadyCompleted(true);
        }
        setCheckingStatus(false);
      } catch {
        /* best-effort; submission is still guarded server-side */
        if (!cancelled) setCheckingStatus(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  async function handleSubmit() {
    if (alreadyCompleted || submitting || nuidMissing || !sessionId) return;
    const unanswered = POST_LIKERT.some((q) => !likert[q.name]);
    if (unanswered) {
      setError("Please answer all rating questions before continuing.");
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
      } else if (err instanceof ApiError && err.code === "survey_already_completed") {
        // Race: show the completed read-only survey and never retry or call
        // REDCap again.
        setLikert({});
        setOpenText({});
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
        <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={2} />
        <div className={`${styles.single}`}>
          <div className={`card ${styles.card}`} role="status">
            <p>Checking survey status…</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={2} />

      <div className={`${styles.single}`}>
        <div className={styles.headerBlock}>
          <h1 className={styles.title}>Post-Interview Survey</h1>
          <p className={styles.subtitle}>
            Thanks for completing the interview. Please share your experience — your answers are
            recorded for research and do not affect your assessment.
          </p>
        </div>

        <div className={styles.main}>
          {alreadyCompleted && (
            <div className={styles.completedBanner} role="alert">
              <h2 className={styles.completedTitle}>Survey already completed</h2>
              <p className={styles.completedMessage}>
                You have already completed the survey for {caseName}. No additional survey response
                is required.
              </p>
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

          {/* Likert section */}
          <div className={`card ${styles.card} ${alreadyCompleted ? styles.readOnlyCard : ""}`}>
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
                  disabled={alreadyCompleted}
                />
              ))}
            </div>
          </div>

          {/* Open-ended section */}
          <div className={`card ${styles.card} ${alreadyCompleted ? styles.readOnlyCard : ""}`}>
            <h2 className={styles.groupTitle}>Open-Ended Feedback (optional)</h2>
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
                    disabled={alreadyCompleted}
                    value={openText[q.name] ?? ""}
                    onChange={(e) =>
                      setOpenText((t) => ({ ...t, [q.name]: e.target.value }))
                    }
                  />
                </div>
              ))}
            </div>
          </div>

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
