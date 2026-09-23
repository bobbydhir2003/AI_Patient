/**
 * Pure navigation decisions for the survey flow. Kept dependency-free so it can
 * be unit-tested directly (see scripts/test-survey-flow.mjs).
 */

/**
 * True when an error is the backend's specific "session_not_found" 404 (a
 * stale/dead/foreign interview session id). Callers use this to distinguish a
 * dead session (discard + recover) from a generic transient/network error
 * (preserve existing best-effort behavior). Shape-based so this module stays
 * dependency-free (works for the ApiError instances thrown by the API layer).
 */
export function isSessionNotFound(err: unknown): boolean {
  return (
    !!err &&
    typeof err === "object" &&
    "code" in err &&
    (err as { code?: unknown }).code === "session_not_found"
  );
}

export type AssessmentStatusValue =
  | "not_started"
  | "pending"
  | "processing"
  | "verifying"
  | "completed"
  | "failed"
  | string;

/**
 * Where to navigate after the Post-Survey is submitted, given the current
 * assessment status:
 *   - completed → Assessment Review (student sees the result immediately)
 *   - everything else (not_started / pending / processing / verifying / failed /
 *     unknown) → the EXISTING Assessment Loading page, which owns the polling
 *     experience AND the failure/retry UI. We do not invent a second failure
 *     system.
 */
export function postSurveyDestination(
  sessionId: string,
  status: AssessmentStatusValue | null | undefined,
): string {
  if (status === "completed") return `/assessment/${sessionId}`;
  return `/assessment/${sessionId}/loading`;
}

// ---------------------------------------------------------------------------
// Case-level survey gating (one survey package per student + case; Pre and Post
// are the two STAGES of it). These are PURE decisions off the backend's
// case-level survey status, kept here so they can be unit-tested without React.
// ---------------------------------------------------------------------------

export type SurveyOverallStatus = "not_started" | "in_progress" | "completed" | string;

/**
 * What the Pre-Survey page should do for this case:
 *   - "completed": the whole package is already done → show the completed
 *     message and let the student continue to a repeat interview without Pre.
 *   - "resume": Pre already reached REDCap but the package is still in progress
 *     → do NOT re-ask Pre; continue straight to the interview/resume flow.
 *   - "collect": no receipt yet, or Pre not yet done → show the Pre-Survey.
 */
export type PreSurveyGate = "completed" | "resume" | "collect";

export function preSurveyGate(
  overallStatus: SurveyOverallStatus | null | undefined,
  preSubmitted: boolean,
): PreSurveyGate {
  if (overallStatus === "completed") return "completed";
  if (preSubmitted) return "resume";
  return "collect";
}

/** The case-level survey package is fully completed (Pre + Post both received
 * by REDCap). Used by both survey pages to avoid re-submitting. */
export function isSurveyCompleted(
  overallStatus: SurveyOverallStatus | null | undefined,
): boolean {
  return overallStatus === "completed";
}

/** Post is skipped only for a fully completed package. An IN_PROGRESS package
 * still needs Post even when its Pre stage was already submitted. */
export type PostSurveyGate = "completed" | "collect";

export function postSurveyGate(
  overallStatus: SurveyOverallStatus | null | undefined,
): PostSurveyGate {
  return isSurveyCompleted(overallStatus) ? "completed" : "collect";
}

// ---------------------------------------------------------------------------
// GLOBAL survey gating (one survey PACKAGE per student, not per case). The
// backend resolves the owning case + global lifecycle server-side; these pure
// functions turn that into what each survey SCREEN renders. Every survey screen
// stays visible in the flow - a non-owning case shows a skip panel, never a
// silently removed step.
// ---------------------------------------------------------------------------

export type GlobalSurveyStatus = "not_started" | "in_progress" | "completed" | string;

