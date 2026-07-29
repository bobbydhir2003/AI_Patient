import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ApiError,
  completeSession,
  createSession,
  fetchInterviewConfig,
  fetchSession,
  fetchSessionTurns,
  sendStudentMessage,
} from "../services/api";
import {
  startStreamingExchange,
  StreamCancelledError,
  StreamStartFailedError,
} from "../services/patientStreamService";
import { isTtsSupported } from "../services/textToSpeechService";
import {
  cancelPatientSpeech,
  speakPatientResponse,
} from "../services/patientVoiceService";
import type { AudioSetup, InterruptionSensitivity } from "../services/voiceActivityDetector";
import { useVoiceConversation } from "../hooks/useVoiceConversation";
import { isUsableTranscript, type VoiceConversationState } from "../hooks/voiceStateMachine";
import { usePatientCase } from "../services/cases";
import { useAppContext } from "../state/AppContext";
import { ProgressSteps } from "../components/layout/ProgressSteps";
import { PatientProfile } from "../components/cases/PatientProfile";
import { ConversationPanel } from "../components/interview/ConversationPanel";
import { ConversationControl } from "../components/interview/ConversationControl";
import { InterviewTimer } from "../components/interview/InterviewTimer";
import { ConfirmationModal } from "../components/interview/ConfirmationModal";
import type {
  ConnectionState,
  ConversationMessage,
  PatientExchange,
} from "../types/interview";
import styles from "./InterviewPage.module.css";

const PROGRESS_STEPS = ["Case Introduction", "Interview", "Complete"];

const CONNECTION_LABELS: Record<ConnectionState, string> = {
  connecting: "Connecting",
  connected: "Connected",
  offline: "Offline",
  error: "Error",
};

