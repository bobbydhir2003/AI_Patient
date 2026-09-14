/**
 * Wraps the SAME LiveKitPocEngine the admin POC page uses (see
 * livekitPocEngine.ts - not a second/parallel LiveKit implementation) for
 * the REAL student InterviewPage. This is the sole voice hook InterviewPage
 * uses (state/errorMessage/supported/active/startConversation/
 * stopConversation/interruptPatient/retry/reset/submitExternal).
 *
 * What's deliberately different from the admin POC page:
 * - Token source: fetchStudentLiveKitToken (require_session_access-gated),
 *   never the admin-only fetchAdminPocLiveKitToken.
 * - No manual room-name copying, no manual case/session entry - sessionId
 *   comes from the real interview's already-created session, caseId is
 *   never touched by the engine directly (it is embedded server-side by
 *   livekit_token_service.py's dispatch metadata, from the SAME session
 *   row - see backend/app/services/livekit_token_service.py).
 * - Patient TEXT is never invented client-side: Realtime transcript_sync
 *   events surface backend-approved persisted text immediately, then
 *   onTurnCompleted causes the page to re-fetch the same authoritative DB
 *   rows. Both paths share ConversationTurn.id, so they reconcile naturally.
 *
 * NEVER calls speechSynthesis or any browser playback primitive - see
 * livekitPocEngine.ts's own docstring/tests for that guarantee; this hook
 * only adds React lifecycle around it.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  LiveKitPocEngine,
  fetchStudentLiveKitToken,
  type PocState,
  type PocStartupStage,
  type PatientTextMeta,
  type StudentTextMeta,
} from "../services/livekit/livekitPocEngine";
import { unlockAudioPlayback } from "../services/audioUnlock";
import { isConversationActive, type VoiceConversationState } from "./voiceStateMachine";

/** Voice eligibility for the LiveKit + OpenAI Realtime prompt-agent path (the
 * only interview voice architecture). This mode uses the microphone purely as
 * LiveKit/WebRTC transport and lets OpenAI Realtime own speech detection AND
 * transcription, so it does NOT require the browser SpeechRecognition API.
 * Gating on isSpeechRecognitionSupported() here wrongly blocked desktop Safari
 * and Firefox (which lack Web Speech) from a voice session they can fully run -
 * the real requirement is getUserMedia + WebRTC (RTCPeerConnection). */
function isRealtimeVoiceSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    typeof navigator.mediaDevices?.getUserMedia === "function" &&
    typeof RTCPeerConnection !== "undefined"
  );
}

/** A subset of VoiceConversationState's own string literals - structurally
 * assignable anywhere the legacy type is expected (badgeFor,
 * ConversationControl) with ZERO changes to voiceStateMachine.ts or any
 * legacy consumer. LiveKit has no COOLDOWN/PAUSED equivalent - pause/resume
 * (as distinct from Stop -> Resume, see stopConversation below) stays out of
 * scope. INTERRUPTING IS used (Phase D2: true SPEAKING-only interruption,
 * see interruptPatient below) - ConversationControl.tsx already renders it
 * correctly with zero changes needed there. */
export type LiveKitVoiceUIState = Extract<
  VoiceConversationState,
  | "IDLE"
  | "REQUESTING_PERMISSION"
  | "LISTENING"
  | "PROCESSING"
  | "SPEAKING"
  | "INTERRUPTING"
  | "STOPPING"
  | "ERROR"
  | "FINISHED"
>;

function mapPocState(state: PocState): LiveKitVoiceUIState {
  switch (state) {
    case "idle":
      return "IDLE";
    case "connecting":
    // Room connected + mic published is not yet "ready" (see
    // livekitPocEngine.ts's Phase C protocol docstring: an explicit
    // agent_ready handshake is required before LISTENING) - reusing the
    // SAME UI state as "connecting" keeps this invisible to the student
    // (no new badge/label), matching "keep student-facing messages simple".
    case "waiting_for_agent":
      return "REQUESTING_PERMISSION";
    case "listening":
      return "LISTENING";
    case "thinking":
      return "PROCESSING";
    case "speaking":
      return "SPEAKING";
    case "interrupting":
      return "INTERRUPTING";
    case "reconnecting":
      // Transient; resolves to listening/error shortly. Reusing PROCESSING
      // avoids adding a new badge/UI case for a brief, rare blip.
      return "PROCESSING";
    case "error":
      return "ERROR";
    case "ended":
      return "FINISHED";
  }
}

