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
 * legacy consumer. LiveKit has no COOLDOWN/INTERRUPTING/PAUSED equivalent -
 * barge-in and pause/resume are explicitly out of scope (see
 * interruptPatient/stopConversation below). */
export type LiveKitVoiceUIState = Extract<
  VoiceConversationState,
  "IDLE" | "REQUESTING_PERMISSION" | "LISTENING" | "PROCESSING" | "SPEAKING" | "ERROR" | "FINISHED"
>;

function mapPocState(state: PocState): LiveKitVoiceUIState {
  switch (state) {
    case "idle":
      return "IDLE";
    case "connecting":
      return "REQUESTING_PERMISSION";
    case "listening":
      return "LISTENING";
    case "thinking":
      return "PROCESSING";
    case "speaking":
      return "SPEAKING";
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

  const buildEngine = useCallback((): LiveKitPocEngine => {
    return new LiveKitPocEngine({
      onStateChange: (pocState) => setState(mapPocState(pocState)),
      onStudentTranscript: (text, isFinal) => {
        optionsRef.current.onInterim(isFinal ? "" : text);
      },
      onError: (message) => setErrorMessage(message),
      onTurnCompleted: () => optionsRef.current.onTurnCompleted(),
      // POC-only diagnostics/room-name surfacing - the real InterviewPage
      // has no admin diagnostic panel and never displays a room name.
      onDiagnostics: () => {},
      onRoomName: () => {},
    });
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
   * only pauses, resumably), the LiveKit engine has no pause/resume concept
   * yet - stopping fully ends the room; starting again mints a fresh token
   * and rejoins the SAME room (same session id), which is safe/idempotent
   * but does re-dispatch a new agent job. Documented as a known Phase B
   * limitation, not silently pretended away. */
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

  /** Barge-in is explicitly OUT OF SCOPE for the LiveKit path (see
   * worker.py's PocAgentSession docstring: a student message that arrives
   * mid-turn is dropped, never used to interrupt playback). A no-op here,
   * not faked as a real interruption. */
  const interruptPatient = useCallback(() => {}, []);

  /** No engine-level equivalent of "cancel the in-flight patient turn"
   * exists (see interruptPatient) - a no-op, never a silent fallback. */
  const cancelPatientSpeech = useCallback(() => {}, []);

  /** Typed input while LiveKit mode is active: sends through the SAME
   * engine.sendText() a spoken final transcript would use - same
   * "listening only" guard inside the engine, so a message typed while
   * thinking/speaking is safely dropped rather than double-submitted. */
  const submitExternal = useCallback((text: string) => {
    void engineRef.current?.sendText(text);
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
