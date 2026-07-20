import { useCallback, useEffect, useRef, useState } from "react";
import {
  createRecognizer,
  describeRecognitionError,
  isSpeechRecognitionSupported,
  type Recognizer,
} from "../services/speechRecognitionService";
import { isTtsSupported } from "../services/textToSpeechService";
import {
  cancelPatientSpeech as cancelVoicePlayback,
  speakPatientResponse,
} from "../services/patientVoiceService";
import type { PatientExchange } from "../types/interview";
import {
  createVoiceActivityDetector,
  type AudioSetup,
  type InterruptionSensitivity,
  type VoiceActivityDetector,
} from "../services/voiceActivityDetector";
import {
  initialMachine,
  isConversationActive,
  reduce,
  type VoiceConversationState,
  type VoiceEvent,
  type VoiceMachine,
} from "./voiceStateMachine";

const COOLDOWN_MS = 800;
const INTERRUPT_SETTLE_MS = 300; // wait after cancelling TTS before listening

/** Microphone constraints: ask the browser for echo cancellation so speaker
 * audio is attenuated at the source. Browsers ignore unsupported keys. */
const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
};

function devLogTrackSettings(stream: MediaStream): void {
  if (!import.meta.env.DEV) return;
  const track = stream.getAudioTracks()[0];
  if (!track) return;
  const s = track.getSettings();
  console.debug("[mic] track settings", {
    echoCancellation: s.echoCancellation,
    noiseSuppression: s.noiseSuppression,
    autoGainControl: s.autoGainControl,
    hasDeviceId: Boolean(s.deviceId),
    sampleRate: s.sampleRate,
  });
}

export interface UseVoiceConversationOptions {
  patientName: string;
  /** Active case id: selects the patient's backend voice profile for TTS. */
  caseId: string;
  /** Backend session id: lets the voice endpoint verify the synthesized text
   * against the saved patient turn. */
  sessionId: string | null;
  /** session ready + backend connected */
  enabled: boolean;
  speakReplies: boolean;
  /** Automatic (voice-triggered) interruption. Manual interruption always works. */
  autoInterrupt: boolean;
  audioSetup: AudioSetup;
  sensitivity: InterruptionSensitivity;
  /** Existing backend submission. Must resolve to the REAL exchange from
   * FastAPI (approved patientText + turn reference + delivery labels), or
   * throw. The hook never generates patient text itself. */
  onSubmitQuestion: (text: string, source?: "typed" | "speech") => Promise<PatientExchange>;
  /** Interim transcript display (e.g. into the chat input). */
  onInterim: (transcript: string) => void;
}

export interface UseVoiceConversationResult {
  state: VoiceConversationState;
  errorMessage: string | null;
  supported: boolean;
  ttsSupported: boolean;
  active: boolean;
  startConversation: () => void;
  stopConversation: () => void;
  interruptPatient: () => void;
  retry: () => void;
  /** Full teardown (route/case change, unmount, interview end). */
  reset: () => void;
  /** Cancel patient speech so a typed question can act as an interruption. */
  cancelPatientSpeech: () => void;
  /** Route a typed question through the voice loop while it is active. */
  submitExternal: (text: string) => void;
}

