import { useEffect, useRef } from "react";
import { pingAssessmentView, type AssessmentVisitSource } from "../services/assessmentViewApi";

/** Expected client heartbeat interval (server tolerates one dropped beat). */
const HEARTBEAT_MS = 20_000;

/**
 * Silently track how long the student ACTIVELY views an assessment, as a hidden
 * active timer. Renders nothing and returns nothing — the student never sees a
 * timer or any message.
 *
 * Active-state model (the server credits time from its OWN timestamps; the client
 * only says WHICH transition happened, never a duration):
 *   - becomes actively visible -> send "resume"  (server: existing visit credits
 *     0 and rebaselines; a brand-new visitId is created and credits 0)
 *   - while visible            -> send "heartbeat" every 20s (server banks the
 *     elapsed interval)
 *   - becomes inactive (tab hidden, Assessment->Transcript, navigation/unmount,
 *     pagehide) -> send "pause" (server banks the FINAL partial interval, so a
 *     5s/10s/35s visit records ~5/~10/~35 instead of 0/0/20)
 * Hidden/background time is never credited: no heartbeats fire while hidden, and
 * the next "resume" credits 0 for the gap. Every ping advances the server-side
 * baseline, so a duplicate pause (the visibilitychange+pagehide+unmount burst)
 * credits ~0 the second time — no double credit.
 *
 * Pass `null`/`undefined` (e.g. before the assessment is ready, or when a sibling
 * Transcript tab is active) to stay inert; the transition INTO `null` runs the
 * cleanup, which sends the final "pause".
 *
 * ``visitId`` identifies ONE real page visit and MUST be stable for the lifetime
 * of that visit (create it once per page mount with useRef — never per render).
 * The same visitId is reused across visibility hide/show and Assessment↔Transcript
 * toggles within the same page mount, so the server treats them as one visit; a
 * later dashboard reopen is a new mount => new visitId => new visit.
 *
 * ``source`` says where the visit was opened from (fixed per page: the
 * post-interview review page vs. the dashboard reopen); the server records it
 * once, when the visit is created, and pause/resume never change it.
 */
export function useAssessmentViewTracker(
  assessmentId: string | null | undefined,
  visitId: string,
  source: AssessmentVisitSource,
): void {
  const timerRef = useRef<number | null>(null);
  const activeRef = useRef(false);

  useEffect(() => {
    if (!assessmentId) return;

    const isVisible = () =>
      typeof document === "undefined" || document.visibilityState === "visible";

    const send = (event: "heartbeat" | "pause" | "resume") =>
      void pingAssessmentView(assessmentId, visitId, source, event);

    // Enter an active period: resume timing (credit 0) and start heartbeats.
    const startActive = () => {
      if (activeRef.current) return; // already timing
      activeRef.current = true;
      send("resume");
      timerRef.current = window.setInterval(() => {
        if (isVisible()) send("heartbeat");
      }, HEARTBEAT_MS);
    };

    // Leave an active period: stop heartbeats and bank the final partial interval.
    // Guarded by activeRef so a duplicate leave (hidden -> pagehide -> unmount)
    // never sends a second pause; the server is idempotent regardless.
    const pauseActive = () => {
      if (!activeRef.current) return;
      activeRef.current = false;
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
      send("pause"); // NOT visibility-guarded: this fires exactly as we go inactive
    };

    const onVisibility = () => {
      if (isVisible()) startActive();
      else pauseActive();
    };
    const onPageHide = () => pauseActive();

    if (isVisible()) startActive();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", onPageHide);

    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", onPageHide);
      pauseActive(); // unmount / navigation / gating -> final best-effort pause
    };
  }, [assessmentId, visitId, source]);
}
