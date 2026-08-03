/**
 * Streaming patient exchange orchestrator (feature-flagged low-latency path).
 *
 * One SSE request to FastAPI per patient turn:
 *   sentence events -> transcript grows + ordered per-sentence TTS queue
 *   final event     -> ONE authoritative committed patient turn
 *   error event     -> nothing was spoken; caller falls back to the stable
 *                      non-streaming exchange with the SAME clientTurnId.
 *
 * Audio: each approved sentence is synthesized through the existing
 * /api/voice/synthesize endpoint (backend keeps the ElevenLabs key and its
 * keep-alive connection pool). Sentences play strictly in order, never
 * overlapping; the first ElevenLabs failure fails the turn over to the
 * browser-TTS fallback for the remaining sentences.
 *
 * Cancellation (student interruption): cancel() aborts the SSE fetch (the
 * backend then commits exactly the sentences already emitted), aborts any TTS
 * fetch, stops current audio, clears the queue, and settles every promise
 * exactly once. Late events from a cancelled turn are absorbed by the pure
 * queue reducer (CANCEL is terminal).
 */
import { API_BASE_URL, fetchVoiceStatus, withAuthHeaders } from "./api";
import {
  cancelSpeech as cancelBrowserSpeech,
  isTtsSupported as isBrowserTtsSupported,
  speak as browserSpeak,
} from "./textToSpeechService";
import { clampPauseMs, remainingPauseMs } from "./voicePlaybackState";
import {
  extractSseEvents,
  initialStreamQueueState,
  isPlaybackComplete,
  nextPlayable,
  pendingFetches,
  reduceStreamQueue,
  type StreamQueueEvent,
  type StreamQueueState,
} from "./streamingQueueState";
import { cancelActiveStream, registerActiveStreamCancel } from "./streamRegistry";
import type { PatientSpeechStyle } from "../types/interview";

export class StreamStartFailedError extends Error {
  code: string;
  constructor(code: string, message?: string) {
    super(message ?? `streaming_failed:${code}`);
    this.name = "StreamStartFailedError";
    this.code = code;
  }
}

export class StreamCancelledError extends Error {
  constructor() {
    super("streaming_cancelled");
    this.name = "StreamCancelledError";
  }
}

export interface StreamTurnFinal {
  turnId: string;
  patientText: string;
  status: string; // "completed" | "interrupted"
  speech: PatientSpeechStyle | null;
}

export interface StartStreamingOptions {
  sessionId: string;
  caseId: string;
  text: string;
  clientTurnId: string;
  source: "typed" | "speech";
  /** Speak sentences aloud as they are approved (voice toggle + TTS support). */
  speakAloud: boolean;
  /** Transcript growth: called once per approved sentence, in order. */
  onSentence?: (index: number, text: string) => void;
  /** The single authoritative turn (normal completion or committed partial). */
  onFinal?: (final: StreamTurnFinal) => void;
}

export interface StreamingExchangeHandle {
  clientTurnId: string;
  /** Resolves at the FIRST approved sentence; rejects with
   * StreamStartFailedError when the stream failed before any speech (safe to
   * fall back), or StreamCancelledError on cancellation. */
  firstSentence: Promise<{ text: string; speech: PatientSpeechStyle | null }>;
  /** Resolves when the final turn is known (null if cancelled before it). */
  completion: Promise<StreamTurnFinal | null>;
  /** Resolves when ALL patient audio for this turn ended or was cancelled.
   * Distinct from generation completion - see streamingQueueState. */
  playbackDone: Promise<void>;
  /** Fires when the first audio actually starts (arms barge-in VAD). If
   * playback already started when set, fires immediately. */
  setOnPlaybackStart(cb: (() => void) | null): void;
  cancel(): void;
  isCancelled(): boolean;
}

function devLog(event: string, detail?: unknown): void {
  if (import.meta.env.DEV) console.debug(`[patient-stream] ${event}`, detail ?? "");
}

