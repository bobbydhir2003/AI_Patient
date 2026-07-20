/**
 * Provider-based patient voice service.
 *
 * ElevenLabs (via the FastAPI backend - the API key never reaches the browser)
 * is the primary provider; the original browser speechSynthesis service is a
 * real fallback, used whenever ElevenLabs is disabled, unconfigured for the
 * case, or fails at request/playback time.
 *
 * This service speaks ONLY the approved patientText returned from FastAPI -
 * it never generates or modifies the patient's medical answer.
 *
 * Playback model (progressive):
 *   FastAPI streams ElevenLabs MP3 chunks → the browser reads response.body
 *   with a stream reader and appends chunks into a MediaSource SourceBuffer
 *   ('audio/mpeg') → playback starts as soon as the FIRST chunk is buffered
 *   and the (overlapped) pauseBeforeMs has elapsed → remaining chunks keep
 *   buffering during playback. Playback genuinely begins before the full
 *   response is downloaded.
 *
 *   If MediaSource/'audio/mpeg' is unsupported, a Blob download of the same
 *   response is played instead (full buffering, ElevenLabs voice preserved).
 *   If ElevenLabs or streaming playback fails entirely, browser TTS speaks
 *   the same approved text. If everything fails, the transcript still shows
 *   the reply and the interview continues.
 *
 * Pause overlap: pauseBeforeMs starts ticking at TTS REQUEST start; before
 * playback only the remaining portion is awaited (see remainingPauseMs).
 *
 * Race-condition rules live in voicePlaybackState (pure + unit-tested):
 * only the latest generation may play; cancellation invalidates everything;
 * the progressive reducer makes CANCEL/FAIL terminal in every phase.
 */
import { API_BASE_URL, fetchVoiceStatus } from "./api";
import { cancelActiveStream } from "./streamRegistry";
import {
  cancelSpeech as cancelBrowserSpeech,
  isTtsSupported as isBrowserTtsSupported,
  speak as browserSpeak,
} from "./textToSpeechService";
import {
  canAppendChunk,
  canStartPlayback,
  chooseProvider,
  clampPauseMs,
  createPlaybackGuard,
  initialProgressiveState,
  isTerminal,
  reduceProgressive,
  remainingPauseMs,
  type ProgressiveEvent,
  type ProgressiveState,
} from "./voicePlaybackState";
import type { PatientSpeechStyle } from "../types/interview";

export interface SpeakOptions {
  caseId: string;
  /** The approved patient text - exactly what the transcript shows. */
  text: string;
  /** Session + turn reference: lets the backend verify the text against the
   * saved patient turn (no arbitrary text can be synthesized). */
  sessionId?: string;
  turnId?: string;
  speechStyle?: PatientSpeechStyle | null;
  /** Fires when audio actually starts playing (used to arm barge-in VAD). */
  onStart?: () => void;
}

/** If progressive playback has not actually started ('playing') within this
 * window, the attempt is failed over to the next provider. Prevents a silent
 * MediaSource stall from ever leaving the speaking promise pending. */
export const PROGRESSIVE_PLAYBACK_START_TIMEOUT_MS = 10_000;

const guard = createPlaybackGuard();

let activeAbortController: AbortController | null = null;
let activeAudio: HTMLAudioElement | null = null;
let activeObjectUrl: string | null = null;
let activeReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
/** Resolver of the ACTIVE SPEAKING PROMISE only. Delay timers have their own
 * resolver (delayResolve) - the two must never share a slot, or cancellation
 * could settle a dead delay instead of the speaking promise. */
let activeResolve: (() => void) | null = null;
/** Resolver of the pending cancellable delay (pauseBeforeMs) only. */
let delayResolve: (() => void) | null = null;
let pauseTimer: number | null = null;
let speaking = false;

/** Per-case availability + fallback rate, cached for the page's lifetime. */
const voiceStatusCache = new Map<string, { available: boolean; fallbackRate: number }>();

