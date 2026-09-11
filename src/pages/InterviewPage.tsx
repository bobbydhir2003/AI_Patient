import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ApiError,
  completeSession,
  createAssessment,
  createSession,
  fetchSession,
  fetchSessionTurns,
} from "../services/api";
import { classifyInterviewInitError } from "../services/interviewErrors";
import { unlockAudioPlayback } from "../services/audioUnlock";
import { useLiveKitInterviewVoice } from "../hooks/useLiveKitInterviewVoice";
import {
  mapSessionMessages,
  reconcileLiveKitPatientMessage,
  reconcileLiveKitStudentMessage,
} from "../services/livekit/liveKitTranscriptMessages";
import { AppImage } from "../components/common/AppImage";
import { isUsableTranscript, type VoiceConversationState } from "../hooks/voiceStateMachine";
import { usePatientCase } from "../services/cases";
import { caseHubPath } from "../services/authRouting";
import { useAppContext } from "../state/AppContext";
import { useAuth } from "../state/AuthContext";
import { ProgressSteps } from "../components/layout/ProgressSteps";
import { ConversationPanel } from "../components/interview/ConversationPanel";
import { ConversationControl } from "../components/interview/ConversationControl";
import { InterviewWelcomeCard } from "../components/interview/InterviewWelcomeCard";
import { InterviewTimer } from "../components/interview/InterviewTimer";
import { ConfirmationModal } from "../components/interview/ConfirmationModal";
import type { ConnectionState } from "../types/interview";
import styles from "./InterviewPage.module.css";

const PROGRESS_STEPS = ["Case Introduction", "Interview", "Complete"];

const CONNECTION_LABELS: Record<ConnectionState, string> = {
  connecting: "Connecting",
  connected: "Connected",
  offline: "Offline",
  error: "Error",
};

