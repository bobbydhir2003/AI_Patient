/**
 * Silent, admin-only "active assessment viewing time" heartbeat.
 *
 * The client sends NO duration — the server credits time from its own
 * timestamps. This call is deliberately best-effort: it never throws to the
 * caller, so a failed heartbeat can never disrupt the student's assessment view.
 * The student sees nothing from this at any point.
 */
import { API_BASE_URL, getStoredAuthToken } from "./api";

export async function pingAssessmentView(assessmentId: string): Promise<void> {
  const token = getStoredAuthToken();
  if (!token) return;
  try {
    await fetch(
      `${API_BASE_URL}/api/assessments/${encodeURIComponent(assessmentId)}/view/ping`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        // Allow a final ping to complete even if the tab is being closed.
        keepalive: true,
      },
    );
  } catch {
    /* best-effort: ignore network/other errors */
  }
}
