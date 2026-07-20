/**
 * Text-to-speech service (window.speechSynthesis).
 * Speaks ONLY the real patientText returned from FastAPI - this service never
 * generates or modifies the patient's medical answer.
 */

export function isTtsSupported(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

let cachedVoice: SpeechSynthesisVoice | null | undefined;

/** Pick a suitable English voice when available; fall back to the browser
 * default. No gender assumptions - preference is simply: local en-US, any
 * en-US, any English, default. */
function pickVoice(): SpeechSynthesisVoice | null {
  if (cachedVoice !== undefined) return cachedVoice;
  const voices = window.speechSynthesis.getVoices();
  if (!voices.length) return null; // not loaded yet; don't cache
  cachedVoice =
    voices.find((v) => v.lang === "en-US" && v.localService) ??
    voices.find((v) => v.lang === "en-US") ??
    voices.find((v) => v.lang.startsWith("en")) ??
    null;
  return cachedVoice;
}

// Voice lists load asynchronously in some browsers.
if (isTtsSupported()) {
  window.speechSynthesis.onvoiceschanged = () => {
    cachedVoice = undefined;
    pickVoice();
  };
}

export interface SpeakCallbacks {
  onStart?: () => void;
  onEnd?: () => void;
}

/**
 * Speak text aloud, cancelling any prior utterance first.
 * Resolves when speech ends (or immediately if TTS is unsupported/cancelled).
 * `rate` lets the caller apply the case's fallback speaking rate.
 */
export function speak(
  text: string,
  callbacks: SpeakCallbacks = {},
  rate: number = 0.97,
): Promise<void> {
  return new Promise((resolve) => {
    if (!isTtsSupported() || !text.trim()) {
      callbacks.onEnd?.();
      resolve();
      return;
    }
    window.speechSynthesis.cancel(); // only one patient utterance at a time
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "en-US";
    utterance.rate = Math.max(0.5, Math.min(1.5, rate));
    utterance.pitch = 1.0;
    utterance.volume = 1.0;
    const voice = pickVoice();
    if (voice) utterance.voice = voice;
    utterance.onstart = () => callbacks.onStart?.();
    utterance.onend = () => {
      callbacks.onEnd?.();
      resolve();
    };
    utterance.onerror = () => {
      callbacks.onEnd?.();
      resolve();
    };
    window.speechSynthesis.speak(utterance);
  });
}

/** Immediately stop any current or queued patient speech. */
export function cancelSpeech(): void {
  if (isTtsSupported()) window.speechSynthesis.cancel();
}