function devLog(event: string, detail?: unknown): void {
  if (import.meta.env.DEV) console.debug(`[patient-voice] ${event}`, detail ?? "");
}

/** Development-only latency instrumentation (performance.now() based). */
function devTiming(label: string, sinceMs: number): void {
  if (import.meta.env.DEV) {
    console.debug(`[patient-voice][timing] ${label}: ${Math.round(performance.now() - sinceMs)} ms`);
  }
}

async function getVoiceStatus(caseId: string): Promise<{ available: boolean; fallbackRate: number }> {
  const cached = voiceStatusCache.get(caseId);
  if (cached) return cached;
  try {
    const status = await fetchVoiceStatus(caseId);
    const entry = {
      available: status.available === true,
      fallbackRate: typeof status.fallbackRate === "number" ? status.fallbackRate : 0.97,
    };
    voiceStatusCache.set(caseId, entry);
    devLog("voice status", { caseId, provider: status.provider });
    return entry;
  } catch (err) {
    devLog("voice status check failed; using browser TTS", err);
    return { available: false, fallbackRate: 0.97 }; // not cached: backend may recover
  }
}

/** Allow a re-check after backend configuration changes (e.g. new case). */
export function clearVoiceStatusCache(): void {
  voiceStatusCache.clear();
}

/** MediaSource progressive playback support for the streamed MP3 format. */
function canUseMediaSource(): boolean {
  return (
    typeof MediaSource !== "undefined" &&
    typeof MediaSource.isTypeSupported === "function" &&
    MediaSource.isTypeSupported("audio/mpeg")
  );
}

function releaseAudio(): void {
  // Stop reading future chunks first so nothing new is appended.
  if (activeReader) {
    void activeReader.cancel().catch(() => undefined);
    activeReader = null;
  }
  if (activeAudio) {
    // Full release per the interruption requirements: pause, rewind, detach
    // the source (also detaches any MediaSource), and drop buffers.
    activeAudio.onplay = null;
    activeAudio.onended = null;
    activeAudio.onerror = null;
    activeAudio.pause();
    try {
      activeAudio.currentTime = 0;
    } catch {
      // ignore: some browsers throw before metadata is loaded
    }
    activeAudio.removeAttribute("src");
    activeAudio.load();
    activeAudio = null;
  }
  if (activeObjectUrl) {
    URL.revokeObjectURL(activeObjectUrl);
    activeObjectUrl = null;
  }
  if (pauseTimer !== null) {
    window.clearTimeout(pauseTimer);
    pauseTimer = null;
  }
  // Settle any pending delay so awaiting code proceeds to its guard checks
  // (which will see the cancellation/terminal state and bail out safely).
  const pendingDelay = delayResolve;
  delayResolve = null;
  pendingDelay?.();
  speaking = false;
}

/** Cancellable wait; resolves early if cancelled via cancelPatientSpeech
 * (the caller re-checks the playback guard afterwards). Uses its OWN resolver
 * slot - it must never touch activeResolve, which belongs to the speaking
 * promise. */
function cancellableDelay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    if (ms <= 0) {
      resolve();
      return;
    }
    const timer = window.setTimeout(() => {
      if (pauseTimer === timer) pauseTimer = null;
      delayResolve = null;
      resolve();
    }, ms);
    pauseTimer = timer;
    delayResolve = resolve;
  });
}

// ---------------------------------------------------------------------------
// ElevenLabs playback
// ---------------------------------------------------------------------------

