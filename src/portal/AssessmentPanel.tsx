import { useEffect, useRef, useState } from "react";
import type { Assessment } from "../types/assessment";
import {
  ApiError,
  createAssessment,
  fetchLatestAssessment,
  getAssessmentStatus,
} from "../services/api";
import { AssessmentDisplay } from "./AssessmentDisplay";
import { EmptyState, Spinner } from "./ui";

/**
 * Assessment tab body with safe FAILED-retry recovery.
 *
 * This is a recovery-only UI on top of the EXISTING backend retry flow
 * (POST /api/sessions/{id}/assessment?retry=true + the assessment status
 * poll). It never restarts the interview, never recreates the session, and
 * never touches the transcript - it only asks the backend to generate a new
 * assessment attempt from the already-saved session + transcript. The failed
 * attempt is preserved server-side (a new run row is created).
 *
 * Phases:
 *   complete     -> render the finished AssessmentDisplay
 *   in_progress  -> disabled "Generating Assessment..." + status polling
 *   failed        -> friendly message + a retry button (completed sessions only)
 *   none          -> no assessment has ever been generated
 */

type Phase = "none" | "in_progress" | "complete" | "failed";

const IN_PROGRESS = new Set(["PENDING", "PROCESSING", "VERIFYING"]);
const COMPLETE = new Set(["COMPLETE", "NEEDS_REVIEW"]);

function phaseFromAssessment(a: Assessment | null): Phase {
  if (!a) return "none";
  if (COMPLETE.has(a.status)) return "complete";
  if (a.status === "FAILED") return "failed";
  if (IN_PROGRESS.has(a.status)) return "in_progress";
  return "none";
}

export function AssessmentPanel({
  sessionId,
  sessionStatus,
  assessment,
  variant,
}: {
  sessionId: string | undefined;
  sessionStatus: string;
  assessment: Assessment | null;
  variant: "student" | "admin";
}) {
  const [phase, setPhase] = useState<Phase>(() => phaseFromAssessment(assessment));
  const [current, setCurrent] = useState<Assessment | null>(assessment);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  // Guards against re-entrancy / double-clicks while a retry request is inflight.
  const submittingRef = useRef(false);

  // Poll the backend's existing status endpoint while an assessment is running.
  // No second polling architecture: this reuses getAssessmentStatus, exactly
  // like the initial AssessmentLoadingPage flow.
  useEffect(() => {
    if (phase !== "in_progress" || !sessionId) return;
    let active = true;

    const tick = async () => {
      try {
        const res = await getAssessmentStatus(sessionId);
        if (!active) return;
        if (res.status === "completed") {
          const latest = await fetchLatestAssessment(sessionId);
          if (!active) return;
          setCurrent(latest);
          setErrorCode(null);
          setPhase("complete");
        } else if (res.status === "failed") {
          setErrorCode(res.error_code ?? null);
          setPhase("failed");
        }
        // pending / processing / verifying / not_started -> keep waiting
      } catch {
        // Transient poll error: keep polling; the interval will retry.
      }
    };

    tick();
    const id = setInterval(tick, 2500);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [phase, sessionId]);

  const canRetry = sessionStatus === "completed";

  const handleRetry = async () => {
    if (!sessionId || submittingRef.current || phase === "in_progress") return;
    submittingRef.current = true;
    setErrorCode(null);
    setPhase("in_progress"); // immediately disables the button + starts polling
    try {
      // Existing retry API. The backend enqueues a NEW attempt and preserves the
      // failed run; its partial unique index also blocks duplicate active runs.
      await createAssessment(sessionId, true);
    } catch (e) {
      // Enqueue itself was rejected (e.g. network). Drop back to failed so the
      // user can try again; do NOT leak internal details to students.
      setPhase("failed");
      setErrorCode(e instanceof ApiError ? e.code : null);
    } finally {
      submittingRef.current = false;
    }
  };

  if (phase === "complete") {
    return <AssessmentDisplay assessment={current} />;
  }

  if (phase === "in_progress") {
    return (
      <div className="pt-card pt-section">
        <Spinner label="Generating Assessment…" />
        <p className="pt-muted" style={{ textAlign: "center", margin: 0 }}>
          Your interview has been saved. This page will update automatically.
        </p>
        <div className="pt-row" style={{ justifyContent: "center", marginTop: 12 }}>
          <button className="pt-btn pt-btn-sm" disabled>
            Generating Assessment…
          </button>
        </div>
      </div>
    );
  }

  if (phase === "failed") {
    return (
      <div className="pt-card pt-section">
        <h3 style={{ marginTop: 0 }}>Assessment generation failed</h3>
        <p className="pt-sub" style={{ marginBottom: 4 }}>
          Your interview and transcript were saved successfully.
          {canRetry ? " You can try generating the assessment again." : ""}
        </p>
        {variant === "admin" && errorCode && (
          <p className="pt-muted" style={{ fontSize: "0.8rem", marginTop: 0 }}>
            Error code: {errorCode}
          </p>
        )}
        {canRetry && (
          <div className="pt-row" style={{ marginTop: 12 }}>
            <button className="pt-btn pt-btn-sm" onClick={handleRetry}>
              {variant === "admin" ? "Retry Assessment" : "Generate Assessment Again"}
            </button>
          </div>
        )}
      </div>
    );
  }

  // phase === "none"
  if (variant === "student") {
    return (
      <EmptyState
        title="No assessment available"
        hint="An AI assessment has not been generated for this session yet."
      />
    );
  }
  return <AssessmentDisplay assessment={null} />;
}
