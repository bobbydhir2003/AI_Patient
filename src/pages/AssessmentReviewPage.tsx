import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ApiError,
  createAssessment,
  fetchAssessmentTranscript,
  fetchLatestAssessment,
  fetchRubrics,
} from "../services/api";
import { useCases } from "../services/cases";
import { AppImage } from "../components/common/AppImage";
import { AssessmentHeader, type AssessmentTab } from "../components/assessment/AssessmentHeader";
import { OverallImpression } from "../components/assessment/OverallImpression";
import { RubricCard } from "../components/assessment/RubricCard";
import { FocusAreas } from "../components/assessment/FocusAreas";
import { TranscriptReview } from "../components/assessment/TranscriptReview";
import { AssessmentMethodPanel } from "../components/assessment/AssessmentMethodPanel";
import { AssessmentStatus } from "../components/assessment/AssessmentStatus";
import { LevelBadge } from "../components/assessment/LevelBadge";
import type { Assessment, AssessmentTurn, Rubric } from "../types/assessment";
import { ReferralAssessmentView } from "../components/referralAssessment/ReferralAssessmentView";
import { useAuth } from "../state/AuthContext";
import { caseHubPath } from "../services/authRouting";
import shared from "../components/assessment/assessment.module.css";
import styles from "./AssessmentReviewPage.module.css";

type Phase = "processing" | "ready" | "failed";

