/**
 * Phase B: wraps the SAME LiveKitPocEngine the admin POC page uses (see
 * livekitPocEngine.ts - not a second/parallel LiveKit implementation) for
 * the REAL student InterviewPage. Exposes a result shape deliberately
 * compatible with useVoiceConversation's (state/errorMessage/supported/
 * active/startConversation/stopConversation/interruptPatient/retry/reset/
 * cancelPatientSpeech/submitExternal) so InterviewPage.tsx can pick ONE of
 * the two hooks per render and drive the rest of its UI unchanged.
 *
 * What's deliberately different from the admin POC page:
 * - Token source: fetchStudentLiveKitToken (require_session_access-gated),
 *   never the admin-only fetchAdminPocLiveKitToken.
 * - No manual room-name copying, no manual case/session entry - sessionId
 *   comes from the real interview's already-created session, caseId is
 *   never touched by the engine directly (it is embedded server-side by
 *   livekit_token_service.py's dispatch metadata, from the SAME session
 *   row - see backend/app/services/livekit_token_service.py).
 * - Patient TEXT is never invented client-side: onTurnCompleted only
 *   signals "a turn just finished" - the page re-fetches the authoritative
 *   transcript from the backend (the same DB rows the agent already wrote),
 *   exactly like the existing "resume an in-progress session" code path
 *   already does. This avoids adding a second, parallel text-delivery
 *   protocol to worker.py/patient_turn_status.
 *
 * NEVER calls speechSynthesis, patientVoiceService, or any legacy playback
 * primitive - see livekitPocEngine.ts's own docstring/tests for that
 * guarantee; this hook only adds React lifecycle around it.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  LiveKitPocEngine,
  fetchStudentLiveKitToken,
  type PocState,
} from "../services/livekit/livekitPocEngine";
import { isSpeechRecognitionSupported } from "../services/speechRecognitionService";
import { isConversationActive, type VoiceConversationState } from "./voiceStateMachine";

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
  /** Interim (non-final) recognized text, for display only - mirrors
   * useVoiceConversation's onInterim. Called with "" once a final result is
   * sent, matching the legacy hook's own draft-clearing behavior. */
  onInterim: (transcript: string) => void;
  /** Fires once per completed turn (the agent's "speaking_ended"). Carries
   * NO text - the page is expected to re-fetch the session's transcript,
   * the single source of truth, rather than trust a client-held copy. */
  onTurnCompleted: () => void;
}

export interface UseLiveKitInterviewVoiceResult {
  state: LiveKitVoiceUIState;
  errorMessage: string | null;
  supported: boolean;
  active: boolean;
  startConversation: () => void;
  stopConversation: () => void;
  interruptPatient: () => void;
  retry: () => void;
  reset: () => void;
  cancelPatientSpeech: () => void;
  submitExternal: (text: string) => void;
}

export function useLiveKitInterviewVoice(
  options: UseLiveKitInterviewVoiceOptions,
): UseLiveKitInterviewVoiceResult {
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const [state, setState] = useState<LiveKitVoiceUIState>("IDLE");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const engineRef = useRef<LiveKitPocEngine | null>(null);
  const supported = isSpeechRecognitionSupported();

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
        setState(mapPocState(pocState));
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
    if (engineRef.current) return; // already starting/started
    setErrorMessage(null);
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
  const stopConversation = useCallback(() => {
    void engineRef.current?.end();
    engineRef.current = null;
    setState("IDLE");
  }, []);

  const reset = useCallback(() => {
    void engineRef.current?.end();
    engineRef.current = null;
    setErrorMessage(null);
    setState("IDLE");
  }, []);

  const retry = useCallback(() => {
    engineRef.current = null; // the previous (ended/error) engine is spent
    setErrorMessage(null);
    setState("IDLE");
    startConversation();
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

  /** No engine-level equivalent of "cancel the in-flight patient turn"
   * exists beyond interruptPatient (SPEAKING-only, see above) - a no-op,
   * never a silent fallback. */
  const cancelPatientSpeech = useCallback(() => {}, []);

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
    startConversation,
    stopConversation,
    interruptPatient,
    retry,
    reset,
    cancelPatientSpeech,
    submitExternal,
  };
}
