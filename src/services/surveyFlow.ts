/**
 * Pure navigation decisions for the survey flow. Kept dependency-free so it can
 * be unit-tested directly (see scripts/test-survey-flow.mjs).
 */

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
