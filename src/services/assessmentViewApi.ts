/**
 * Silent, admin-only "active assessment viewing time" heartbeat.
 *
 * The client sends NO duration — the server credits time from its own
 * timestamps. This call is deliberately best-effort: it never throws to the
 * caller, so a failed heartbeat can never disrupt the student's assessment view.
 * The student sees nothing from this at any point.
 */
import { API_BASE_URL, getStoredAuthToken } from "./api";

/** Where a visit was opened from. Sent with every ping but only recorded by the
 * server when the visit is first created (it never changes afterwards). */
export type AssessmentVisitSource = "initial_assessment" | "student_dashboard";

/** Active-timer transition this ping represents. The server credits the bounded
 * elapsed interval for "heartbeat"/"pause"; "resume" credits 0 and rebaselines,
 * so the hidden/away gap it ends is never counted. The client NEVER sends a
 * duration — only which transition happened; the server uses its own clock. */
export type AssessmentViewEvent = "heartbeat" | "pause" | "resume";

export async function pingAssessmentView(
  assessmentId: string,
  visitId: string,
  source: AssessmentVisitSource,
  event: AssessmentViewEvent,
): Promise<void> {
  const token = getStoredAuthToken();
  if (!token) return;
  try {
    await fetch(
      `${API_BASE_URL}/api/assessments/${encodeURIComponent(assessmentId)}/view/ping`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        // Opaque per-visit id + its source enum + the timer transition only; the
        // server credits time from its own timestamps and derives all identity.
        body: JSON.stringify({ visitId, source, event }),
        // Allow a final ping (pause on leave/close) to complete even as the tab
        // is being torn down. keepalive is required because sendBeacon cannot set
        // the Authorization header this endpoint needs.
        keepalive: true,
      },
    );
  } catch {
    /* best-effort: ignore network/other errors */
  }
}
