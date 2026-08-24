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
import { API_BASE_URL, fetchVoiceStatus, withAuthHeaders } from "./api";
import { cancelActiveStream } from "./streamRegistry";
import {
  cancelSpeech as cancelBrowserSpeech,
  isTtsSupported as isBrowserTtsSupported,
  speak as browserSpeak,
} from "./textToSpeechService";
import {
  canAppendChunk,
  canStartPlayback,
  clampPauseMs,
  createPlaybackGuard,
  initialProgressiveState,
  isTerminal,
  reduceProgressive,
  remainingPauseMs,
  shouldAttemptElevenLabs,
  type ProgressiveEvent,
  type ProgressiveState,
  type VoiceStatusResult,
} from "./voicePlaybackState";
import { describeError, logVoiceEvent, type VoiceFailureCategory } from "./voiceDiagnostics";
import type { PatientSpeechStyle } from "../types/interview";

/**
 * Stage-tagged error for the atomic voice path. Distinguishes WHERE a failure
 * happened so a single generic catch can no longer conflate "ElevenLabs
 * generation failed" with "the browser failed to play ElevenLabs' audio":
 *
 *   tts_http    - the /synthesize request itself failed (bad status, or a
 *                 200 with an empty body). Re-fetching the identical request
 *                 would just fail again the same way, so there is no point
 *                 retrying via Blob - drop straight to browser TTS.
 *   audio_decode - MediaSource/SourceBuffer append or decode failed. The
 *                 ElevenLabs audio bytes may still be perfectly valid; a full
 *                 Blob download is worth trying before the robotic voice.
 *   audio_play  - audio.play() itself was rejected (autoplay policy, aborted,
 *                 or an unknown DOM rejection). Also worth one Blob retry,
 *                 since a fresh <audio> element sometimes succeeds where a
 *                 MediaSource-backed one was blocked.
 */
class VoiceStageError extends Error {
  stage: "tts_http" | "audio_decode" | "audio_play";
  category: VoiceFailureCategory;
  constructor(stage: VoiceStageError["stage"], category: VoiceFailureCategory, message: string) {
    super(message);
    this.name = "VoiceStageError";
    this.stage = stage;
    this.category = category;
  }
}

/** A "fetch-level" ElevenLabs error means the /synthesize request itself failed
 * (bad status or empty body) — re-fetching would just fail again, so we drop to
 * browser TTS. Any OTHER error is a client-side progressive/playback failure
 * (MediaSource append/decode, sourcebuffer error, start timeout, audio.play()
 * rejection); the ElevenLabs audio may still be perfectly playable via a full
 * Blob download, so that is tried once before the robotic voice. */
function isFetchLevelVoiceError(err: unknown): boolean {
  return err instanceof VoiceStageError && err.stage === "tts_http";
}

/** Classifies an audio.play() promise rejection into a specific, reportable
 * category. NotAllowedError is a mobile/desktop autoplay-policy block;
 * AbortError is an interruption (new turn, cancel, page navigation) — not a
 * real failure; anything else is an unclassified DOM/media rejection. */
function classifyPlayRejection(err: unknown): VoiceFailureCategory {
  const name = err instanceof Error ? err.name : "";
  if (name === "NotAllowedError") return "AUDIO_PLAY_NOT_ALLOWED";
  if (name === "AbortError") return "AUDIO_PLAY_ABORTED";
  return "AUDIO_PLAY_UNKNOWN";
}

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

/** Per-case availability + fallback rate, cached for the page's lifetime.
 * Only a CONFIRMED result is ever cached (see getVoiceStatus) - a transient
 * probe failure is never cached, so the very next turn probes fresh. */
const voiceStatusCache = new Map<string, VoiceStatusResult & { fallbackRate: number }>();

function devLog(event: string, detail?: unknown): void {
  if (import.meta.env.DEV) console.debug(`[patient-voice] ${event}`, detail ?? "");
}