export interface UseLiveKitInterviewVoiceOptions {
  sessionId: string | null;
  enabled: boolean;
  /** Interim (non-final) recognized text, for display only. Called with ""
   * once a final result is sent, to clear the draft. */
  onInterim: (transcript: string) => void;
  /** Fires once per completed turn (the agent's "speaking_ended"). Carries
   * NO text - the page is expected to re-fetch the session's transcript,
   * the single source of truth, rather than trust a client-held copy. */
  onTurnCompleted: () => void;
  /** P0-1: the backend-APPROVED patient text for a Realtime turn, delivered
   * as soon as it is persisted (`final:false`, before/at speech start) and
   * reconciled on completion/interruption (`final:true`, with `reason`).
   * Keyed by `patientTurnId` (the DB ConversationTurn id) so the page can
   * render Carly's text immediately and later reconcile with the authoritative
   * DB refetch WITHOUT duplicating the message. Optional/no-op in legacy mode
   * (transcript_sync never arrives there). */
  onPatientText?: (
    text: string,
    meta: PatientTextMeta,
  ) => void;
  /** prompt_agent mode: a FINAL student transcript persisted server-side,
   * keyed by `studentTurnId` (the DB ConversationTurn id) so the page can
   * render it immediately and reconcile with the authoritative DB refetch
   * WITHOUT duplicating the message. Optional/no-op in every other mode. */
  onStudentText?: (
    text: string,
    meta: StudentTextMeta,
  ) => void;
}

/** Phase 2: forward-only startup milestone, re-exported so the page can render
 * a concrete progress label ("Preparing microphone", "Starting patient") while
 * state is REQUESTING_PERMISSION, instead of one indefinite "Connecting". */
export type { PocStartupStage };

export interface UseLiveKitInterviewVoiceResult {
  state: LiveKitVoiceUIState;
  errorMessage: string | null;
  supported: boolean;
  active: boolean;
  /** Phase 2: the latest startup progress milestone, or null once IDLE/ended.
   * Presentational only - `state` remains the behavioral source of truth. */
  startupStage: PocStartupStage | null;
  startConversation: () => void;
  /** Phase 1: awaitable. Resolves only once the LiveKit room/mic/timers have
   * fully torn down (the UI passes through STOPPING until then), so a caller
   * - most importantly End Interview - can guarantee no voice resources are
   * still live before it proceeds. Idempotent: concurrent/repeated calls
   * return the same in-flight cleanup promise. */
  stopConversation: () => Promise<void>;
  interruptPatient: () => void;
  /** Awaitable: waits for any in-flight cleanup, then starts fresh. */
  retry: () => Promise<void>;
  /** Awaitable teardown that also clears any error message. Used by End
   * Interview and case-change/unmount resets. */
  reset: () => Promise<void>;
  submitExternal: (text: string) => void;
}

