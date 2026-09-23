import { useEffect, useRef } from "react";
import { pingAssessmentView } from "../services/assessmentViewApi";

/** Expected client heartbeat interval (server tolerates one dropped beat). */
const HEARTBEAT_MS = 20_000;

/**
 * Silently track how long the student ACTIVELY views an assessment.
 *
 * Renders nothing and returns nothing — the student never sees a timer or any
 * message. Pings immediately (if visible) then every 20s WHILE the page is
 * visible; pauses when the tab/window is hidden and resumes when visible again.
 * All heartbeats are best-effort and never surface errors. The server credits
 * the actual active time from its own timestamps (the client sends no duration).
 *
 * Pass `null`/`undefined` (e.g. before the assessment is ready) to stay inert.
 */
export function useAssessmentViewTracker(assessmentId: string | null | undefined): void {
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (!assessmentId) return;

    const isVisible = () =>
      typeof document === "undefined" || document.visibilityState === "visible";

    const ping = () => {
      if (isVisible()) void pingAssessmentView(assessmentId);
    };

    const stop = () => {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };

    const start = () => {
      if (timerRef.current !== null) return; // already running
      ping(); // immediate baseline / resume ping (server clamps any long gap to 0)
      timerRef.current = window.setInterval(ping, HEARTBEAT_MS);
    };

    const onVisibility = () => {
      if (isVisible()) start();
      else stop();
    };

    if (isVisible()) start();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
  }, [assessmentId]);
}