/** Development-only latency instrumentation (performance.now() based). */
function devTiming(label: string, sinceMs: number): void {
  if (import.meta.env.DEV) {
    console.debug(`[patient-voice][timing] ${label}: ${Math.round(performance.now() - sinceMs)} ms`);
  }
}

/**
 * Stage A - status probe. Distinguishes a DEFINITIVE backend answer
 * (confirmed: true) from a probe that failed to get an answer at all
 * (confirmed: false). GET /voice/status never contacts ElevenLabs - it is a
 * local case-config lookup behind auth - so a failure here is a statement
 * about OUR backend/network at that instant, never about ElevenLabs. It must
 * not be treated as "ElevenLabs is unavailable" (see shouldAttemptElevenLabs
 * in voicePlaybackState.ts, which is what actually makes that decision).
 */
async function getVoiceStatus(
  caseId: string,
): Promise<VoiceStatusResult & { fallbackRate: number }> {
  const cached = voiceStatusCache.get(caseId);
  if (cached) return cached;
  try {
    const status = await fetchVoiceStatus(caseId);
    const entry = {
      available: status.available === true,
      fallbackRate: typeof status.fallbackRate === "number" ? status.fallbackRate : 0.97,
      confirmed: true as const,
    };
    voiceStatusCache.set(caseId, entry);
    logVoiceEvent(
      entry.available ? "voice_status_ok" : "voice_status_confirmed_unavailable",
      { caseId, category: entry.available ? undefined : "STATUS_CONFIRMED_UNAVAILABLE" },
    );
    devLog("voice status", { caseId, provider: status.provider });
    return entry;
  } catch (err) {
    logVoiceEvent("voice_status_failed", { caseId, category: "STATUS_PROBE_TRANSIENT", ...describeError(err) });
    devLog("voice status probe failed (transient); ElevenLabs will still be attempted", err);
    // NOT cached: this is not a real answer, so the next call must probe
    // again rather than remembering a false "unavailable". `available` is a
    // meaningless placeholder here - callers MUST check `confirmed` (via
    // shouldAttemptElevenLabs) rather than reading `available` directly.
    return { available: false, fallbackRate: 0.97, confirmed: false };
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
  logVoiceEvent("tts_request_started", { caseId: options.caseId, path: "progressive" });

  const response = await fetch(`${API_BASE_URL}/api/voice/synthesize`, {
    method: "POST",
    headers: withAuthHeaders({ "Content-Type": "application/json" }),
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
    logVoiceEvent("tts_http_failed", { caseId: options.caseId, status: response.status, category: "TTS_HTTP_ERROR" });
    throw new VoiceStageError("tts_http", "TTS_HTTP_ERROR", `voice_synthesis_http_${response.status}`);
  }
  logVoiceEvent("tts_http_success", { caseId: options.caseId, status: response.status });
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
 * Re-request the same approved synthesis and play it as a fully-downloaded Blob.
 * Used ONLY as a recovery step when progressive (MediaSource) playback failed
 * but the ElevenLabs audio itself is fine — this keeps the realistic voice
 * instead of dropping straight to the robotic browser voice. A completed clip is
 * usually served from the backend audio cache, so this is typically cheap.
 */
async function playElevenLabsBuffered(options: SpeakOptions, generation: number, t0: number): Promise<void> {
  const abort = new AbortController();
  activeAbortController = abort;
  const requestStartedAt = performance.now();
  logVoiceEvent("tts_request_started", { caseId: options.caseId, path: "blob_recovery" });
  const response = await fetch(`${API_BASE_URL}/api/voice/synthesize`, {
    method: "POST",
    headers: withAuthHeaders({ "Content-Type": "application/json" }),
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
    logVoiceEvent("tts_http_failed", {
      caseId: options.caseId, status: response.status, path: "blob_recovery", category: "TTS_HTTP_ERROR",
    });
    throw new VoiceStageError("tts_http", "TTS_HTTP_ERROR", `voice_synthesis_http_${response.status}`);
  }
  logVoiceEvent("tts_http_success", { caseId: options.caseId, status: response.status, path: "blob_recovery" });
  const pauseBeforeMs = clampPauseMs(
    response.headers.get("X-Pause-Before-Ms") ?? options.speechStyle?.pauseBeforeMs ?? 150,
  );
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
      logVoiceEvent("tts_progressive_start_timeout", {
        caseId: options.caseId, path: "progressive", category: "TTS_TIMEOUT",
      });
      // Treated as a playback-stage failure (not a generation failure): the
      // request succeeded and chunks were arriving, they just never turned
      // into 'playing' - worth a Blob retry before the robotic voice.
      fail(new VoiceStageError("audio_play", "TTS_TIMEOUT", "voice_progressive_start_timeout"));
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
        logVoiceEvent("audio_decode_failed", {
          caseId: options.caseId, path: "progressive", reason: "append", category: "AUDIO_DECODE_ERROR",
          ...describeError(err),
        });
        fail(new VoiceStageError("audio_decode", "AUDIO_DECODE_ERROR", "voice_decode_failed"));
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
        audio.play().catch((err: unknown) => {
          const category = classifyPlayRejection(err);
          logVoiceEvent("audio_play_failed", {
            caseId: options.caseId, path: "progressive", category, ...describeError(err),
          });
          fail(new VoiceStageError("audio_play", category, "voice_play_rejected"));
        });
      });
    };

    audio.onplay = () => {
      clearWatchdog(); // playback actually began
      speaking = true;
      devLog("playback start", { caseId: options.caseId, progressive: true });
      logVoiceEvent("audio_play_started", { caseId: options.caseId, path: "progressive" });
      options.onStart?.();
    };
    audio.onended = () => {
      if (dispatch({ type: "PLAYBACK_ENDED" }).phase !== "ended") return; // stale event
      devLog("playback end", { caseId: options.caseId });
      devTiming("patient response → playback ended", t0);
      logVoiceEvent("audio_play_success", { caseId: options.caseId, path: "progressive" });
      releaseAudio();
      finish(resolve);
    };
    audio.onerror = () => {
      // The <audio> element itself reported an error (distinct from a
      // play()-promise rejection): a decode failure on already-appended data.
      logVoiceEvent("audio_decode_failed", {
        caseId: options.caseId, path: "progressive", reason: "media_error", category: "AUDIO_DECODE_ERROR",
        ...describeError(audio.error),
      });
      fail(new VoiceStageError("audio_decode", "AUDIO_DECODE_ERROR", "voice_playback_failed"));
    };

    mediaSource.addEventListener(
      "sourceopen",
      () => {
        if (settled || !guard.isCurrent(generation)) return;
        try {
          sourceBuffer = mediaSource.addSourceBuffer("audio/mpeg");
        } catch (err) {
          logVoiceEvent("audio_decode_failed", {
            caseId: options.caseId, path: "progressive", reason: "add_source_buffer", category: "AUDIO_DECODE_ERROR",
            ...describeError(err),
          });
          fail(new VoiceStageError("audio_decode", "AUDIO_DECODE_ERROR", "voice_add_source_buffer_failed"));
          return;
        }
        sourceBuffer.addEventListener("updateend", () => {
          maybeStartPlayback(); // first chunk buffered → playback may begin
          appendNext();
          maybeEndOfStream();
        });
        sourceBuffer.addEventListener("error", () => {
          logVoiceEvent("audio_decode_failed", {
            caseId: options.caseId, path: "progressive", reason: "sourcebuffer_event", category: "AUDIO_DECODE_ERROR",
          });
          fail(new VoiceStageError("audio_decode", "AUDIO_DECODE_ERROR", "voice_sourcebuffer_error"));
        });
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
  if (blob.size === 0) {
    logVoiceEvent("tts_empty_audio", { caseId: options.caseId, path: "blob", category: "TTS_EMPTY_AUDIO" });
    throw new VoiceStageError("tts_http", "TTS_EMPTY_AUDIO", "voice_synthesis_empty_audio");
  }
  logVoiceEvent("audio_blob_ready", { caseId: options.caseId, path: "blob" });
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
      logVoiceEvent("audio_play_started", { caseId: options.caseId, path: "blob" });
      options.onStart?.();
    };
    audio.onended = () => {
      devLog("playback end", { caseId: options.caseId });
      logVoiceEvent("audio_play_success", { caseId: options.caseId, path: "blob" });
      releaseAudio();
      finish(resolve);
    };
    audio.onerror = () => {
      logVoiceEvent("audio_decode_failed", {
        caseId: options.caseId, path: "blob", reason: "media_error", category: "AUDIO_DECODE_ERROR",
        ...describeError(audio.error),
      });
      releaseAudio();
      finish(() => reject(new VoiceStageError("audio_decode", "AUDIO_DECODE_ERROR", "voice_playback_failed")));
    };
    audio.play().catch((err: unknown) => {
      const category = classifyPlayRejection(err);
      logVoiceEvent("audio_play_failed", {
        caseId: options.caseId, path: "blob", category, ...describeError(err),
      });
      releaseAudio();
      finish(() => reject(new VoiceStageError("audio_play", category, "voice_play_rejected")));
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

  // Bug-2 fix: a TRANSIENT status-probe failure (status.confirmed === false)
  // must not be treated as a confirmed "no voice for this case". ElevenLabs
  // is still attempted; only a real generation/playback failure below sends
  // this turn to the browser voice. See shouldAttemptElevenLabs's docstring.
  let browserFallbackReason: "status_confirmed_unavailable" | "elevenlabs_failed" | null = null;
  if (shouldAttemptElevenLabs(status)) {
    try {
      await playElevenLabs(options, generation, t0);
      logVoiceEvent("tts_succeeded", { caseId: options.caseId, path: "progressive" });
      return;
    } catch (err) {
      if (!guard.isCurrent(generation)) return; // cancelled mid-flight: done
      releaseAudio();
      // Recovery order (mobile-friendly): if PLAYBACK (progressive MediaSource
      // decode, or an audio.play() rejection) failed but the ElevenLabs
      // request itself was fine, try a standard Blob download of the same
      // audio BEFORE dropping to the robotic browser voice. A confirmed
      // tts_http-stage failure skips straight to browser TTS - re-fetching
      // the identical request would just fail the same way again.
      if (canUseMediaSource() && !isFetchLevelVoiceError(err)) {
        try {
          await playElevenLabsBuffered(options, generation, t0);
          logVoiceEvent("tts_succeeded", { caseId: options.caseId, path: "blob_recovery" });
          return; // ElevenLabs voice preserved via Blob playback
        } catch (blobErr) {
          if (!guard.isCurrent(generation)) return;
          releaseAudio();
          devLog("blob recovery also failed; falling back to browser TTS", blobErr);
        }
      }
      browserFallbackReason = "elevenlabs_failed";
      devLog("elevenlabs failed; falling back to browser TTS", err);
    }
  } else {
    browserFallbackReason = "status_confirmed_unavailable";
  }

  if (!guard.isCurrent(generation)) return;
  if (!isBrowserTtsSupported()) {
    devLog("no TTS provider available; transcript-only");
    return;
  }
  try {
    logVoiceEvent("browser_fallback_started", {
      caseId: options.caseId, reason: browserFallbackReason ?? "elevenlabs_failed", category: "BROWSER_FALLBACK",
    });
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
  logVoiceEvent("tts_cancelled", {});
  stopActivePlayback();
  // Streaming pipeline (if active): aborts the SSE request, sentence TTS
  // fetches, queued audio, and settles the streaming promises exactly once.
  cancelActiveStream();
}

/** True while patient audio (either provider) is actually playing. */
export function isPatientSpeaking(): boolean {
  return speaking;
}