function StatusIcon({ type }: { type: "connection" | "session" | "time" }) {
  if (type === "connection") {
    return (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M12 19a7 7 0 1 0-7-7" />
        <path d="M12 5a7 7 0 0 1 7 7" />
        <path d="M8 17l-3 3-2-2" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="8" />
      <path d="M12 8v4l2.5 2.5" />
    </svg>
  );
}

/** Map voice states to the sidebar badge (text + existing badge styles). */
function badgeFor(state: VoiceConversationState, typedBusy: boolean): { label: string; css: string } {
  if (typedBusy) return { label: "Processing", css: "processing" };
  switch (state) {
    case "LISTENING":
      return { label: "Listening", css: "listening" };
    case "REQUESTING_PERMISSION":
      // In the LiveKit + OpenAI Realtime path (the only voice architecture),
      // this state covers the whole "connecting"/"waiting_for_agent" setup
      // window (token fetch, room join, worker dispatch, OpenAI session setup)
      // - mic acquisition happens internally, not via a browser permission
      // prompt. "Connecting" is what the student is actually waiting on, so it
      // is shown instead of the misleading "Mic access".
      return { label: "Connecting", css: "processing" };
    case "PROCESSING":
      return { label: "Processing", css: "processing" };
    case "SPEAKING":
      return { label: "Patient Speaking", css: "speaking" };
    case "INTERRUPTING":
      return { label: "Interrupting", css: "listening" };
    case "COOLDOWN":
      return { label: "One moment", css: "cooldown" };
    case "ERROR":
      return { label: "Voice error", css: "error" };
    case "PAUSED":
      return { label: "Paused", css: "idle" };
    case "FINISHED":
      return { label: "Finished", css: "finished" };
    default:
      return { label: "Ready", css: "idle" };
  }
}

function formatTimestamp(): string {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function InterviewPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { patientCase, loading: caseLoading, error: caseError, retry: retryCase } =
    usePatientCase(caseId);
  const studentHome = caseHubPath(user?.role);

  const {
    studentName,
    studentId,
    activeInterview,
    setActiveInterview,
    clearInterview,
    messages,
    setMessages,
  } = useAppContext();

  const [connection, setConnection] = useState<ConnectionState>("connecting");
  // Specific message for non-connectivity init failures (403/401/5xx). When set
  // with connection === "error", it is shown instead of the "offline" banner.
  const [initError, setInitError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [banner, setBanner] = useState<string | null>(null);
  const [showEndModal, setShowEndModal] = useState(false);
  const [connectAttempt, setConnectAttempt] = useState(0);
  const [typedBusy, setTypedBusy] = useState(false);
  // End-interview pipeline: flush saves → verify transcript → complete+lock →
  // generate AI assessment → navigate. Each stage is user-visible.
  const [endPhase, setEndPhase] = useState<
    "flushing" | "completing" | "generating" | null
  >(null);
  // Guard against duplicate session creation (React StrictMode double-mount).
  const initKeyRef = useRef<string | null>(null);

  const sessionReady =
    connection === "connected" &&
    activeInterview !== null &&
    activeInterview.caseId === caseId;

  // ------------------------------------------------------------------
  // The patient conversation runs EXCLUSIVELY through LiveKit + OpenAI
  // Realtime (see the voice hook below). Typed input during an interview is
  // injected into that same Realtime session via voice.submitExternal(); there
  // is no separate HTTP patient-generation path anymore.
  // ------------------------------------------------------------------

  // ------------------------------------------------------------------
  // Voice conversation: LiveKit + OpenAI Realtime prompt_agent, the only
  // interview voice architecture. Patient audio comes ONLY from the LiveKit
  // RemoteAudioTrack (see useLiveKitInterviewVoice.ts / livekitPocEngine.ts).
  // Patient TEXT is never invented client-side: onTurnCompleted re-fetches
  // the session's authoritative transcript (the SAME rows the agent already
  // persisted server-side), reusing the exact mapSessionMessages() helper
  // the "resume an in-progress session" path above already uses.
  // ------------------------------------------------------------------
  const voice = useLiveKitInterviewVoice({
    sessionId: activeInterview?.sessionId ?? null,
    enabled: sessionReady,
    onInterim: setDraft,
    onTurnCompleted: () => {
      if (!activeInterview) return;
      void fetchSession(activeInterview.sessionId)
        .then((session) => setMessages(mapSessionMessages(session)))
        .catch((err) => {
          if (import.meta.env.DEV) console.error("Could not refresh transcript after LiveKit turn:", err);
        });
    },
    // P0-1: render Carly's approved text the instant it's ready (before speech
    // completes) and reconcile it on final - keyed by patientTurnId (the DB
    // ConversationTurn id) so the later authoritative DB refetch REPLACES this
    // same message rather than appending a duplicate.
    onPatientText: (text, meta) => {
      setMessages((prev) =>
        reconcileLiveKitPatientMessage(prev, text, meta, formatTimestamp()),
      );
    },
    // prompt_agent mode: OpenAI Realtime owns the conversation, so FINAL student
    // turns arrive as transcript_sync events (keyed by the DB ConversationTurn
    // id) rather than from the browser recognizer. Insert them the same way as
    // patient text so the authoritative refetch reconciles without duplicating.
    onStudentText: (text, meta) => {
      setMessages((prev) =>
        reconcileLiveKitStudentMessage(prev, text, meta, formatTimestamp()),
      );
    },
  });

  const voiceRef = useRef(voice);
  voiceRef.current = voice;

  // ------------------------------------------------------------------
  // Initialize (and re-initialize) the interview whenever the case changes.
  // Voice state is fully reset - no voice state is reused between patients.
  // ------------------------------------------------------------------
  useEffect(() => {
    if (!caseId) return;
    let cancelled = false;

    voiceRef.current.reset();
    setMessages([]);
    setDraft("");
    setBanner(null);
    setTypedBusy(false);
    setConnection("connecting");

    async function initializeInterview(routeCaseId: string) {
      // Yield one tick: React StrictMode mounts, unmounts, and remounts in
      // dev; the first run is cancelled before it can POST a duplicate session.
      await new Promise((resolve) => window.setTimeout(resolve, 0));
      if (cancelled) return;
      initKeyRef.current = `${routeCaseId}:${connectAttempt}`;

      const existing =
        activeInterview && activeInterview.caseId === routeCaseId ? activeInterview : null;
      if (existing) {
        try {
          const session = await fetchSession(existing.sessionId);
          if (cancelled) return;
          if (session.caseId === routeCaseId && !session.locked) {
            // The backend transcript is the source of truth: restored turns
            // are saved by definition.
            setMessages(mapSessionMessages(session));
            setConnection("connected");
            return;
          }
        } catch (err) {
          if (import.meta.env.DEV) console.error("Could not resume session:", err);
        }
      }

      try {
        const session = await createSession(
          studentName.trim() || "Student",
          studentId.trim(),
          routeCaseId,
        );
        if (cancelled) return;
        setActiveInterview({
          caseId: routeCaseId,
          sessionId: session.sessionId,
          startedAt: Date.now(),
        });
        setConnection("connected");
      } catch (err) {
        if (cancelled) return;
        console.error("Backend session could not be created:", err);
        setActiveInterview(null);
        // Only genuine network failures are "offline"; 403/401/5xx are not.
        const result = classifyInterviewInitError(err);
        setInitError(result.offline ? null : result.message);
        setConnection(result.connection);
      }
    }

    void initializeInterview(caseId);
    return () => {
      cancelled = true;
      voiceRef.current.reset();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId, connectAttempt]);

  // ------------------------------------------------------------------
  // Typed input during an interview. There is a SINGLE patient conversation
  // engine now (LiveKit + OpenAI Realtime): a typed question is injected into
  // the SAME Realtime session via voice.submitExternal() and, mid-speech, acts
  // as an interruption. If the LiveKit voice session is not active/connected,
  // typed input cannot be delivered - we show a clear message instead of
  // silently falling back to any legacy HTTP patient engine.
  // ------------------------------------------------------------------
  async function handleTypedSend() {
    const text = draft.trim();
    if (!text || typedBusy) return;
    // Typed Send is a user gesture: unlock audio so the patient's spoken reply
    // is allowed to play on iOS Safari.
    unlockAudioPlayback();

    if (voice.active) {
      // The Realtime session owns the loop: typed question = interruption +
      // normal turn, delivered over the LiveKit data channel.
      setDraft("");
      voice.submitExternal(text);
      return;
    }

    // No active voice session: the only patient engine is LiveKit/Realtime, so
    // there is nowhere to deliver a typed turn. Ask the student to connect.
    setBanner(
      "Start the voice conversation to talk to the patient. Type-only chat is no longer available - the patient is powered by the live voice session.",
    );
  }

  async function handleConfirmEnd() {
    if (endPhase) return; // prevent duplicate clicks while a request runs
    setShowEndModal(false);
    voice.reset();
    if (!activeInterview) {
      clearInterview();
      navigate("/interview/complete");
      return;
    }
    const completedSessionId = activeInterview.sessionId;
    try {
      // 1) Verify the SAVED backend transcript (the assessment's only source).
      //    Voice turns are persisted server-side by the LiveKit worker as they
      //    complete, so there is no client-side exchange to flush first.
      setEndPhase("flushing");
      const savedTurns = await fetchSessionTurns(completedSessionId);
      const hasUsableTranscript =
        savedTurns.some((t) => t.speaker === "student" && t.content.trim()) &&
        savedTurns.some((t) => t.speaker === "patient" && t.content.trim());
      if (!hasUsableTranscript) {
        setEndPhase(null);
        setBanner(
          "This interview has no saved conversation yet. Ask the patient at least one question before ending the interview.",
        );
        return;
      }
      if (import.meta.env.DEV) {
        console.info("end_interview transcript verified", {
          sessionId: completedSessionId,
          backendTurnCount: savedTurns.length,
        });
      }

      // 3) Complete and lock the interview (backend re-validates row counts).
      setEndPhase("completing");
      await completeSession(completedSessionId);

      // 4) Start assessment generation IMMEDIATELY, in the background. This is
      //    the same fire-and-forget kickoff the loading screen used to make; the
      //    backend enqueues it and a worker runs it independently. We do it here
      //    so grading is already underway while the student fills the Post-Survey.
      //    createAssessment is idempotent, so the loading page re-triggering it
      //    later (if the student lands there) is harmless.
      createAssessment(completedSessionId).catch((err) => {
        if (import.meta.env.DEV) console.error("assessment kickoff failed:", err);
      });

      // 5) Navigate to the Post-Interview Survey (NOT the assessment loading
      //    screen). The survey routes onward to the assessment on submit.
      clearInterview();
      navigate(`/survey/${completedSessionId}/post`, { replace: true });
    } catch (err) {
      console.error("End-interview pipeline failed:", err);
      setEndPhase(null);
      if (err instanceof ApiError && err.code === "transcript_empty") {
        setBanner(err.message);
        return;
      }
      setBanner(
        "The interview could not be completed. Your conversation remains open and saved - please try again.",
      );
    }
  }

  // --------------------------- Render guards ---------------------------
  if (caseLoading && !patientCase) {
    return (
      <div className="page">
        <p role="status">Loading patient case...</p>
      </div>
    );
  }

  if (caseError && !patientCase) {
    return (
      <div className="page">
        <div className={`card ${styles.connectCard}`}>
          <p>{caseError}</p>
          <button type="button" className="btn btn-primary" onClick={retryCase}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!patientCase) {
    return (
      <div className="page">
        <p>We couldn't find that patient case.</p>
        <button type="button" className="btn btn-primary" onClick={() => navigate(studentHome)}>
          Back to Case Selection
        </button>
      </div>
    );
  }

  const badge = badgeFor(voice.state, typedBusy);
  const badgeClass = styles[badge.css] ?? "";
  const connectionBadgeClass = styles[`conn-${connection}`] ?? "";
  const patientResponding = typedBusy || voice.state === "PROCESSING";
  const sendUsable = isUsableTranscript(draft);

  return (
    <div className={`${styles.page} page`}>
      <ProgressSteps steps={PROGRESS_STEPS} currentStepIndex={1} />

      {connection === "offline" && (
        <div className={`card ${styles.offlineBanner}`} role="alert">
          <div>
            <strong>Not connected to the interview backend.</strong>
            <p className={styles.offlineText}>
              A live session is required - no simulated replies are shown without it. Start the
              backend, then retry.
            </p>
          </div>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setConnectAttempt((n) => n + 1)}
          >
            Retry connection
          </button>
        </div>
      )}

      {connection === "error" && initError && (
        <div className={`card ${styles.offlineBanner}`} role="alert">
          <div>
            <strong>{initError}</strong>
          </div>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              setInitError(null);
              setConnectAttempt((n) => n + 1);
            }}
          >
            Retry
          </button>
        </div>
      )}

      {banner && (
        <div className={`card ${styles.errorBanner}`} role="alert">
          <span>{banner}</span>
          <button type="button" className="btn btn-ghost" onClick={() => setBanner(null)}>
            Dismiss
          </button>
        </div>
      )}

      {endPhase && (
        <div className={`card ${styles.errorBanner}`} role="status" aria-live="polite">
          <span>
            {endPhase === "flushing"
              ? "Saving conversation..."
              : endPhase === "completing"
                ? "Completing interview..."
                : "Generating your AI assessment — this can take a minute. Please stay on this page."}
          </span>
        </div>
      )}

      {/* Compact patient header — mobile only (CSS hides it on desktop). Keeps
          the patient card from taking half the phone screen. */}
      <div className={`card ${styles.mobilePatientHeader}`}>
        <AppImage
          src={patientCase.image}
          alt={`${patientCase.name} patient portrait`}
          className={styles.mobilePatientImg}
        />
        <div className={styles.mobilePatientMeta}>
          <span className={styles.mobilePatientName}>{patientCase.name}</span>
          <span className={styles.mobilePatientAge}>Age {patientCase.age}</span>
          <span className={styles.mobilePatientStatus}>
            <InterviewTimer startTime={sessionReady ? activeInterview.startedAt : null} />
            {" · "}
            <span className={`${styles.statusBadge} ${badgeClass}`}>
              <span className={styles.statusDot} />
              {badge.label}
            </span>
          </span>
        </div>
      </div>

      <div className={styles.layout}>
        <aside className={styles.sidebar}>
          <div className={styles.patientPanel}>
            <div className={styles.patientHero}>
              <AppImage
                src={patientCase.image}
                alt={`${patientCase.name} patient portrait`}
                className={styles.patientImage}
              />
              <div className={styles.patientIdentity}>
                <h2 className={styles.patientName}>{patientCase.name}</h2>
                <p className={styles.patientAge}>Age: {patientCase.age}</p>
              </div>
            </div>

            <div className={styles.statusCard}>
              <div className={styles.statusPanelRow}>
                <span className={styles.statusInfoLabel}>
                  <StatusIcon type="connection" />
                  Connection
                </span>
                <span className={`${styles.statusBadge} ${connectionBadgeClass}`}>
                  <span className={styles.statusDot} />
                  {CONNECTION_LABELS[connection]}
                </span>
              </div>
              <div className={styles.statusPanelRow}>
                <span className={styles.statusInfoLabel}>
                  <StatusIcon type="session" />
                  Session Status
                </span>
                <span className={`${styles.statusBadge} ${badgeClass}`}>
                  <span className={styles.statusDot} />
                  {badge.label}
                </span>
              </div>
              <div className={styles.statusPanelRow}>
                <span className={styles.statusInfoLabel}>
                  <StatusIcon type="time" />
                  Time Elapsed
                </span>
                <span className={styles.statusValue}>
                  <InterviewTimer startTime={sessionReady ? activeInterview.startedAt : null} />
                </span>
              </div>
            </div>
          </div>

          {/* Auto-interrupt/sensitivity/audio-output and the "Speak patient
              replies" toggle were legacy browser-VAD / typed-chat-TTS concepts.
              The patient is now voiced entirely by OpenAI Realtime over the
              LiveKit audio track (barge-in is server-side), so those controls
              no longer apply and have been removed. */}

          <button
            type="button"
            className={`btn btn-secondary ${styles.endButton}`}
            onClick={() => setShowEndModal(true)}
            disabled={endPhase !== null}
          >
            {endPhase === "flushing"
              ? "Saving conversation..."
              : endPhase === "completing"
                ? "Completing interview..."
                : endPhase === "generating"
                  ? "Generating assessment..."
                  : "End Interview"}
          </button>
        </aside>

        <section className={styles.mainPanel}>
          <div className={styles.mainHeader}>
            <div className={styles.mainHeaderText}>
              <h1 className={styles.mainTitle}>
                Interview with {patientCase.name}
                {patientCase.caseCategory === "referral" && (
                  <span className={styles.advancedChip}>Advanced Case</span>
                )}
              </h1>
              <p className={styles.mainSubtitle}>
                Ask questions to gather information about {patientCase.name}'s condition.
              </p>
            </div>
            <span
              className={`${styles.headerPill} ${badgeClass}`}
              role="status"
              aria-live="polite"
            >
              <span className={styles.statusDot} />
              {badge.label}
            </span>
          </div>

          <ConversationPanel
            messages={messages}
            isPatientResponding={patientResponding}
            draft={draft}
            onDraftChange={setDraft}
            onSend={() => void handleTypedSend()}
            inputDisabled={!sessionReady || typedBusy || endPhase !== null || voice.state === "PROCESSING"}
            sendDisabled={
              !sessionReady ||
              typedBusy ||
              endPhase !== null ||
              !sendUsable ||
              voice.state === "PROCESSING" ||
              voice.state === "INTERRUPTING" ||
              voice.state === "REQUESTING_PERMISSION"
            }
            patientName={patientCase.name}
            welcome={<InterviewWelcomeCard patientName={patientCase.name} />}
            mic={{
              supported: voice.supported,
              active: voice.active,
              // Same voice hook as the big button: toggle start/stop. Disabled
              // only while the backend isn't ready, the interview is ending, or
              // a request/permission is mid-flight (avoids double triggers).
              disabled:
                !sessionReady ||
                endPhase !== null ||
                voice.state === "PROCESSING" ||
                voice.state === "REQUESTING_PERMISSION" ||
                voice.state === "INTERRUPTING",
              label: voice.active
                ? "Stop voice conversation"
                : `Start voice conversation with ${patientCase.name}`,
              onToggle: () => (voice.active ? voice.stopConversation() : voice.startConversation()),
            }}
            voiceControl={
              <ConversationControl
                patientName={patientCase.name}
                supported={voice.supported}
                enabled={sessionReady}
                hasConversation={messages.length > 0}
                state={voice.state}
                errorMessage={voice.errorMessage}
                onStart={voice.startConversation}
                onStop={voice.stopConversation}
                onInterrupt={voice.interruptPatient}
                onRetry={voice.retry}
                // Preserves existing behavior: retry has always been disabled
                // for the LiveKit engine (now the only engine).
                retryDisabled={true}
              />
            }
          />
          <div className={styles.statusFooter}>
            <span className={styles.footerItem}>
              <span className={`${styles.statusDot} ${connectionBadgeClass}`} />
              Backend: {CONNECTION_LABELS[connection]}
            </span>
            {import.meta.env.DEV && sessionReady && (
              <span className={styles.footerItem}>Response source: OpenAI backend (dev only)</span>
            )}
            <span className={styles.footerItem}>Your session is secure and private</span>
          </div>
        </section>
      </div>

      {showEndModal && (
        <ConfirmationModal
          title="End Interview?"
          message="Are you sure you want to end the interview? The transcript will be saved and the session locked."
          confirmLabel="End Interview"
          cancelLabel="Continue Interview"
          onConfirm={() => void handleConfirmEnd()}
          onCancel={() => setShowEndModal(false)}
        />
      )}
    </div>
  );
}