export function AssessmentReviewPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { cases } = useCases();
  const studentHome = caseHubPath(user?.role);

  const [phase, setPhase] = useState<Phase>("processing");
  const [assessment, setAssessment] = useState<Assessment | null>(null);
  const [turns, setTurns] = useState<AssessmentTurn[]>([]);
  const [rubrics, setRubrics] = useState<Rubric[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [tab, setTab] = useState<AssessmentTab>("overview");
  const [targetTurnId, setTargetTurnId] = useState<string | null>(null);
  const [targetEvidenceId, setTargetEvidenceId] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  const load = useCallback(async () => {
    if (!sessionId) return;
    setPhase("processing");
    setErrorMessage(null);
    try {
      let result: Assessment | null = null;
      try {
        const existing = await fetchLatestAssessment(sessionId);
        if (existing.status === "COMPLETE" || existing.status === "NEEDS_REVIEW") {
          result = existing;
        }
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 404)) throw err;
      }
      if (!result) {
        // POST is idempotent on the backend: it returns the existing
        // assessment for a completed session or runs the full three-stage
        // AI pipeline. No fake feedback on failure.
        result = await createAssessment(sessionId);
      }
      const [transcript, rubricList] = await Promise.all([
        fetchAssessmentTranscript(result.assessmentId),
        fetchRubrics().catch(() => [] as Rubric[]),
      ]);
      setAssessment(result);
      setTurns(transcript);
      setRubrics(rubricList);
      setPhase("ready");
    } catch (err) {
      // Raw transport details stay in the console; students see a
      // structured backend message only when one exists.
      console.error("Assessment generation failed:", err);
      const studentSafeCodes = [
        "session_not_found",
        "session_not_completed",
        "assessment_not_possible",
        "ASSESSMENT_UNAVAILABLE",
      ];
      setErrorMessage(
        err instanceof ApiError && studentSafeCodes.includes(err.code) ? err.message : null,
      );
      setPhase("failed");
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load, attempt]);

  const patientCase = useMemo(
    () => cases.find((c) => c.id === assessment?.caseId),
    [cases, assessment],
  );
  const rubricByDomain = useMemo(
    () => new Map(rubrics.map((r) => [r.domain, r])),
    [rubrics],
  );

  function handleViewTranscript(turnId: string, evidenceId: string) {
    setTab("transcript");
    setTargetTurnId(turnId);
    setTargetEvidenceId(evidenceId);
  }

  if (!sessionId) {
    return (
      <div className="page">
        <p>No interview session was provided.</p>
        <button type="button" className="btn btn-primary" onClick={() => navigate(studentHome)}>
          Back to Case Selection
        </button>
      </div>
    );
  }

  if (phase !== "ready" || !assessment) {
    return (
      <div className="page">
        {phase === "processing" ? (
          <AssessmentStatus phase="processing" onRetry={() => setAttempt((n) => n + 1)} />
        ) : (
          <AssessmentStatus
            phase="failed"
            friendlyMessage={errorMessage}
            onRetry={() => setAttempt((n) => n + 1)}
            onBack={() => navigate(studentHome)}
          />
        )}
      </div>
    );
  }

  if (assessment.assessmentMode === "advanced_referral" && assessment.referral) {
    return (
      <ReferralAssessmentView
        assessment={assessment}
        patientCase={patientCase}
        turns={turns}
      />
    );
  }

  const durationText = (() => {
    if (!turns.length) return null;
    const start = new Date(turns[0].timestamp).getTime();
    const end = new Date(turns[turns.length - 1].timestamp).getTime();
    const totalSeconds = Math.max(0, Math.floor((end - start) / 1000));
    return `${Math.floor(totalSeconds / 60)} min ${(totalSeconds % 60).toString().padStart(2, "0")} sec · ${turns.length} turns`;
  })();

  return (
    <div className={`page ${styles.wide}`}>
      <div className={styles.layout}>
        {/* ---------------- left sidebar ---------------- */}
        <aside className={styles.column}>
          <div className={`card ${styles.sideCard}`}>
            {patientCase && (
              <div className={styles.patientRow}>
                <AppImage
                  src={patientCase.image}
                  alt={`${patientCase.name} patient portrait`}
                  className={styles.patientImage}
                />
                <div>
                  <p className={styles.patientName}>{patientCase.name}</p>
                  <p className={styles.patientMeta}>
                    {patientCase.age} years old · {assessment.caseId} case
                  </p>
                </div>
              </div>
            )}
            <div>
              <p className={styles.patientName}>Interview Completed</p>
              <p className={styles.metaLine}>
                {new Date(assessment.createdAt).toLocaleString()}
              </p>
              {durationText && <p className={styles.metaLine}>{durationText}</p>}
            </div>
            <div className={styles.stepList}>
              <span className={styles.stepRow}>
                <span className={styles.stepDone}>✓</span> Case Introduction
              </span>
              <span className={styles.stepRow}>
                <span className={styles.stepDone}>✓</span> Interview
              </span>
              <span className={styles.stepRow}>
                <span className={styles.stepDone}>✓</span> AI Assessment
              </span>
              <span className={`${styles.stepRow} ${styles.stepCurrent}`}>4&nbsp; Review</span>
            </div>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setTab("transcript")}
            >
              View Full Transcript
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => navigate(studentHome)}>
              Try Another Case
            </button>
          </div>
        </aside>

        {/* ---------------- center content ---------------- */}
        <section className={styles.column}>
          <AssessmentHeader tab={tab} onTabChange={setTab} />

          {tab === "overview" && (
            <>
              <OverallImpression assessment={assessment} />
              <div className={styles.rubricGrid}>
                {assessment.domains.map((domain) => (
                  <RubricCard
                    key={domain.rubricDomain}
                    domain={domain}
                    rubric={rubricByDomain.get(domain.rubricDomain)}
                    onViewTranscript={handleViewTranscript}
                  />
                ))}
              </div>
              <FocusAreas areas={assessment.focusAreas} />
            </>
          )}

          {tab === "rubrics" && (
            <div className={styles.rubricGrid}>
              {assessment.domains.map((domain) => (
                <RubricCard
                  key={domain.rubricDomain}
                  domain={domain}
                  rubric={rubricByDomain.get(domain.rubricDomain)}
                  onViewTranscript={handleViewTranscript}
                />
              ))}
            </div>
          )}

          {tab === "transcript" && (
            <div className={`card ${shared.sectionCard}`}>
              <h2 className={shared.sectionTitle}>Transcript Review</h2>
              <p className={shared.mutedText}>
                Your complete saved interview with AI feedback markers. Click a marker to see the
                explanation, why it mattered, and a stronger alternative.
              </p>
              <TranscriptReview
                turns={turns}
                rubricDomains={assessment.domains.map((d) => d.rubricDomain)}
                targetTurnId={targetTurnId}
                targetEvidenceId={targetEvidenceId}
              />
            </div>
          )}

          {tab === "method" && (
            <div className={`card ${shared.sectionCard}`}>
              <h2 className={shared.sectionTitle}>How This Assessment Works</h2>
              <p className={shared.mutedText}>
                Completed Transcript → Case-Specific Reference → Rubric Evidence Extraction → AI
                Rubric Evaluation → AI Verification → Student Review
              </p>
              <ul className={shared.plainList}>
                <li>The four rubrics are defined by PT educators; the AI applies them by reading your actual conversation.</li>
                <li>No keyword counting, fixed answer matching, or automatic credit exists anywhere in this system.</li>
                <li>Case-specific expectations are used, so each patient is assessed differently and fairly.</li>
                <li>Every feedback item must cite a real transcript turn; unsupported feedback is rejected by a second AI review.</li>
                <li>Performance signals are purely qualitative: Advanced, Proficient, Developing, Needs Improvement, or Insufficient Evidence — never numeric grades.</li>
                <li>This is a formative review to guide practice; instructor review may be added later.</li>
              </ul>
              <p className={shared.mutedText}>
                AI assessment may make mistakes. Review the linked transcript evidence and discuss
                disputed feedback with an instructor.
              </p>
              <div>
                <span className={shared.listHeading}>Overall signal</span>{" "}
                <LevelBadge level={assessment.overallLevel} />
              </div>
            </div>
          )}
        </section>

        {/* ---------------- right sidebar ---------------- */}
        <aside className={`${styles.column} ${styles.rightColumn}`}>
          <AssessmentMethodPanel assessment={assessment} />
        </aside>
      </div>
    </div>
  );
}
