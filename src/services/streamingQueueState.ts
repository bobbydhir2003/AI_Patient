/**
 * Pure, dependency-free state for the STREAMING patient response pipeline:
 * SSE event extraction plus the ordered sentence-audio queue rules. All
 * browser side effects (fetch, Audio elements) live in patientStreamService;
 * this module encodes the safety rules so they are unit-testable in Node:
 *
 * - Sentences play strictly in order; audio clips never overlap.
 * - A sentence index is added exactly once (sentence 1 can never repeat).
 * - CANCEL is terminal: late audio/results from a cancelled turn are ignored.
 * - Three completion states are distinct and all required:
 *     generationDone  - OpenAI finished (final/error event received)
 *     audio readiness - per-sentence TTS results
 *     playback done   - every queued sentence finished (or queue cancelled)
 *   The patient is "finished speaking" only at playback done.
 * - The first ElevenLabs failure flips voiceFailed: remaining sentences are
 *   spoken by the browser-TTS fallback (in order) instead of being dropped.
 */

// ---------------------------------------------------------------------------
// SSE extraction (fetch-stream text -> events + unconsumed remainder)
// ---------------------------------------------------------------------------

export interface SseEvent {
  event: string;
  data: string;
}

/** Extract complete `event:`/`data:` blocks from an SSE text buffer. Returns
 * the parsed events and the unconsumed remainder (a partial trailing block). */
export function extractSseEvents(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = [];
  const normalized = buffer.replace(/\r\n/g, "\n");
  const lastBoundary = normalized.lastIndexOf("\n\n");
  if (lastBoundary === -1) return { events, rest: normalized };
  const complete = normalized.slice(0, lastBoundary);
  const rest = normalized.slice(lastBoundary + 2);
  for (const block of complete.split("\n\n")) {
    let event = "";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7).trim();
      else if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
      else if (line.startsWith("data:")) dataLines.push(line.slice(5));
    }
    if (event) events.push({ event, data: dataLines.join("\n") });
  }
  return { events, rest };
}

// ---------------------------------------------------------------------------
// Ordered sentence audio queue
// ---------------------------------------------------------------------------

export type SentenceAudioStatus =
  | "queued" // sentence approved; audio not requested yet
  | "fetching" // TTS request in flight
  | "ready" // audio blob buffered, waiting its turn
  | "failed" // ElevenLabs failed for this sentence (browser TTS will speak it)
  | "playing"
  | "done";

export interface StreamQueueSentence {
  index: number;
  text: string;
  status: SentenceAudioStatus;
}

export interface StreamQueueState {
  sentences: StreamQueueSentence[];
  /** OpenAI generation finished (final or error event) - NOT playback. */
  generationDone: boolean;
  cancelled: boolean;
  /** First ElevenLabs failure: the rest of the turn uses browser TTS. */
  voiceFailed: boolean;
  /** Ordering cursor: only this index may start playing. */
  nextToPlay: number;
}

export const initialStreamQueueState: StreamQueueState = {
  sentences: [],
  generationDone: false,
  cancelled: false,
  voiceFailed: false,
  nextToPlay: 0,
};

export type StreamQueueEvent =
  | { type: "ADD_SENTENCE"; index: number; text: string }
  | { type: "FETCH_STARTED"; index: number }
  | { type: "AUDIO_READY"; index: number }
  | { type: "AUDIO_FAILED"; index: number }
  | { type: "PLAY_STARTED"; index: number }
  | { type: "PLAY_ENDED"; index: number }
  | { type: "GENERATION_DONE" }
  | { type: "CANCEL" };

function withSentence(
  state: StreamQueueState,
  index: number,
  status: SentenceAudioStatus,
): StreamQueueState {
  return {
    ...state,
    sentences: state.sentences.map((s) => (s.index === index ? { ...s, status } : s)),
  };
}

export function reduceStreamQueue(
  state: StreamQueueState,
  event: StreamQueueEvent,
): StreamQueueState {
  // Cancellation is terminal: every later event (late audio results, play
  // callbacks from a detached element, stray sentences) is absorbed.
  if (state.cancelled) return state;

  switch (event.type) {
    case "ADD_SENTENCE": {
      // Exactly-once: duplicate indices (e.g. a replayed event) are ignored,
      // so sentence 1 can never be queued or spoken twice.
      if (state.sentences.some((s) => s.index === event.index)) return state;
      const added: StreamQueueSentence = {
        index: event.index,
        text: event.text,
        status: "queued",
      };
      return {
        ...state,
        sentences: [...state.sentences, added].sort((a, b) => a.index - b.index),
      };
    }
    case "FETCH_STARTED": {
      const s = state.sentences.find((x) => x.index === event.index);
      if (!s || s.status !== "queued") return state;
      return withSentence(state, event.index, "fetching");
    }
    case "AUDIO_READY": {
      const s = state.sentences.find((x) => x.index === event.index);
      if (!s || s.status !== "fetching") return state; // stale/duplicate result
      return withSentence(state, event.index, "ready");
    }
    case "AUDIO_FAILED": {
      const s = state.sentences.find((x) => x.index === event.index);
      if (!s || (s.status !== "fetching" && s.status !== "queued")) return state;
      return { ...withSentence(state, event.index, "failed"), voiceFailed: true };
    }
    case "PLAY_STARTED": {
      // Strict ordering + no overlap: only the cursor sentence may start, and
      // only if nothing else is playing.
      if (event.index !== state.nextToPlay) return state;
      if (state.sentences.some((s) => s.status === "playing")) return state;
      const s = state.sentences.find((x) => x.index === event.index);
      if (!s || s.status === "done") return state;
      return withSentence(state, event.index, "playing");
    }
    case "PLAY_ENDED": {
      const s = state.sentences.find((x) => x.index === event.index);
      if (!s || s.status !== "playing") return state; // stale 'ended'
      return {
        ...withSentence(state, event.index, "done"),
        nextToPlay: state.nextToPlay + 1,
      };
    }
    case "GENERATION_DONE":
      return { ...state, generationDone: true };
    case "CANCEL":
      return { ...state, cancelled: true };
    default:
      return state;
  }
}

/** The sentence allowed to start playing now, or null. A sentence may start
 * when it is at the cursor, nothing is playing, and its audio is either ready
 * (ElevenLabs) or destined for the browser-TTS fallback (failed/voiceFailed). */
export function nextPlayable(state: StreamQueueState): StreamQueueSentence | null {
  if (state.cancelled) return null;
  if (state.sentences.some((s) => s.status === "playing")) return null;
  const s = state.sentences.find((x) => x.index === state.nextToPlay);
  if (!s) return null;
  if (s.status === "ready" || s.status === "failed") return s;
  if (state.voiceFailed && s.status === "queued") return s; // browser TTS path
  return null;
}

/** True while any sentence audio is actually playing. */
export function isQueuePlaying(state: StreamQueueState): boolean {
  return state.sentences.some((s) => s.status === "playing");
}

/** Playback (NOT generation) is fully finished for this turn. */
export function isPlaybackComplete(state: StreamQueueState): boolean {
  if (state.cancelled) return true;
  if (!state.generationDone) return false;
  if (state.sentences.some((s) => s.status === "playing")) return false;
  return state.sentences.every((s) => s.status === "done") &&
    state.nextToPlay >= state.sentences.length;
}

/** Which sentences still need a TTS fetch (in order). */
export function pendingFetches(state: StreamQueueState): StreamQueueSentence[] {
  if (state.cancelled || state.voiceFailed) return [];
  return state.sentences.filter((s) => s.status === "queued");
}