export function useVoiceConversation(
  options: UseVoiceConversationOptions,
): UseVoiceConversationResult {
  const [machine, setMachine] = useState<VoiceMachine>(initialMachine);
  const machineRef = useRef(machine);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const recognizerRef = useRef<Recognizer | null>(null);
  const vadRef = useRef<VoiceActivityDetector | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const timersRef = useRef<number[]>([]);
  const inFlightRef = useRef(false);
  const interruptionInProgressRef = useRef(false); // duplicate-trigger lock

  const supported = isSpeechRecognitionSupported();
  const tts = isTtsSupported();

  const dispatch = useCallback((event: VoiceEvent): VoiceMachine => {
    const next = reduce(machineRef.current, event);
    machineRef.current = next;
    setMachine(next);
    return next;
  }, []);

  const later = useCallback((fn: () => void, ms: number) => {
    const id = window.setTimeout(fn, ms);
    timersRef.current.push(id);
    return id;
  }, []);

  const clearTimers = useCallback(() => {
    timersRef.current.forEach((id) => window.clearTimeout(id));
    timersRef.current = [];
  }, []);

  const stopRecognition = useCallback(() => {
    recognizerRef.current?.abort();
    recognizerRef.current = null;
  }, []);

  const releaseMedia = useCallback(() => {
    vadRef.current?.dispose();
    vadRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  /** Stop everything the voice loop owns (mic, VAD, TTS, timers). */
  const teardown = useCallback(() => {
    clearTimers();
    stopRecognition();
    vadRef.current?.stopMonitoring();
    cancelVoicePlayback(); // aborts pending TTS + stops ElevenLabs/browser audio
  }, [clearTimers, stopRecognition]);

  // ------------------------------------------------------------------
  const startListening = useCallback(() => {
    if (!isConversationActive(machineRef.current.state)) return;
    if (machineRef.current.state !== "LISTENING") return;
    stopRecognition();
    const recognizer = createRecognizer({
      onInterim: (transcript) => optionsRef.current.onInterim(transcript),
      onFinal: (transcript) => {
        void handleFinalTranscript(transcript);
      },
      onError: (error) => {
        const { message, fatal } = describeRecognitionError(error);
        const next = dispatch({ type: "RECOGNITION_ERROR", message, fatal });
        if (fatal) {
          teardown();
        } else if (next.state === "LISTENING") {
          // non-fatal (e.g. no-speech): recognizer will fire onEnd; restart there
        }
      },
      onEnd: () => {
        // Browser one-shot recognition ended without a usable final result:
        // keep the conversation loop alive by listening again.
        if (machineRef.current.state === "LISTENING") {
          later(() => startListening(), 150);
        }
      },
    });
    if (!recognizer) return;
    recognizerRef.current = recognizer;
    try {
      recognizer.start();
    } catch {
      // start() throws if a session is already active; retry shortly
      later(() => startListening(), 250);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const enterCooldownThenListen = useCallback(() => {
    later(() => {
      const next = dispatch({ type: "COOLDOWN_ELAPSED" });
      if (next.state === "LISTENING") startListening();
    }, COOLDOWN_MS);
  }, [dispatch, later, startListening]);

  const beginSpeaking = useCallback(
    (exchange: PatientExchange) => {
      vadRef.current?.stopMonitoring();
      interruptionInProgressRef.current = false; // rearm for this utterance

      // STREAMING path: sentence audio is already owned by the streaming
      // handle (it may even be playing). Wire barge-in VAD to the actual
      // playback start and settle the machine when ALL audio finished.
      if (exchange.streaming) {
        const handle = exchange.streaming;
        handle.setOnPlaybackStart(() => {
          if (!optionsRef.current.autoInterrupt) return;
          vadRef.current?.startMonitoring({
            sensitivity: optionsRef.current.sensitivity,
            audioSetup: optionsRef.current.audioSetup,
            onSustainedVoice: () => {
              if (machineRef.current.state === "SPEAKING") interruptPatient();
            },
          });
        });
        void handle.playbackDone.then(() => {
          handle.setOnPlaybackStart(null);
          vadRef.current?.stopMonitoring();
          const next = dispatch({ type: "TTS_ENDED" });
          if (next.state === "COOLDOWN") enterCooldownThenListen();
          // if INTERRUPTING, the interrupt flow owns what's next
        });
        return;
      }

      // Provider-based TTS: ElevenLabs via FastAPI when available for this
      // case, browser speechSynthesis as the fallback. Speaks EXACTLY the
      // approved patientText - the same text already in the transcript.
      void speakPatientResponse({
        caseId: optionsRef.current.caseId,
        text: exchange.patientText,
        sessionId: optionsRef.current.sessionId ?? undefined,
        turnId: exchange.turnId,
        speechStyle: exchange.speech,
        onStart: () => {
          // Automatic barge-in is OPTIONAL. When it is off, the detector is
          // not accessed at all during patient speech - only the manual
          // Interrupt button can stop the patient.
          if (!optionsRef.current.autoInterrupt) return;
          // Monitor audio LEVEL only (no transcription). The detector's
          // protection window starts here, at actual playback start; it then
          // samples the speaker echo to build a dynamic baseline.
          vadRef.current?.startMonitoring({
            sensitivity: optionsRef.current.sensitivity,
            audioSetup: optionsRef.current.audioSetup,
            onSustainedVoice: () => {
              if (machineRef.current.state === "SPEAKING") interruptPatient();
            },
          });
        },
      }).then(() => {
        vadRef.current?.stopMonitoring();
        const next = dispatch({ type: "TTS_ENDED" });
        if (next.state === "COOLDOWN") enterCooldownThenListen();
        // if state moved to INTERRUPTING, the interrupt flow owns what's next
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  async function handleFinalTranscript(transcript: string) {
    const next = dispatch({ type: "FINAL_TRANSCRIPT", text: transcript });
    if (next.state !== "PROCESSING" || next.acceptedTranscript === null) return; // rejected: noise or wrong state
    if (inFlightRef.current) return; // only one backend request at a time
    stopRecognition(); // recognition must be fully off before the request
    optionsRef.current.onInterim("");
    await runExchange(next.acceptedTranscript, "speech");
  }

  async function runExchange(question: string, source: "typed" | "speech" = "speech") {
    inFlightRef.current = true;
    try {
      const exchange = await optionsRef.current.onSubmitQuestion(question, source);
      const speakIt = optionsRef.current.speakReplies && tts;
      const next = dispatch({ type: "RESPONSE_RECEIVED", speak: speakIt });
      if (next.state === "SPEAKING") beginSpeaking(exchange);
      else if (next.state === "COOLDOWN") enterCooldownThenListen();
    } catch (err) {
      const message =
        err instanceof Error && err.message
          ? err.message
          : "The patient response could not be generated. Please retry.";
      teardown();
      dispatch({ type: "RESPONSE_FAILED", message });
    } finally {
      inFlightRef.current = false;
    }
  }

  // ------------------------------------------------------------------
  const startConversation = useCallback(() => {
    if (!supported || !optionsRef.current.enabled) return;
    const current = machineRef.current.state;
    const next =
      current === "PAUSED" ? dispatch({ type: "RESUME" }) : dispatch({ type: "START" });
    if (next.state !== "REQUESTING_PERMISSION") return;

    void (async () => {
      try {
        if (!streamRef.current) {
          streamRef.current = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
          devLogTrackSettings(streamRef.current);
          vadRef.current = createVoiceActivityDetector(streamRef.current);
        }
        // Ambient-noise calibration (initial floor for the echo baseline).
        await vadRef.current?.calibrate(600);
        const granted = dispatch({ type: "PERMISSION_GRANTED" });
        if (granted.state === "LISTENING") startListening();
      } catch (err) {
        console.error("Microphone unavailable:", err);
        releaseMedia();
        dispatch({
          type: "PERMISSION_DENIED",
          message:
            "Microphone access was denied or no microphone is available. Allow access and retry, or continue by typing.",
        });
      }
    })();
  }, [dispatch, releaseMedia, startListening, supported]);

  const stopConversation = useCallback(() => {
    teardown();
    optionsRef.current.onInterim("");
    dispatch({ type: "STOP" });
  }, [dispatch, teardown]);

  const interruptPatient = useCallback(() => {
    // Interruption lock: detector callbacks, double-clicks, or races must not
    // cancel speech twice or start recognition twice.
    if (interruptionInProgressRef.current) return;
    const next = dispatch({ type: "INTERRUPT" });
    if (next.state !== "INTERRUPTING") return;
    interruptionInProgressRef.current = true;
    // Stop the patient immediately (exactly once): aborts any pending
    // synthesis request AND stops the active audio element / browser speech.
    cancelVoicePlayback();
    vadRef.current?.stopMonitoring();
    // Settle delay so recognition doesn't catch the speaker tail/echo.
    later(() => {
      const ready = dispatch({ type: "INTERRUPT_READY" });
      interruptionInProgressRef.current = false;
      if (ready.state === "LISTENING") startListening();
    }, INTERRUPT_SETTLE_MS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const retry = useCallback(() => {
    if (machineRef.current.state !== "ERROR") return;
    // Re-enter through permission/calibration (stream may still be alive).
    const next = dispatch({ type: "RETRY" });
    if (next.state !== "REQUESTING_PERMISSION") return;
    void (async () => {
      try {
        if (!streamRef.current) {
          streamRef.current = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
          devLogTrackSettings(streamRef.current);
          vadRef.current = createVoiceActivityDetector(streamRef.current);
        }
        await vadRef.current?.calibrate(400);
        const granted = dispatch({ type: "PERMISSION_GRANTED" });
        if (granted.state === "LISTENING") startListening();
      } catch {
        releaseMedia();
        dispatch({
          type: "PERMISSION_DENIED",
          message: "Microphone access is still unavailable. You can continue by typing.",
        });
      }
    })();
  }, [dispatch, releaseMedia, startListening]);

  const reset = useCallback(() => {
    teardown();
    releaseMedia();
    inFlightRef.current = false;
    interruptionInProgressRef.current = false;
    machineRef.current = initialMachine;
    setMachine(initialMachine);
  }, [releaseMedia, teardown]);

  const cancelPatientSpeech = useCallback(() => {
    if (machineRef.current.state === "SPEAKING") {
      vadRef.current?.stopMonitoring();
      cancelVoicePlayback();
      dispatch({ type: "TTS_ENDED" });
      clearTimers();
      // The typed flow takes over; move loop to LISTENING later via cooldown.
      enterCooldownThenListen();
    } else {
      cancelVoicePlayback();
    }
  }, [clearTimers, dispatch, enterCooldownThenListen]);

  /** Typed question while voice mode is running: acts as an interruption and
   * goes through the same backend pipeline. */
  const submitExternal = useCallback((text: string) => {
    if (!isConversationActive(machineRef.current.state)) return;
    if (inFlightRef.current) return;
    cancelVoicePlayback();
    vadRef.current?.stopMonitoring();
    stopRecognition();
    clearTimers();
    // Force the machine through the same path a spoken final transcript takes.
    if (machineRef.current.state !== "LISTENING") {
      machineRef.current = { ...machineRef.current, state: "LISTENING" };
    }
    const next = dispatch({ type: "FINAL_TRANSCRIPT", text });
    if (next.state === "PROCESSING" && next.acceptedTranscript) {
      void runExchange(next.acceptedTranscript, "typed"); // typed barge-in
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Full cleanup on unmount - the microphone must never stay active.
  useEffect(() => {
    return () => {
      clearTimers();
      recognizerRef.current?.abort();
      cancelVoicePlayback();
      vadRef.current?.dispose();
      streamRef.current?.getTracks().forEach((track) => track.stop());
    };
  }, [clearTimers]);

  return {
    state: machine.state,
    errorMessage: machine.errorMessage,
    supported,
    ttsSupported: tts,
    active: isConversationActive(machine.state),
    startConversation,
    stopConversation,
    interruptPatient,
    retry,
    reset,
    cancelPatientSpeech,
    submitExternal,
  };
}