async function playElevenLabs(options: SpeakOptions, generation: number, t0: number): Promise<void> {
  const abort = new AbortController();
  activeAbortController = abort;
  const requestStartedAt = performance.now(); // pause overlap starts HERE
  devLog("tts request start", { caseId: options.caseId, provider: "elevenlabs" });
  devTiming("patient response → TTS request start", t0);

  const response = await fetch(`${API_BASE_URL}/api/voice/synthesize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal: abort.signal,
    body: JSON.stringify({
      caseId: options.caseId,
      text: options.text,
      sessionId: options.sessionId ?? "",
      turnId: options.turnId ?? "",
      speechStyle: options.speechStyle ?? null,
    }),
  });
  if (!response.ok) {
    throw new Error(`voice_synthesis_http_${response.status}`);
  }
  devTiming("patient response → response headers", t0);
  const pauseBeforeMs = clampPauseMs(
    response.headers.get("X-Pause-Before-Ms") ?? options.speechStyle?.pauseBeforeMs ?? 150,
  );

  if (canUseMediaSource() && response.body) {
    await playProgressive(response.body, options, generation, pauseBeforeMs, requestStartedAt, t0);
    return;
  }
  // MediaSource unsupported in this browser: keep the ElevenLabs voice via
  // Blob playback (full buffering), still with the overlapped pause.
  devLog("MediaSource unsupported; using Blob playback", { caseId: options.caseId });
  await playBuffered(response, options, generation, pauseBeforeMs, requestStartedAt, t0);
}

/**
 * Progressive playback: stream-read the response body and append chunks into
 * a MediaSource SourceBuffer. Playback starts after the FIRST appended chunk
 * (plus any remaining overlapped pause) - before the download completes.
 */
function playProgressive(
  body: ReadableStream<Uint8Array>,
  options: SpeakOptions,
  generation: number,
  pauseBeforeMs: number,
  requestStartedAt: number,
  t0: number,
): Promise<void> {
  let state: ProgressiveState = initialProgressiveState;
  const dispatch = (event: ProgressiveEvent): ProgressiveState => {
    state = reduceProgressive(state, event);
    return state;
  };

  const mediaSource = new MediaSource();
  const url = URL.createObjectURL(mediaSource);
  const audio = new Audio();
  // PRIMARY WIRING: attach the MediaSource object URL to the element. Without
  // this assignment 'sourceopen' never fires and nothing can ever play.
  audio.src = url;
  activeAudio = audio;
  activeObjectUrl = url;
  const reader = body.getReader();
  activeReader = reader;

  return new Promise<void>((resolve, reject) => {
    let settled = false;

    // Watchdog: if actual playback ('playing') hasn't begun within the
    // timeout, fail this attempt so the caller's existing fallback chain
    // (browser TTS) takes over. Cleared on playing / success / fail / cancel.
    let watchdogTimer: number | null = window.setTimeout(() => {
      watchdogTimer = null;
      devLog("progressive playback did not start in time; failing over", {
        timeoutMs: PROGRESSIVE_PLAYBACK_START_TIMEOUT_MS,
      });
      fail(new Error("voice_progressive_start_timeout"));
    }, PROGRESSIVE_PLAYBACK_START_TIMEOUT_MS);
    const clearWatchdog = () => {
      if (watchdogTimer !== null) {
        window.clearTimeout(watchdogTimer);
        watchdogTimer = null;
      }
    };

    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      clearWatchdog();
      fn();
    };
    const fail = (err: unknown) => {
      if (settled) return;
      dispatch({ type: "FAIL" }); // terminal: late chunks/events are absorbed
      releaseAudio();
      finish(() => reject(err instanceof Error ? err : new Error("voice_playback_failed")));
    };
    // Cancellation (interrupt / new turn / teardown) in ANY phase: terminal
    // CANCEL absorbs all later chunks and events; resolve (not an error).
    activeResolve = () => {
      dispatch({ type: "CANCEL" });
      finish(resolve);
    };

    const pendingChunks: Uint8Array[] = [];
    let sourceBuffer: SourceBuffer | null = null;
    let playbackRequested = false;

    const maybeEndOfStream = () => {
      if (
        state.streamComplete &&
        pendingChunks.length === 0 &&
        sourceBuffer !== null &&
        !sourceBuffer.updating &&
        mediaSource.readyState === "open"
      ) {
        try {
          mediaSource.endOfStream();
        } catch {
          // ignore: element may already be detached by cancellation
        }
      }
    };

    const appendNext = () => {
      if (settled || sourceBuffer === null || sourceBuffer.updating) return;
      if (isTerminal(state)) return; // stale chunks after cancel/fail: dropped
      const chunk = pendingChunks.shift();
      if (chunk === undefined) {
        maybeEndOfStream();
        return;
      }
      try {
        sourceBuffer.appendBuffer(chunk as BufferSource);
      } catch (err) {
        fail(err); // decode/append failure → browser TTS fallback upstream
      }
    };

    const maybeStartPlayback = () => {
      if (playbackRequested || settled) return;
      if (!canStartPlayback(state)) return; // needs >=1 buffered chunk
      playbackRequested = true;
      // Overlapped pause: wait only what the network hasn't already consumed.
      const wait = remainingPauseMs(pauseBeforeMs, performance.now() - requestStartedAt);
      devLog("pause overlap", { pauseBeforeMs, remainingWaitMs: wait });
      void cancellableDelay(wait).then(() => {
        if (settled || !guard.isCurrent(generation) || isTerminal(state)) return;
        dispatch({ type: "PLAYBACK_STARTED" });
        devTiming("patient response → playback start", t0);
        audio.play().catch((err: unknown) => fail(err));
      });
    };

    audio.onplay = () => {
      clearWatchdog(); // playback actually began
      speaking = true;
      devLog("playback start", { caseId: options.caseId, progressive: true });
      options.onStart?.();
    };
    audio.onended = () => {
      if (dispatch({ type: "PLAYBACK_ENDED" }).phase !== "ended") return; // stale event
      devLog("playback end", { caseId: options.caseId });
      devTiming("patient response → playback ended", t0);
      releaseAudio();
      finish(resolve);
    };
    audio.onerror = () => fail(new Error("voice_playback_failed"));

    mediaSource.addEventListener(
      "sourceopen",
      () => {
        if (settled || !guard.isCurrent(generation)) return;
        try {
          sourceBuffer = mediaSource.addSourceBuffer("audio/mpeg");
        } catch (err) {
          fail(err);
          return;
        }
        sourceBuffer.addEventListener("updateend", () => {
          maybeStartPlayback(); // first chunk buffered → playback may begin
          appendNext();
          maybeEndOfStream();
        });
        sourceBuffer.addEventListener("error", () => fail(new Error("voice_sourcebuffer_error")));
        appendNext(); // chunks may already be queued
      },
      { once: true },
    );

    // Stream-read loop: runs concurrently with buffering and playback.
    void (async () => {
      let firstChunk = true;
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (settled || !guard.isCurrent(generation) || isTerminal(state)) return; // stale
          if (done) {
            dispatch({ type: "STREAM_ENDED" });
            devTiming("patient response → full audio downloaded", t0);
            appendNext(); // flush → endOfStream once the queue drains
            maybeEndOfStream();
            return;
          }
          if (value && value.byteLength > 0) {
            if (firstChunk) {
              firstChunk = false;
              devTiming("patient response → first audio chunk", t0);
            }
            if (!canAppendChunk(state)) return; // cancelled/failed mid-read
            dispatch({ type: "CHUNK" });
            pendingChunks.push(value);
            appendNext();
          }
        }
      } catch (err) {
        // AbortError from cancellation is not a failure; the CANCEL path
        // already resolved the promise. Anything else is a real error.
        if (settled || !guard.isCurrent(generation)) return;
        fail(err);
      }
    })();
  });
}

/** Blob playback (MediaSource-unsupported browsers only). Full download, then
 * play - still using the overlapped pause. */
async function playBuffered(
  response: Response,
  options: SpeakOptions,
  generation: number,
  pauseBeforeMs: number,
  requestStartedAt: number,
  t0: number,
): Promise<void> {
  const blob = await response.blob();
  activeAbortController = null;
  if (!guard.isCurrent(generation)) return; // cancelled/superseded while downloading
  if (blob.size === 0) throw new Error("voice_synthesis_empty_audio");
  devTiming("patient response → full audio downloaded (blob path)", t0);

  const wait = remainingPauseMs(pauseBeforeMs, performance.now() - requestStartedAt);
  await cancellableDelay(wait);
  if (!guard.isCurrent(generation)) return; // cancelled during the pause

  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  activeObjectUrl = url;
  activeAudio = audio;

  await new Promise<void>((resolve, reject) => {
    let settled = false;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      fn();
    };
    activeResolve = () => finish(resolve); // cancellation resolves (not an error)
    audio.onplay = () => {
      speaking = true;
      devLog("playback start", { caseId: options.caseId, progressive: false });
      devTiming("patient response → playback start (blob path)", t0);
      options.onStart?.();
    };
    audio.onended = () => {
      devLog("playback end", { caseId: options.caseId });
      releaseAudio();
      finish(resolve);
    };
    audio.onerror = () => {
      releaseAudio();
      finish(() => reject(new Error("voice_playback_failed")));
    };
    audio.play().catch((err: unknown) => {
      releaseAudio();
      finish(() => reject(err instanceof Error ? err : new Error("voice_playback_failed")));
    });
  });
  activeResolve = null;
}

async function playBrowser(options: SpeakOptions, fallbackRate: number): Promise<void> {
  devLog("tts provider", { caseId: options.caseId, provider: "browser" });
  speaking = true;
  try {
    await browserSpeak(options.text, { onStart: options.onStart }, fallbackRate);
  } finally {
    speaking = false;
  }
}

/**
 * Speak an approved patient response. Resolves only when playback has ended
 * or been cancelled. Never rejects: if every provider fails, it resolves so
 * the interview continues (the reply is already visible in the transcript).
 */
export async function speakPatientResponse(options: SpeakOptions): Promise<void> {
  const t0 = performance.now(); // ≈ patient response received (called right after)
  const generation = guard.begin();
  // Only one patient audio stream may exist: stop anything already playing.
  stopActivePlayback();

  if (!options.text.trim()) return;

  const status = await getVoiceStatus(options.caseId);
  if (!guard.isCurrent(generation)) return; // a newer request or cancel won

  if (chooseProvider(status.available) === "elevenlabs") {
    try {
      await playElevenLabs(options, generation, t0);
      return;
    } catch (err) {
      if (!guard.isCurrent(generation)) return; // cancelled mid-flight: done
      devLog("elevenlabs failed; falling back to browser TTS", err);
      releaseAudio();
    }
  }

  if (!guard.isCurrent(generation)) return;
  if (!isBrowserTtsSupported()) {
    devLog("no TTS provider available; transcript-only");
    return;
  }
  try {
    await playBrowser(options, status.fallbackRate);
  } catch (err) {
    devLog("browser TTS failed; transcript-only", err);
  }
}

/** Stop audio + abort network work WITHOUT touching the guard (internal). */
function stopActivePlayback(): void {
  activeAbortController?.abort(); // aborts the pending fetch AND the reader
  activeAbortController = null;
  const resolveActive = activeResolve; // progressive path: dispatches CANCEL
  activeResolve = null;
  resolveActive?.();
  releaseAudio();
  cancelBrowserSpeech(); // browser fallback path (safe no-op otherwise)
}

/**
 * Immediately cancel patient speech in ANY phase (before the first chunk,
 * during buffering, during playback, or during browser fallback): aborts the
 * pending synthesis request, cancels the stream reader, stops and detaches
 * the audio element / MediaSource, revokes the object URL, cancels the
 * browser fallback, and resolves the pending speak promise.
 */
export function cancelPatientSpeech(): void {
  guard.invalidateAll(); // every in-flight generation becomes stale
  devLog("tts cancelled");
  stopActivePlayback();
  // Streaming pipeline (if active): aborts the SSE request, sentence TTS
  // fetches, queued audio, and settles the streaming promises exactly once.
  cancelActiveStream();
}

/** True while patient audio (either provider) is actually playing. */
export function isPatientSpeaking(): boolean {
  return speaking;
}
