/**
 * iOS/mobile audio unlock.
 *
 * Mobile Safari (and, to a lesser extent, Android Chrome) only allows audio
 * playback that is initiated from a real user gesture. The patient's TTS is
 * produced AFTER an `await` (the backend round-trip), which breaks the gesture
 * chain — so on iPhone the first `new Audio().play()` is rejected and the
 * patient is silent.
 *
 * The fix is the standard "unlock" trick: during the SAME tap that starts the
 * conversation we (1) create/resume a shared AudioContext, (2) play a short
 * silent sound on a throwaway HTMLAudioElement, and (3) prime speechSynthesis
 * with an empty utterance. Once any audio has played from a gesture, iOS marks
 * the page's audio session active and later programmatic playback is allowed.
 *
 * Everything here is defensive (wrapped in try/catch) and idempotent: it is a
 * no-op after the first successful unlock and never throws into the caller.
 */

type WindowWithWebkitAudio = Window & {
  webkitAudioContext?: typeof AudioContext;
};

let sharedContext: AudioContext | null = null;
let unlocked = false;

/** A 1-sample silent WAV as a data URI — enough to satisfy iOS's "played from a
 * gesture" requirement without any audible output. */
const SILENT_WAV =
  "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA=";

/** Lazily create the process-wide AudioContext (reused by the VAD too, so iOS
 * doesn't run out of audio contexts). Returns null when Web Audio is absent. */
export function getSharedAudioContext(): AudioContext | null {
  if (sharedContext) return sharedContext;
  const Ctor =
    typeof AudioContext !== "undefined"
      ? AudioContext
      : (window as WindowWithWebkitAudio).webkitAudioContext;
  if (!Ctor) return null;
  try {
    sharedContext = new Ctor();
  } catch {
    sharedContext = null;
  }
  return sharedContext;
}

/** True once audio has been unlocked in this page session. */
export function isAudioUnlocked(): boolean {
  return unlocked;
}

/**
 * Unlock audio playback. MUST be called synchronously from within a user
 * gesture handler (e.g. the "Start Voice Conversation" tap) — before any
 * `await`. Safe to call repeatedly.
 */
export function unlockAudioPlayback(): void {
  // 1) AudioContext: create + resume (resume() only succeeds from a gesture on
  //    iOS). A resumed context is what later Web-Audio playback needs.
  try {
    const ctx = getSharedAudioContext();
    if (ctx) {
      if (ctx.state === "suspended") void ctx.resume().catch(() => undefined);
      // Play one silent buffer through the context to fully activate it.
      const buffer = ctx.createBuffer(1, 1, 22050);
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      source.start(0);
    }
  } catch {
    /* Web Audio unavailable — continue; the HTMLAudio prime below still helps. */
  }

  // 2) HTMLAudioElement: play a silent clip then immediately pause. This is the
  //    element path the patient TTS uses (`new Audio()`), so priming it here
  //    unlocks that path for the rest of the session on iOS.
  try {
    const el = new Audio(SILENT_WAV);
    el.muted = true;
    el.setAttribute("playsinline", "true");
    const p = el.play();
    if (p && typeof p.then === "function") {
      void p
        .then(() => {
          el.pause();
          el.currentTime = 0;
        })
        .catch(() => undefined);
    }
  } catch {
    /* ignore */
  }

  // 3) speechSynthesis (browser TTS fallback): iOS also gates it behind a
  //    gesture. Speaking an empty utterance here primes it without sound.
  try {
    if (typeof window !== "undefined" && "speechSynthesis" in window) {
      const u = new SpeechSynthesisUtterance("");
      u.volume = 0;
      window.speechSynthesis.speak(u);
      window.speechSynthesis.cancel();
    }
  } catch {
    /* ignore */
  }

  unlocked = true;
}

/** Resume the shared AudioContext if it exists and is suspended (e.g. after the
 * app returns from the background). Best-effort. */
export function resumeSharedAudioContext(): void {
  try {
    if (sharedContext && sharedContext.state === "suspended") {
      void sharedContext.resume().catch(() => undefined);
    }
  } catch {
    /* ignore */
  }
}

/** Testing/cleanup hook: forget unlock state (does not close the context). */
export function _resetAudioUnlockForTests(): void {
  unlocked = false;
  sharedContext = null;
}