/** Map voice states to the sidebar badge (text + existing badge styles). */
function badgeFor(state: VoiceConversationState, typedBusy: boolean): { label: string; css: string } {
  if (typedBusy) return { label: "Processing", css: "processing" };
  switch (state) {
    case "LISTENING":
      return { label: "Listening", css: "listening" };
    case "REQUESTING_PERMISSION":
      return { label: "Mic access", css: "processing" };
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

function localId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export function InterviewPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const { patientCase, loading: caseLoading, error: caseError, retry: retryCase } =
    usePatientCase(caseId);

  const {
    studentName,
    studentId,
    activeInterview,
    setActiveInterview,
    clearInterview,
    messages,
    setMessages,
    addMessage,
  } = useAppContext();

  const [connection, setConnection] = useState<ConnectionState>("connecting");
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
  // The exchange endpoint saves both turns atomically; at most one exchange is
  // in flight. End Interview awaits it so no pending save is ever lost.
  const inFlightExchangeRef = useRef<Promise<unknown> | null>(null);
  // Guard against duplicate session creation (React StrictMode double-mount).
  const initKeyRef = useRef<string | null>(null);
  // Barge-in defaults chosen for laptop speakers: automatic interruption OFF
  // (manual Interrupt button always available). Enable auto with headphones.
  const [autoInterrupt, setAutoInterrupt] = useState(false);
  const [audioSetup, setAudioSetup] = useState<AudioSetup>("speakers");
  const [sensitivity, setSensitivity] = useState<InterruptionSensitivity>("medium");

  const ttsAvailable = isTtsSupported();
  const [voiceEnabled, setVoiceEnabled] = useState(ttsAvailable);

  const sessionReady =
    connection === "connected" &&
    activeInterview !== null &&
    activeInterview.caseId === caseId;

  // ------------------------------------------------------------------
  // The ONLY path that produces patient text: the real backend/OpenAI flow.
  // Shared by typed chat and voice mode. Appends both transcript messages
  // and returns the patientText; throws on any failure (question preserved).
  //
  // When the backend enables streaming (OPENAI_PATIENT_STREAMING_ENABLED),
  // performExchange routes through the low-latency SSE pipeline first and
  // falls back to this stable atomic path automatically whenever the stream
  // fails BEFORE any sentence was spoken (same clientTurnId => idempotent,
  // no duplicate turns, no regeneration on replay).
  // ------------------------------------------------------------------
  async function performExchange(question: string, source: "typed" | "speech" = "typed"): Promise<PatientExchange> {
    if (!caseId || !activeInterview || activeInterview.caseId !== caseId) {
      setBanner("This session does not belong to the selected patient. Reconnecting...");
      clearInterview();
      setConnectAttempt((n) => n + 1);
      throw new Error("Session/case mismatch.");
    }
    setBanner(null);
    // One client id per submitted question: retries replay the SAME exchange
    // on the backend instead of creating duplicate rows or regenerating.
    const clientTurnId =
      typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : localId();

    const config = await fetchInterviewConfig();
    if (config.streamingEnabled) {
      try {
        return await performStreamingTurn(question, source, clientTurnId);
      } catch (err) {
        if (err instanceof StreamCancelledError) {
          throw new Error("The patient response was interrupted.");
        }
        if (err instanceof StreamStartFailedError) {
          // Nothing was spoken or saved: the stable path below retries the
          // SAME exchange safely (idempotent clientTurnId).
          if (import.meta.env.DEV) {
            console.debug("[patient-stream] falling back to stable path:", err.code);
          }
        } else {
          throw err;
        }
      }
    }
    return performAtomicExchange(question, source, clientTurnId);
  }

  /** Streamed exchange: transcript grows sentence-by-sentence; audio (when
   * enabled) starts on the first approved sentence. Resolves at the first
   * sentence so the voice loop enters SPEAKING immediately. */
  async function performStreamingTurn(
    question: string,
    source: "typed" | "speech",
    clientTurnId: string,
  ): Promise<PatientExchange> {
    const studentMsgId = localId();
    const patientMsgId = localId();
    let messagesAdded = false;

    const handle = startStreamingExchange({
      sessionId: activeInterview!.sessionId,
      caseId: caseId!,
      text: question,
      clientTurnId,
      source,
      speakAloud: voiceEnabled && ttsAvailable,
      onSentence: (_index, text) => {
        if (!messagesAdded) {
          messagesAdded = true;
          addMessage({
            id: studentMsgId, sender: "student", text: question,
            timestamp: formatTimestamp(), clientTurnId, source, saveStatus: "pending",
          });
          addMessage({
            id: patientMsgId, sender: "patient", text,
            timestamp: formatTimestamp(), clientTurnId: `${clientTurnId}:patient`,
            source: "openai", saveStatus: "pending",
          });
        } else {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === patientMsgId ? { ...m, text: `${m.text} ${text}`.trim() } : m,
            ),
          );
        }
      },
      onFinal: (final) => {
        // ONE authoritative patient turn: the final text replaces the
        // accumulated sentences (they are identical on normal completion).
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id === studentMsgId) return { ...m, saveStatus: "saved" as const };
            if (m.id === patientMsgId) {
              return {
                ...m,
                text: final.patientText || m.text,
                saveStatus: "saved" as const,
              };
            }
            return m;
          }),
        );
        if (import.meta.env.DEV) {
          console.info("Response source: OpenAI backend (streamed)", {
            turnId: final.turnId, status: final.status,
          });
        }
      },
    });

    // End Interview flushes this so a pending streamed save is never lost.
    inFlightExchangeRef.current = handle.completion;
    void handle.completion.finally(() => {
      if (inFlightExchangeRef.current === handle.completion) {
        inFlightExchangeRef.current = null;
      }
    });

    try {
      const first = await handle.firstSentence;
      return { patientText: first.text, turnId: "", speech: first.speech, streaming: handle };
    } catch (err) {
      if (err instanceof StreamStartFailedError || err instanceof StreamCancelledError) throw err;
      throw new StreamStartFailedError("stream_error", String(err));
    }
  }

  /** Original stable atomic exchange (unchanged behavior; always available). */
  async function performAtomicExchange(
    question: string,
    source: "typed" | "speech",
    clientTurnId: string,
  ): Promise<PatientExchange> {
    try {
      const exchange = sendStudentMessage(
        activeInterview!.sessionId, question, caseId!, clientTurnId, source,
      );
      inFlightExchangeRef.current = exchange;
      const turn = await exchange;
      addMessage({
        id: localId(), sender: "student", text: question, timestamp: formatTimestamp(),
        clientTurnId, source, saveStatus: "saved",
      });
      // Multi-participant: render one bubble per ordered segment (a joint
      // "both" turn shows Camden then his mother). Single-speaker cases have one.
      const segments = turn.responses && turn.responses.length > 0
        ? turn.responses
        : [{ turnId: turn.turnId, speakerId: turn.speakerId ?? "patient",
             speakerLabel: turn.speakerLabel ?? "", text: turn.patientText, speech: turn.speech ?? null }];
      for (const seg of segments) {
        addMessage({
          id: seg.turnId, sender: "patient", text: seg.text, timestamp: formatTimestamp(),
          clientTurnId: `${clientTurnId}:patient:${seg.speakerId}`, source: "openai",
          saveStatus: "saved", speakerId: seg.speakerId, speakerLabel: seg.speakerLabel,
        });
      }
      if (import.meta.env.DEV) {
        console.info("Response source: OpenAI backend", { turnId: turn.turnId });
      }
      // The transcript above shows EXACTLY turn.patientText; the speech labels
      // only shape TTS delivery and are never displayed or stored client-side.
      return { patientText: turn.patientText, turnId: turn.turnId, speech: turn.speech ?? null };
    } catch (err) {
      console.error("Patient response failed:", err);
      let message = "Connection interrupted. Your question was kept - check the backend and retry.";
      if (err instanceof ApiError && err.code === "case_session_mismatch") {
        clearInterview();
        setConnectAttempt((n) => n + 1);
        message = "Session/case mismatch detected. A new session will be created - please resend your question.";
      } else if (err instanceof ApiError && err.code === "PATIENT_RESPONSE_UNAVAILABLE") {
        message = "The patient response could not be generated. Your question was kept - please retry.";
      } else if (err instanceof ApiError && err.code === "session_locked") {
        message = "This interview is already completed and locked.";
      } else if (!(err instanceof ApiError)) {
        setConnection("offline");
      } else if (err.status >= 500) {
        setConnection("offline");
      }
      setBanner(message);
      setDraft(question); // keep the unsent question available
      throw new Error(message);
    } finally {
      inFlightExchangeRef.current = null;
    }
  }

  // ------------------------------------------------------------------
  // Voice conversation controller (state machine + STT + TTS + barge-in).
  // It routes every recognized question through performExchange above and
  // never generates patient text itself.
  // ------------------------------------------------------------------
  const voice = useVoiceConversation({
    patientName: patientCase?.name ?? "the patient",
    caseId: caseId ?? "",
    sessionId: activeInterview?.sessionId ?? null,
    enabled: sessionReady,
    speakReplies: voiceEnabled,
    autoInterrupt,
    audioSetup,
    sensitivity,
    onSubmitQuestion: performExchange,
    onInterim: setDraft,
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
    cancelPatientSpeech();
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
            const restored: ConversationMessage[] = session.messages.map((m) => ({
              id: m.id,
              sender: m.sender,
              text: m.text,
              speakerId: m.speakerId,
              speakerLabel: m.speakerLabel,
              saveStatus: "saved" as const,
              timestamp: new Date(m.timestamp).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
              }),
            }));
            setMessages(restored);
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
        setConnection("offline");
      }
    }

    void initializeInterview(caseId);
    return () => {
      cancelled = true;
      voiceRef.current.reset();
      cancelPatientSpeech();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId, connectAttempt]);

  // ------------------------------------------------------------------
  // Typed chat. Preserved fully; during patient speech a typed Send acts as
  // an interruption (stops the patient, then goes through the same backend).
  // ------------------------------------------------------------------
  async function handleTypedSend() {
    const text = draft.trim();
    if (!text || typedBusy) return;

    if (voice.active) {
      // Voice mode owns the loop: typed question = interruption + normal flow.
      setDraft("");
      voice.submitExternal(text);
      return;
    }

    setTypedBusy(true);
    try {
      const exchange = await performExchange(text);
      setDraft("");
      if (exchange.streaming) {
        // Streaming path: audio (if enabled) is already playing sentence by
        // sentence; stay busy until the WHOLE response audio finished (or the
        // final text arrived when voice is off).
        await exchange.streaming.playbackDone;
      } else if (voiceEnabled && ttsAvailable && caseId) {
        // Same provider path as voice mode: ElevenLabs via the backend when
        // available, browser speechSynthesis otherwise.
        await speakPatientResponse({
          caseId,
          text: exchange.patientText,
          sessionId: activeInterview?.sessionId,
          turnId: exchange.turnId,
          speechStyle: exchange.speech,
        });
      }
    } catch {
      // performExchange already set the banner and preserved the draft
    } finally {
      setTypedBusy(false);
    }
  }

  async function handleConfirmEnd() {
    if (endPhase) return; // prevent duplicate clicks while a request runs
    setShowEndModal(false);
    voice.reset();
    cancelPatientSpeech();
    if (!activeInterview) {
      clearInterview();
      navigate("/interview/complete");
      return;
    }
    const completedSessionId = activeInterview.sessionId;
    try {
      // 1) Flush: await any in-flight exchange so no turn save is lost, then
      //    verify the SAVED backend transcript (the assessment's only source).
      setEndPhase("flushing");
      if (inFlightExchangeRef.current) {
        await inFlightExchangeRef.current.catch((err: unknown) => {
          // Not silent: performExchange already surfaced this failure to the
          // student (banner + preserved draft). Ending must not double-report.
          console.error("Pending exchange failed while ending the interview:", err);
        });
      }
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

      // 4) Navigate to the assessment loading screen.
      // The loading screen is responsible for triggering the generation and polling status.
      clearInterview();
      navigate(`/assessment/${completedSessionId}/loading`, { replace: true });
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
        <button type="button" className="btn btn-primary" onClick={() => navigate("/cases")}>
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
    <div className="page">
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

      <div className={styles.layout}>
        <aside className={`card ${styles.sidebar}`}>
          <PatientProfile patientCase={patientCase} size="large" />

          <div className={styles.statusRow}>
            <div className={styles.statusItem}>
              <span className={styles.statusLabel}>Time Elapsed</span>
              <span className={styles.statusValue}>
                <InterviewTimer startTime={sessionReady ? activeInterview.startedAt : null} />
              </span>
            </div>
            <div className={styles.statusItem}>
              <span className={styles.statusLabel}>Backend</span>
              <span className={`${styles.statusBadge} ${connectionBadgeClass}`}>
                <span className={styles.statusDot} />
                {CONNECTION_LABELS[connection]}
              </span>
            </div>
            <div className={styles.statusItem}>
              <span className={styles.statusLabel}>Status</span>
              <span className={`${styles.statusBadge} ${badgeClass}`}>
                <span className={styles.statusDot} />
                {badge.label}
              </span>
            </div>
            {ttsAvailable && (
              <label className={styles.voiceToggle}>
                <input
                  type="checkbox"
                  checked={voiceEnabled}
                  onChange={(e) => {
                    if (!e.target.checked) cancelPatientSpeech();
                    setVoiceEnabled(e.target.checked);
                  }}
                />
                Speak patient replies
              </label>
            )}
            {voice.supported && (
              <>
                <label className={styles.voiceToggle}>
                  <input
                    type="checkbox"
                    checked={autoInterrupt}
                    onChange={(e) => setAutoInterrupt(e.target.checked)}
                  />
                  Automatic interruption
                </label>
                {autoInterrupt && (
                  <p className={styles.settingNote}>
                    Works best with headphones. Laptop speakers may cause false interruptions.
                  </p>
                )}
                <label className={styles.sensitivityRow}>
                  <span>Audio setup</span>
                  <select
                    className={styles.sensitivitySelect}
                    value={audioSetup}
                    onChange={(e) => setAudioSetup(e.target.value as AudioSetup)}
                  >
                    <option value="speakers">Laptop speakers</option>
                    <option value="headphones">Headphones / headset</option>
                  </select>
                </label>
                <label
                  className={`${styles.sensitivityRow} ${!autoInterrupt ? styles.settingDisabled : ""}`}
                >
                  <span>Interruption sensitivity</span>
                  <select
                    className={styles.sensitivitySelect}
                    value={sensitivity}
                    disabled={!autoInterrupt}
                    onChange={(e) => setSensitivity(e.target.value as InterruptionSensitivity)}
                  >
                    <option value="low">Low</option>
                    <option value="medium">Medium</option>
                    <option value="high">High</option>
                  </select>
                </label>
              </>
            )}
          </div>

          <button
            type="button"
            className="btn btn-secondary"
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
            <h1 className={styles.mainTitle}>
              Interview with {patientCase.name}
              {patientCase.caseCategory === "referral" && (
                <span className={styles.advancedChip}>Advanced Case</span>
              )}
            </h1>
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
            patientImage={patientCase.image}
            patientName={patientCase.name}
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
              />
            }
          />
          {import.meta.env.DEV && sessionReady && (
            <p className={styles.devSourceLabel}>Response source: OpenAI backend (dev only)</p>
          )}
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
