import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { usePatientCase } from "../services/cases";
import { caseHubPath } from "../services/authRouting";
import { useAuth } from "../state/AuthContext";
import { ApiError } from "../services/api";
import { joinQueue, leaveQueue, queueStatus, type QueueState } from "../services/queueApi";
import { AppImage } from "../components/common/AppImage";
import { Spinner } from "../portal/ui";
import styles from "./InterviewQueuePage.module.css";

const POLL_MS = 3000;
const STEPS = ["Request Received", "In Queue", "Waiting", "Up Next", "Interview Starting"];

function stepIndex(state: QueueState | null): number {
  if (!state) return 1;
  if (state.admitted || state.state === "admitted") return 4;
  const pos = state.position ?? 99;
  if (pos <= 1) return 3;
  if (pos <= 3) return 2;
  return 1;
}

export function InterviewQueuePage() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { token, user } = useAuth();
  const { patientCase } = usePatientCase(caseId);
  const casesHome = caseHubPath(user?.role);

  const initialEntry = (location.state as { entryId?: string } | null)?.entryId ?? null;
  const entryRef = useRef<string | null>(initialEntry);
  const [state, setState] = useState<QueueState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [leaving, setLeaving] = useState(false);
  const admittedRef = useRef(false);          // transition into the interview exactly once
  const pollRef = useRef<number | null>(null);

  const goToInterview = useCallback(() => {
    if (admittedRef.current) return;
    admittedRef.current = true;
    if (pollRef.current !== null) window.clearInterval(pollRef.current);
    navigate(`/interview/${caseId}`, { replace: true });
  }, [caseId, navigate]);

  // One poll tick: read real queue status; admit → interview; expired → surfaced.
  const poll = useCallback(async () => {
    if (!caseId || admittedRef.current) return;
    try {
      let s: QueueState;
      if (!entryRef.current) {
        // No entry yet (e.g. page refresh): (re)join — idempotent per student, so
        // this never creates a duplicate entry.
        s = await joinQueue(token, caseId);
        entryRef.current = s.entry_id;
      } else {
        s = await queueStatus(token, entryRef.current);
      }
      setState(s);
      setError(null);
      if (s.admitted || s.state === "admitted") {
        goToInterview();
      } else if (s.state === "expired") {
        entryRef.current = null; // allow a fresh rejoin from the retry button
      }
    } catch (e) {
      // Transient issue — keep the last known position and retry next tick.
      setError(e instanceof ApiError ? e.message : "Temporarily unable to reach the queue. Retrying…");
    }
  }, [caseId, token, goToInterview]);

  useEffect(() => {
    void poll();
    pollRef.current = window.setInterval(() => void poll(), POLL_MS);
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId]);

  // Leaving stops polling and removes ONLY the waiting entry (backend never
  // touches the session/assessment/grading), then returns to Cases.
  const leaveAndReturn = useCallback(async () => {
    if (leaving) return;
    setLeaving(true);
    admittedRef.current = true; // stop any admit race
    if (pollRef.current !== null) window.clearInterval(pollRef.current);
    try {
      if (entryRef.current) await leaveQueue(token, entryRef.current);
    } catch {
      /* best-effort: the backend entry also self-expires via TTL */
    } finally {
      entryRef.current = null;
      navigate(casesHome, { replace: true });
    }
  }, [leaving, token, navigate, casesHome]);

  const admitted = state?.admitted || state?.state === "admitted";
  const expired = state?.state === "expired";
  const idx = stepIndex(state);
  const position = state?.position ?? null;

  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <div className={styles.main}>
          <div className={styles.statusHead}>
            <span className={styles.badge} aria-hidden="true"><span className={styles.badgeDot} /></span>
            <h1 className={styles.title}>
              {admitted ? "Your interview is ready" : "Your interview is almost ready"}
            </h1>
          </div>
          <p className={styles.subtitle}>
            {admitted ? "A slot just opened — starting your interview…" : "All interview slots are currently in use."}
          </p>

          {/* aria-live so screen readers hear position changes */}
          <div className={styles.positionBox} role="status" aria-live="polite">
            {expired ? (
              <span className={styles.expired}>Your place in the queue expired.</span>
            ) : admitted ? (
              <span className={styles.ready}><Spinner label="Starting your interview…" /></span>
            ) : position ? (
              <>You are <strong>#{position}</strong> in line</>
            ) : (
              <Spinner label="Checking the queue…" />
            )}
          </div>

          {/* Progress visualization */}
          <ol className={styles.progress} aria-label="Queue progress">
            {STEPS.map((label, i) => {
              const done = i < idx;
              const active = i === idx;
              return (
                <li key={label} className={`${styles.step} ${done ? styles.stepDone : ""} ${active ? styles.stepActive : ""}`}>
                  <span className={styles.stepMark} aria-hidden="true">{done ? "✓" : active ? "●" : i + 1}</span>
                  <span className={styles.stepLabel}>{label}</span>
                </li>
              );
            })}
          </ol>

          {!admitted && !expired && state?.estimated_wait_minutes != null && (
            <p className={styles.estimate}>Estimated wait: ~{state.estimated_wait_minutes} minute{state.estimated_wait_minutes === 1 ? "" : "s"}</p>
          )}

          <p className={styles.keepOpen}>
            Keep this page open. Your interview will begin automatically when a slot becomes available.
          </p>

          {error && <p className={styles.error} role="alert">{error}</p>}

          <div className={styles.actions}>
            <button type="button" className="btn btn-secondary" onClick={() => void leaveAndReturn()} disabled={leaving}>
              {leaving ? "Leaving…" : "Leave Queue"}
            </button>
            <button type="button" className="btn btn-secondary" onClick={() => void leaveAndReturn()} disabled={leaving}>
              Return to Cases
            </button>
          </div>
          {expired && (
            <button type="button" className="btn btn-primary" style={{ marginTop: 12 }} onClick={() => { admittedRef.current = false; void poll(); }}>
              Rejoin Queue
            </button>
          )}
        </div>

        {/* Selected case card (real case data) */}
        <aside className={styles.sideCard} aria-label="Selected assessment">
          <div className={styles.sideLabel}>Selected Assessment</div>
          {patientCase ? (
            <>
              <AppImage src={patientCase.image} alt={`${patientCase.name} patient portrait`} className={styles.patientImg} />
              <div className={styles.patientName}>{patientCase.name}</div>
              <div className={styles.patientMeta}>Age {patientCase.age}</div>
              {patientCase.caseCategory === "referral" && <div className={styles.assessType}>Referral &amp; Interprofessional</div>}
            </>
          ) : (
            <Spinner label="Loading case…" />
          )}
          <p className={styles.autoNote}>The interview will start automatically once a slot is free.</p>
        </aside>
      </div>
    </div>
  );
}