function devTiming(label: string, sinceMs: number, turn: string): void {
  if (import.meta.env.DEV) {
    console.debug(
      `[patient-stream][timing] ${label}: ${Math.round(performance.now() - sinceMs)} ms (turn=${turn})`,
    );
  }
}

/** Per-case voice availability cache (mirrors patientVoiceService's cache but
 * kept local to avoid a circular import). */
const voiceStatusCache = new Map<string, { available: boolean; fallbackRate: number }>();

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
    return entry;
  } catch {
    return { available: false, fallbackRate: 0.97 }; // not cached: may recover
  }
}

export function startStreamingExchange(options: StartStreamingOptions): StreamingExchangeHandle {
  // Only one patient may ever speak: a stale active stream (should not exist,
  // but defensively) is cancelled before this turn begins.
  cancelActiveStream();
  const t0 = performance.now();
  const turn = options.clientTurnId;

  let state: StreamQueueState = initialStreamQueueState;
  const dispatch = (event: StreamQueueEvent): StreamQueueState => {
    state = reduceStreamQueue(state, event);
    return state;
  };

  let cancelled = false;
  let earlySpeech: PatientSpeechStyle | null = null;
  let finalTurn: StreamTurnFinal | null = null;
  const sentenceTexts: string[] = [];
  const audioBlobs = new Map<number, Blob>();
  let pauseBeforeMs = 150;
  let firstAudioStarted = false;
  let onPlaybackStart: (() => void) | null = null;

  const sseAbort = new AbortController();
  let ttsAbort: AbortController | null = null;
  let activeAudio: HTMLAudioElement | null = null;
  let activeObjectUrl: string | null = null;
  let fetchPumpRunning = false;
  let playPumpRunning = false;
  let browserRate = 0.97;

  // ---- exactly-once promise plumbing ----
  let resolveFirst!: (v: { text: string; speech: PatientSpeechStyle | null }) => void;
  let rejectFirst!: (err: Error) => void;
  let firstSettled = false;
  const firstSentence = new Promise<{ text: string; speech: PatientSpeechStyle | null }>(
    (res, rej) => {
      resolveFirst = res;
      rejectFirst = rej;
    },
  );
  firstSentence.catch(() => undefined); // avoid unhandled rejection when unused

  let resolveCompletion!: (v: StreamTurnFinal | null) => void;
  let completionSettled = false;
  const completion = new Promise<StreamTurnFinal | null>((res) => {
    resolveCompletion = res;
  });

  let resolvePlayback!: () => void;
  let playbackSettled = false;
  const playbackDone = new Promise<void>((res) => {
    resolvePlayback = res;
  });

  const settleFirst = (err: Error | null, value?: { text: string; speech: PatientSpeechStyle | null }) => {
    if (firstSettled) return;
    firstSettled = true;
    if (err) rejectFirst(err);
    else resolveFirst(value!);
  };
  const settleCompletion = (value: StreamTurnFinal | null) => {
    if (completionSettled) return;
    completionSettled = true;
    resolveCompletion(value);
  };
  const settlePlayback = () => {
    if (playbackSettled) return;
    playbackSettled = true;
    resolvePlayback();
  };

  const maybeFinishPlayback = () => {
    if (!options.speakAloud) {
      if (state.generationDone || state.cancelled) settlePlayback();
      return;
    }
    if (isPlaybackComplete(state)) settlePlayback();
  };

  // ------------------------------------------------------------------
  // Audio: sentence TTS prefetch pump (order-preserving, one at a time)
  // ------------------------------------------------------------------
  async function fetchPump(): Promise<void> {
    if (fetchPumpRunning || cancelled || !options.speakAloud) return;
    fetchPumpRunning = true;
    try {
      for (;;) {
        const pending = pendingFetches(state);
        const next = pending[0];
        if (!next) break;
        dispatch({ type: "FETCH_STARTED", index: next.index });
        const controller = new AbortController();
        ttsAbort = controller;
        try {
          if (next.index === 0) devTiming("first sentence -> TTS request start", t0, turn);
          const response = await fetch(`${API_BASE_URL}/api/voice/synthesize`, {
            method: "POST",
            headers: withAuthHeaders({ "Content-Type": "application/json" }),
            signal: controller.signal,
            body: JSON.stringify({
              caseId: options.caseId,
              text: next.text,
              // Live sentence streaming: the turn is not committed yet, so there
              // is no turnId. The backend authorizes by session ownership and
              // caps the length (see voice.py A5 mode 2).
              sessionId: options.sessionId,
              turnId: "",
              speechStyle: earlySpeech,
              correlationId: `${turn}:s${next.index}`,
            }),
          });
          if (!response.ok) throw new Error(`voice_synthesis_http_${response.status}`);
          if (next.index === 0) {
            pauseBeforeMs = clampPauseMs(
              response.headers.get("X-Pause-Before-Ms") ?? earlySpeech?.pauseBeforeMs ?? 150,
            );
            devTiming("first sentence -> TTS response headers", t0, turn);
          }
          const blob = await response.blob();
          if (cancelled) return;
          if (blob.size === 0) throw new Error("voice_synthesis_empty_audio");
          audioBlobs.set(next.index, blob);
          dispatch({ type: "AUDIO_READY", index: next.index });
          if (next.index === 0) devTiming("first sentence -> audio buffered", t0, turn);
        } catch (err) {
          if (cancelled) return;
          devLog("sentence TTS failed; browser fallback for the rest", {
            index: next.index,
            err,
          });
          dispatch({ type: "AUDIO_FAILED", index: next.index });
        } finally {
          if (ttsAbort === controller) ttsAbort = null;
        }
        void playPump();
      }
    } finally {
      fetchPumpRunning = false;
    }
    void playPump();
  }

  // ------------------------------------------------------------------
  // Audio: strictly ordered playback pump (no overlap, exactly-once)
  // ------------------------------------------------------------------
  async function playPump(): Promise<void> {
    if (playPumpRunning || cancelled || !options.speakAloud) return;
    playPumpRunning = true;
    try {
      for (;;) {
        const sentence = nextPlayable(state);
        if (!sentence) break;
        const useBrowser =
          state.voiceFailed || sentence.status === "failed" || !audioBlobs.has(sentence.index);
        dispatch({ type: "PLAY_STARTED", index: sentence.index });
        if (state.cancelled) return;

        if (sentence.index === 0) {
          // Natural pre-speech pause, overlapped with the network time already
          // spent since the exchange started (same rule as the stable path).
          const wait = remainingPauseMs(pauseBeforeMs, performance.now() - t0);
          if (wait > 0) await new Promise((r) => window.setTimeout(r, wait));
          if (cancelled) return;
        }

        const markStarted = () => {
          if (!firstAudioStarted) {
            firstAudioStarted = true;
            devTiming("question submitted -> first audible patient word", t0, turn);
            onPlaybackStart?.();
          }
        };

        if (useBrowser && isBrowserTtsSupported()) {
          markStarted();
          try {
            await browserSpeak(sentence.text, {}, browserRate);
          } catch {
            // browser TTS failure: transcript already shows the text
          }
        } else if (!useBrowser) {
          const blob = audioBlobs.get(sentence.index)!;
          audioBlobs.delete(sentence.index);
          await new Promise<void>((resolve) => {
            const url = URL.createObjectURL(blob);
            const audio = new Audio(url);
            activeAudio = audio;
            activeObjectUrl = url;
            let settled = false;
            const finish = () => {
              if (settled) return;
              settled = true;
              if (activeAudio === audio) activeAudio = null;
              if (activeObjectUrl === url) activeObjectUrl = null;
              URL.revokeObjectURL(url);
              resolve();
            };
            audio.onplay = markStarted;
            audio.onended = finish;
            audio.onerror = finish;
            audio.play().catch(() => {
              // Autoplay rejection / decode failure: try the browser voice so
              // the patient still speaks this sentence.
              if (!cancelled && isBrowserTtsSupported()) {
                markStarted();
                void browserSpeak(sentence.text, {}, browserRate).finally(finish);
              } else {
                finish();
              }
            });
          });
        }
        // else: no audio provider at all -> transcript-only, mark as done.

        if (cancelled) return;
        dispatch({ type: "PLAY_ENDED", index: sentence.index });
        maybeFinishPlayback();
      }
    } finally {
      playPumpRunning = false;
    }
    maybeFinishPlayback();
  }

  // ------------------------------------------------------------------
  // Cancellation (interruption / teardown): stop EVERYTHING exactly once.
  // ------------------------------------------------------------------
  const cancel = () => {
    if (cancelled) return;
    cancelled = true;
    devLog("stream cancelled", { turn });
    dispatch({ type: "CANCEL" });
    sseAbort.abort(); // backend commits exactly the sentences already emitted
    ttsAbort?.abort();
    ttsAbort = null;
    if (activeAudio) {
      activeAudio.onplay = null;
      activeAudio.onended = null;
      activeAudio.onerror = null;
      activeAudio.pause();
      activeAudio.removeAttribute("src");
      activeAudio.load();
      activeAudio = null;
    }
    if (activeObjectUrl) {
      URL.revokeObjectURL(activeObjectUrl);
      activeObjectUrl = null;
    }
    audioBlobs.clear();
    cancelBrowserSpeech();
    settleFirst(new StreamCancelledError());
    settleCompletion(finalTurn);
    settlePlayback();
    registerActiveStreamCancel(null);
  };

  // ------------------------------------------------------------------
  // SSE consumption
  // ------------------------------------------------------------------
  async function consume(): Promise<void> {
    let response: Response;
    try {
      response = await fetch(
        `${API_BASE_URL}/api/interviews/${encodeURIComponent(options.sessionId)}/messages/stream`,
        {
          method: "POST",
          headers: withAuthHeaders({ "Content-Type": "application/json" }),
          signal: sseAbort.signal,
          body: JSON.stringify({
            text: options.text,
            caseId: options.caseId,
            clientTurnId: options.clientTurnId,
            source: options.source,
          }),
        },
      );
    } catch (err) {
      if (cancelled) return;
      throw new StreamStartFailedError("network_error", String(err));
    }
    if (!response.ok || !response.body) {
      let code = `http_${response.status}`;
      try {
        const body = (await response.json()) as { error?: { code?: string } };
        if (body.error?.code) code = body.error.code;
      } catch {
        // keep the status-based code
      }
      throw new StreamStartFailedError(code);
    }
    devTiming("question submitted -> stream response started", t0, turn);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let sawFirstEvent = false;

    for (;;) {
      const { done, value } = await reader.read();
      if (cancelled) return;
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { events, rest } = extractSseEvents(buffer);
      buffer = rest;
      for (const { event, data } of events) {
        if (!sawFirstEvent) {
          sawFirstEvent = true;
          devTiming("question submitted -> first stream event", t0, turn);
        }
        let parsed: Record<string, unknown> = {};
        try {
          parsed = JSON.parse(data) as Record<string, unknown>;
        } catch {
          continue; // malformed event: ignore rather than break the turn
        }
        if (event === "speech") {
          earlySpeech = parsed as PatientSpeechStyle;
        } else if (event === "sentence") {
          const index = Number(parsed.index);
          const text = String(parsed.text ?? "");
          if (!Number.isInteger(index) || !text) continue;
          if (index === 0) devTiming("question submitted -> first approved sentence", t0, turn);
          dispatch({ type: "ADD_SENTENCE", index, text });
          sentenceTexts[index] = text;
          options.onSentence?.(index, text);
          settleFirst(null, { text, speech: earlySpeech });
          void fetchPump();
          void playPump();
        } else if (event === "final") {
          finalTurn = {
            turnId: String(parsed.turnId ?? ""),
            patientText: String(parsed.patientText ?? ""),
            status: String(parsed.status ?? "completed"),
            speech: (parsed.speech as PatientSpeechStyle | null) ?? null,
          };
          devTiming("question submitted -> generation complete (final event)", t0, turn);
        } else if (event === "error") {
          const code = String(parsed.code ?? "stream_error");
          throw new StreamStartFailedError(code);
        }
      }
    }

    // Stream ended.
    if (finalTurn) {
      // Idempotent replays send ONLY a final event (no sentences): surface the
      // text through the sentence callback so the transcript still renders it.
      if (sentenceTexts.length === 0 && finalTurn.patientText) {
        dispatch({ type: "ADD_SENTENCE", index: 0, text: finalTurn.patientText });
        sentenceTexts[0] = finalTurn.patientText;
        options.onSentence?.(0, finalTurn.patientText);
        settleFirst(null, { text: finalTurn.patientText, speech: earlySpeech });
        void fetchPump();
        void playPump();
      }
      dispatch({ type: "GENERATION_DONE" });
      options.onFinal?.(finalTurn);
      settleCompletion(finalTurn);
      maybeFinishPlayback();
      void playPump();
      return;
    }
    if (sentenceTexts.length > 0) {
      // Connection ended without a final event (e.g. backend restart). The
      // backend committed the emitted sentences; mirror that here.
      const partial: StreamTurnFinal = {
        turnId: "",
        patientText: sentenceTexts.filter(Boolean).join(" ").trim(),
        status: "interrupted",
        speech: earlySpeech,
      };
      finalTurn = partial;
      dispatch({ type: "GENERATION_DONE" });
      options.onFinal?.(partial);
      settleCompletion(partial);
      maybeFinishPlayback();
      void playPump();
      return;
    }
    throw new StreamStartFailedError("stream_ended_without_content");
  }

  // Voice status: resolve availability up front so the queue knows whether to
  // fetch ElevenLabs audio or use per-sentence browser TTS from the start.
  const begin = async () => {
    if (options.speakAloud) {
      const status = await getVoiceStatus(options.caseId);
      browserRate = status.fallbackRate;
      if (!status.available && !cancelled) {
        state = { ...state, voiceFailed: true }; // browser TTS per sentence
      }
    }
    await consume();
  };

  registerActiveStreamCancel(cancel);

  void begin().catch((err: unknown) => {
    if (cancelled) return;
    devLog("stream failed", err);
    // Zero sentences spoken -> reject so the caller falls back to the stable
    // path. If sentences WERE spoken, keep them: settle with the partial turn.
    if (sentenceTexts.length === 0) {
      const failure =
        err instanceof StreamStartFailedError
          ? err
          : new StreamStartFailedError("stream_error", String(err));
      dispatch({ type: "GENERATION_DONE" });
      settleFirst(failure);
      settleCompletion(null);
      settlePlayback();
      registerActiveStreamCancel(null);
      return;
    }
    const partial: StreamTurnFinal = {
      turnId: "",
      patientText: sentenceTexts.filter(Boolean).join(" ").trim(),
      status: "interrupted",
      speech: earlySpeech,
    };
    finalTurn = partial;
    dispatch({ type: "GENERATION_DONE" });
    options.onFinal?.(partial);
    settleCompletion(partial);
    maybeFinishPlayback();
    void playPump();
  });

  return {
    clientTurnId: options.clientTurnId,
    firstSentence,
    completion,
    playbackDone,
    setOnPlaybackStart(cb) {
      onPlaybackStart = cb;
      if (cb && firstAudioStarted) cb();
    },
    cancel,
    isCancelled: () => cancelled,
  };
}