/**
 * What the Pre-Survey screen should do:
 *   - "collect"  : no owner yet (this case may claim it) OR this is the owning
 *                  case and its Pre is not yet submitted → show Pre questions.
 *   - "continue" : this IS the owning case and its Pre is already submitted →
 *                  do not re-ask; show a "continue to interview" affordance.
 *   - "skip"     : a DIFFERENT case owns the package (in progress or completed)
 *                  → show "already submitted, thank you" + Skip & Continue.
 */
export type GlobalPreGate = "collect" | "continue" | "skip";

export function globalPreGate(
  globalStatus: GlobalSurveyStatus | null | undefined,
  isOwnerCase: boolean,
  preSubmitted: boolean,
): GlobalPreGate {
  if (globalStatus === "not_started") return "collect";
  if (isOwnerCase) return preSubmitted ? "continue" : "collect";
  return "skip";
}

/**
 * What the Post-Survey screen should do:
 *   - "collect" : this IS the owning case and the package is not yet completed
 *                 → show Post questions.
 *   - "skip"    : any other case, or the package is already completed → show
 *                 "already submitted" + Skip & Continue to Assessment.
 */
export type GlobalPostGate = "collect" | "skip";

export function globalPostGate(
  globalStatus: GlobalSurveyStatus | null | undefined,
  isOwnerCase: boolean,
): GlobalPostGate {
  return isOwnerCase && globalStatus !== "completed" ? "collect" : "skip";
}

// ---------------------------------------------------------------------------
// Centralized interview flow resolution. One place decides where a session
// belongs so NEW, RESUME, refresh and dashboard-continue all agree instead of
// hard-coding navigation in each component. Pure + dependency-free (unit-tested
// in scripts/test-survey-flow.mjs).
// ---------------------------------------------------------------------------

export interface SessionFlowState {
  status: string; // "active" | "completed" | ...
  locked: boolean;
  hasAssessment: boolean;
}

export type InterviewDestination =
  | { kind: "caseInfo" }
  | { kind: "interview" }
  | { kind: "postSurvey" }
  | { kind: "assessmentLoading" }
  | { kind: "assessment" };

/** A session is genuinely resumable into the live interview only while it is
 * ACTIVE and not locked. */
export function isResumableSession(session: SessionFlowState | null | undefined): boolean {
  return !!session && session.status === "active" && !session.locked;
}

/**
 * Where a RESUME should ENTER from the dashboard "Continue Last Session":
 *   - resumable active session → the Case Information screen (the full workflow
 *     is shown; the existing session is reused later, no new session/queue).
 *   - completed with an assessment → the Assessment review.
 *   - completed without an assessment yet → the Post-Survey step.
 * This never sends an active resume straight to the interview.
 */
export function resumeEntryDestination(session: SessionFlowState): InterviewDestination {
  if (isResumableSession(session)) return { kind: "caseInfo" };
  if (session.hasAssessment) return { kind: "assessment" };
  return { kind: "postSurvey" };
}

/**
 * Where the student should go AFTER the (resume) Pre-Survey step, based on the
 * session's CURRENT backend state — so a session that went stale (locked /
 * completed) while the student sat on Case Info / Pre-Survey is routed safely
 * instead of blindly entering the interview.
 */
export function resolveInterviewDestination(
  session: SessionFlowState | null,
): InterviewDestination {
  if (isResumableSession(session)) return { kind: "interview" };
  if (!session) return { kind: "caseInfo" };
  if (session.hasAssessment) return { kind: "assessment" };
  return { kind: "postSurvey" };
}

/** Turn a resolved destination into a concrete route. Kept here (pure) so every
 * caller builds the SAME URLs from the SAME decision. */
export function destinationToPath(
  dest: InterviewDestination,
  ids: { caseId: string; sessionId: string },
): string {
  switch (dest.kind) {
    case "caseInfo":
      return `/cases/${ids.caseId}`;
    case "interview":
      return `/interview/${ids.caseId}`;
    case "postSurvey":
      return `/survey/${ids.sessionId}/post`;
    case "assessmentLoading":
      return `/assessment/${ids.sessionId}/loading`;
    case "assessment":
      return `/assessment/${ids.sessionId}`;
  }
}
