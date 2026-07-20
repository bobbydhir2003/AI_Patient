/**
 * Browser speech-recognition service (Web Speech API).
 * Transcribes the student's voice only - it never generates patient text.
 */

interface RecognitionAlternative {
  transcript: string;
}
interface RecognitionResult {
  isFinal: boolean;
  0: RecognitionAlternative;
}
interface RecognitionEvent {
  resultIndex: number;
  results: ArrayLike<RecognitionResult>;
}
interface RecognitionErrorEvent {
  error: string;
}
interface BrowserSpeechRecognition {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onstart: (() => void) | null;
  onresult: ((event: RecognitionEvent) => void) | null;
  onerror: ((event: RecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

type RecognitionConstructor = new () => BrowserSpeechRecognition;

function getRecognitionConstructor(): RecognitionConstructor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: RecognitionConstructor;
    webkitSpeechRecognition?: RecognitionConstructor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function isSpeechRecognitionSupported(): boolean {
  return getRecognitionConstructor() !== null;
}

/** Map raw Web Speech error names to understandable messages. */
export function describeRecognitionError(error: string): { message: string; fatal: boolean } {
  switch (error) {
    case "not-allowed":
    case "service-not-allowed":
      return {
        message:
          "Microphone permission was denied. Allow microphone access in the browser and retry, or continue by typing.",
        fatal: true,
      };
    case "audio-capture":
      return {
        message: "No microphone was found, or it is in use by another application.",
        fatal: true,
      };
    case "network":
      return { message: "The browser speech service could not be reached.", fatal: true };
    case "no-speech":
      return { message: "No speech was detected.", fatal: false };
    case "aborted":
      return { message: "Listening stopped.", fatal: false };
    default:
      return { message: `Speech recognition problem (${error}).`, fatal: false };
  }
}

export interface RecognizerCallbacks {
  onStart?: () => void;
  onInterim: (transcript: string) => void;
  onFinal: (transcript: string) => void;
  onError: (error: string) => void;
  onEnd: () => void;
}

export interface Recognizer {
  start: () => void;
  stop: () => void;
  abort: () => void;
}

export function createRecognizer(callbacks: RecognizerCallbacks): Recognizer | null {
  const Ctor = getRecognitionConstructor();
  if (!Ctor) return null;

  const recognition = new Ctor();
  recognition.lang = "en-US";
  recognition.continuous = false;
  recognition.interimResults = true;

  let finalDelivered = false; // a recognizer instance delivers at most one final

  recognition.onstart = () => callbacks.onStart?.();
  recognition.onresult = (event) => {
    let interim = "";
    let final = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const result = event.results[i];
      if (result.isFinal) final += result[0].transcript;
      else interim += result[0].transcript;
    }
    if (final.trim() && !finalDelivered) {
      finalDelivered = true;
      callbacks.onFinal(final.trim());
    } else if (interim.trim()) {
      callbacks.onInterim(interim.trim());
    }
  };
  recognition.onerror = (event) => callbacks.onError(event.error);
  recognition.onend = () => callbacks.onEnd();

  return {
    start: () => recognition.start(),
    stop: () => recognition.stop(),
    abort: () => {
      recognition.onresult = null;
      recognition.onerror = null;
      recognition.onend = null;
      recognition.abort();
    },
  };
}