export function useLiveKitInterviewVoice(
  options: UseLiveKitInterviewVoiceOptions,
): UseLiveKitInterviewVoiceResult {
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const [state, setState] = useState<LiveKitVoiceUIState>("IDLE");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [startupStage, setStartupStage] = useState<PocStartupStage | null>(null);
  const engineRef = useRef<LiveKitPocEngine | null>(null);
  /** Phase 1: the single in-flight teardown promise. While non-null, a Stop
   * (or fatal-error) cleanup is running: Start/retry must not spin up a new
   * engine until it resolves, which is what prevents a new room from
   * overlapping a still-dying one. Set by stopConversation() and the fatal-
   * error path; cleared when teardown resolves. */
  const cleanupPromiseRef = useRef<Promise<void> | null>(null);
  const supported = isRealtimeVoiceSupported();

  /**
   * Phase D1: every callback below first checks that `engine` (the specific
   * instance this closure was built for) is STILL `engineRef.current` before
   * touching any hook state. Without this, a stale async completion from a
   * PREVIOUS engine - most notably LiveKitPocEngine.end()'s own delayed
   * setState("ended") firing well after room.disconnect() resolves - could
   * silently overwrite state a NEWER action (Stop, reset, or an unmounting
   * component) already set correctly. This was the confirmed cause of Stop
   * Voice Conversation flashing "Conversation finished": stopConversation()
   * synchronously set "IDLE", but the OLD engine's in-flight end() call
   * would still land moments later and stomp it with "FINISHED" - the guard
   * below is what makes stopConversation()/reset()/retry()/unmount all safe
   * with NO changes needed to any of them individually, since they all
   * already null out engineRef.current before (or as part of) tearing down.
   * Deliberately NOT a delay/timeout-based fix - identity comparison is
   * exact and instantaneous, whichever order the async work resolves in.
   */
  const buildEngine = useCallback((): LiveKitPocEngine => {
    const engine: LiveKitPocEngine = new LiveKitPocEngine({
      onStateChange: (pocState) => {
        if (engineRef.current !== engine) return;
        const uiState = mapPocState(pocState);
        if (uiState === "ERROR") {
          // Phase 1 (fatal-error cleanup): a bare ERROR used to leave the
          // errored engine attached with its LiveKit room still connected and
          // mic still live. Detach it now and run the SAME idempotent teardown
          // Stop uses, so ERROR is a clean, resource-free state and Retry
          // starts from scratch. errorMessage was already set via onError, and
          // is deliberately preserved. engineRef is nulled first so the
          // engine's own later "ended" callback is guarded out (identity check
          // above) and can't stomp ERROR.
          engineRef.current = null;
          setState("ERROR");
          if (!cleanupPromiseRef.current) {
            cleanupPromiseRef.current = engine.end().finally(() => {
              cleanupPromiseRef.current = null;
            });
          }
          return;
        }
        setState(uiState);
      },
      onStudentTranscript: (text, isFinal) => {
        if (engineRef.current !== engine) return;
        optionsRef.current.onInterim(isFinal ? "" : text);
      },
      onError: (message) => {
        if (engineRef.current !== engine) return;
        setErrorMessage(message);
      },
      onTurnCompleted: () => {
        if (engineRef.current !== engine) return;
        optionsRef.current.onTurnCompleted();
      },
      // P0-1: forward the Realtime approved/final patient text so the page can
      // render Carly immediately (no page refresh needed).
      onPatientText: (text, meta) => {
        if (engineRef.current !== engine) return;
        optionsRef.current.onPatientText?.(text, meta);
      },
      // prompt_agent mode: forward the FINAL student transcript so the page can
      // insert it into the conversation window with a stable DB id.
      onStudentText: (text, meta) => {
        if (engineRef.current !== engine) return;
        optionsRef.current.onStudentText?.(text, meta);
      },
      // Phase 2: surface startup progress for the connecting UI. Same stale-
      // engine guard as every other callback.
      onStartupStage: (stage) => {
        if (engineRef.current !== engine) return;
        setStartupStage(stage);
      },
      // POC-only diagnostics/room-name surfacing - the real InterviewPage
      // has no admin diagnostic panel and never displays a room name.
      onDiagnostics: () => {},
      onRoomName: () => {},
    });
    return engine;
  }, []);

  const startConversation = useCallback(() => {
    if (!optionsRef.current.enabled || !optionsRef.current.sessionId) return;
    if (!supported) return;
    // Phase 1: never start while a teardown is still resolving - the new room
    // must not overlap the one being torn down. We do NOT queue the Start; the
    // student can press Start again once the button leaves STOPPING.
    if (cleanupPromiseRef.current) return;
    if (engineRef.current) return; // already starting/started
    // Phase 1 (mobile): unlock audio playback synchronously, inside this user
    // gesture and BEFORE any await, or iOS Safari blocks the patient's first
    // spoken reply. Runs on every Start (including Resume/Retry, which route
    // through here), and is a cheap no-op after the first unlock.
    unlockAudioPlayback();
    setErrorMessage(null);
    setStartupStage(null);
    const engine = buildEngine();
    engineRef.current = engine;
    void engine.start(optionsRef.current.sessionId, fetchStudentLiveKitToken);
  }, [buildEngine, supported]);

  /** Full disconnect: room, mic, recognizer, and the attached remote audio
   * track are all torn down by LiveKitPocEngine.end() itself (see its own
   * cleanup discipline) - this hook only needs to drop its reference and
   * reset local UI state. Unlike the legacy hook's stopConversation (which
   * only pauses, resumably), the LiveKit engine has no pause/resume concept -
   * stopping fully ends the room. Resuming (calling startConversation again)
   * mints a fresh token and joins a brand-NEW room (see Phase C3's
   * connection_id-suffixed student_room_name) rather than rejoining the one
   * just left, which is what makes Stop -> Resume safe even if the old
   * room is still tearing down on LiveKit's side. IDLE here (rather than a
   * dedicated "stopped" state) is intentional: ConversationControl already
   * derives "Resume" vs "Start" label/aria from hasConversation while IDLE,
   * so no new UI state is needed for Stop to read correctly. */
  const stopConversation = useCallback((): Promise<void> => {
    // Idempotent: a repeated Stop (or Stop while STOPPING) returns the same
    // in-flight cleanup promise rather than starting a second teardown.
    if (cleanupPromiseRef.current) return cleanupPromiseRef.current;
    const engine = engineRef.current;
    // Detach immediately so any late callback from this engine (most notably
    // its own delayed "ended") is guarded out by buildEngine's identity check
    // and can never stomp STOPPING/IDLE.
    engineRef.current = null;
    setStartupStage(null);
    if (!engine) {
      setState("IDLE");
      return Promise.resolve();
    }
    // Enter STOPPING now and only expose IDLE once teardown actually resolves -
    // the UI must never advertise a restartable IDLE while the old room/mic is
    // still shutting down.
    setState("STOPPING");
    const cleanup = engine.end().finally(() => {
      cleanupPromiseRef.current = null;
      setState("IDLE");
    });
    cleanupPromiseRef.current = cleanup;
    return cleanup;
  }, []);

  const reset = useCallback((): Promise<void> => {
    setErrorMessage(null);
    return stopConversation();
  }, [stopConversation]);

  const retry = useCallback((): Promise<void> => {
    // Retry only after any in-flight cleanup (Stop or fatal-error teardown)
    // fully completes, so the fresh engine never overlaps the spent one.
    const pending = cleanupPromiseRef.current ?? Promise.resolve();
    return pending.then(() => {
      setErrorMessage(null);
      startConversation();
    });
  }, [startConversation]);

  /** Phase D2: true SPEAKING-only interruption - delegates entirely to the
   * engine (see LiveKitPocEngine.interruptPatient's own docstring for the
   * THINKING-vs-SPEAKING rationale and the bounded ack timeout that always
   * returns to LISTENING). The D1 stale-callback guard already protects
   * every state change this can trigger (onStateChange/onTurnCompleted are
   * both wrapped in buildEngine() above), so no separate guard is needed
   * here - this call is a no-op once engineRef.current is null (Stop/reset/
   * unmount already happened). */
  const interruptPatient = useCallback(() => {
    engineRef.current?.interruptPatient();
  }, []);

  /** Typed input while LiveKit mode is active: sends through the SAME
   * engine.sendText() a spoken final transcript would use - same
   * "listening only" guard inside the engine, so a message typed while
   * thinking/speaking is safely dropped rather than double-submitted.
   * source: "manual_typed" is what keeps this working as an explicit
   * student turn even when semantic turn control is active for this
   * session - see sendText's docstring and worker.py's
   * TurnSource.MANUAL_OVERRIDE for why a deliberate typed Send is exempted
   * from the "browser text is non-authoritative under semantic control"
   * rule that governs "speech_browser"-sourced (SpeechRecognition) text. */
  const submitExternal = useCallback((text: string) => {
    void engineRef.current?.sendText(text, { source: "manual_typed" });
  }, []);

  // Full cleanup on unmount - the room/mic must never stay connected.
  useEffect(() => {
    return () => {
      void engineRef.current?.end();
      engineRef.current = null;
    };
  }, []);

  return {
    state,
    errorMessage,
    supported,
    active: isConversationActive(state),
    startupStage,
    startConversation,
    stopConversation,
    interruptPatient,
    retry,
    reset,
    submitExternal,
  };
}
