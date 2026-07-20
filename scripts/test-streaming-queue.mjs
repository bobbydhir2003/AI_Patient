/**
 * Unit tests for the pure streaming queue state (streamingQueueState.ts):
 * SSE extraction, strict sentence ordering, no-overlap, exactly-once
 * emission, cancellation absorption, and the three distinct completion
 * states (generation done vs TTS ready vs playback complete).
 * Run via: npm run test:voice
 */
import assert from "node:assert/strict";
import test from "node:test";
import {
  extractSseEvents,
  initialStreamQueueState,
  isPlaybackComplete,
  isQueuePlaying,
  nextPlayable,
  pendingFetches,
  reduceStreamQueue,
} from "../.test-build/services/streamingQueueState.js";

function run(state, ...events) {
  return events.reduce(reduceStreamQueue, state);
}

// ---------------------------------------------------------------------------
// SSE extraction
// ---------------------------------------------------------------------------

test("extractSseEvents parses complete blocks and keeps the partial rest", () => {
  const chunk =
    'event: speech\ndata: {"emotion":"warm"}\n\n' +
    'event: sentence\ndata: {"index":0,"text":"Hi."}\n\n' +
    "event: sen"; // partial block still streaming
  const { events, rest } = extractSseEvents(chunk);
  assert.equal(events.length, 2);
  assert.deepEqual(events[0], { event: "speech", data: '{"emotion":"warm"}' });
  assert.deepEqual(events[1], { event: "sentence", data: '{"index":0,"text":"Hi."}' });
  assert.equal(rest, "event: sen");
});

test("extractSseEvents handles CRLF and empty buffers", () => {
  assert.deepEqual(extractSseEvents(""), { events: [], rest: "" });
  const { events } = extractSseEvents("event: final\r\ndata: {}\r\n\r\n");
  assert.equal(events.length, 1);
  assert.equal(events[0].event, "final");
});

// ---------------------------------------------------------------------------
// Ordering and exactly-once
// ---------------------------------------------------------------------------

test("sentences play strictly in order", () => {
  let s = run(
    initialStreamQueueState,
    { type: "ADD_SENTENCE", index: 0, text: "One." },
    { type: "ADD_SENTENCE", index: 1, text: "Two." },
    { type: "FETCH_STARTED", index: 0 },
    { type: "FETCH_STARTED", index: 1 },
    { type: "AUDIO_READY", index: 1 }, // sentence 2 ready FIRST
  );
  // Sentence 2 must NOT play before sentence 1.
  assert.equal(nextPlayable(s), null);
  s = run(s, { type: "AUDIO_READY", index: 0 });
  assert.equal(nextPlayable(s).index, 0);
});

test("sentence audio never overlaps", () => {
  let s = run(
    initialStreamQueueState,
    { type: "ADD_SENTENCE", index: 0, text: "One." },
    { type: "ADD_SENTENCE", index: 1, text: "Two." },
    { type: "FETCH_STARTED", index: 0 },
    { type: "AUDIO_READY", index: 0 },
    { type: "FETCH_STARTED", index: 1 },
    { type: "AUDIO_READY", index: 1 },
    { type: "PLAY_STARTED", index: 0 },
  );
  assert.equal(isQueuePlaying(s), true);
  // While 0 plays, 1 may not start (nextPlayable is null; PLAY_STARTED noop).
  assert.equal(nextPlayable(s), null);
  const before = s;
  s = run(s, { type: "PLAY_STARTED", index: 1 });
  assert.deepEqual(s, before);
  s = run(s, { type: "PLAY_ENDED", index: 0 });
  assert.equal(nextPlayable(s).index, 1);
});

test("sentence 1 is never queued twice (duplicate ADD ignored)", () => {
  const s = run(
    initialStreamQueueState,
    { type: "ADD_SENTENCE", index: 0, text: "One." },
    { type: "ADD_SENTENCE", index: 0, text: "One again." },
  );
  assert.equal(s.sentences.length, 1);
  assert.equal(s.sentences[0].text, "One.");
});

test("a sentence that already played cannot restart", () => {
  let s = run(
    initialStreamQueueState,
    { type: "ADD_SENTENCE", index: 0, text: "One." },
    { type: "FETCH_STARTED", index: 0 },
    { type: "AUDIO_READY", index: 0 },
    { type: "PLAY_STARTED", index: 0 },
    { type: "PLAY_ENDED", index: 0 },
  );
  const before = s;
  s = run(s, { type: "PLAY_STARTED", index: 0 });
  assert.deepEqual(s, before); // cursor moved on; replay is impossible
});

// ---------------------------------------------------------------------------
// Cancellation
// ---------------------------------------------------------------------------

test("CANCEL is terminal: late events from the old turn are absorbed", () => {
  let s = run(
    initialStreamQueueState,
    { type: "ADD_SENTENCE", index: 0, text: "One." },
    { type: "FETCH_STARTED", index: 0 },
    { type: "CANCEL" },
  );
  const cancelled = s;
  s = run(
    s,
    { type: "AUDIO_READY", index: 0 }, // late TTS result
    { type: "ADD_SENTENCE", index: 1, text: "Two." }, // late sentence
    { type: "PLAY_STARTED", index: 0 },
    { type: "GENERATION_DONE" },
  );
  assert.deepEqual(s, cancelled);
  assert.equal(nextPlayable(s), null);
  assert.equal(pendingFetches(s).length, 0);
  assert.equal(isPlaybackComplete(s), true); // cancelled counts as finished
});

test("queued sentence 2 never plays after interruption during sentence 1", () => {
  let s = run(
    initialStreamQueueState,
    { type: "ADD_SENTENCE", index: 0, text: "One." },
    { type: "ADD_SENTENCE", index: 1, text: "Two." },
    { type: "FETCH_STARTED", index: 0 },
    { type: "AUDIO_READY", index: 0 },
    { type: "FETCH_STARTED", index: 1 },
    { type: "AUDIO_READY", index: 1 },
    { type: "PLAY_STARTED", index: 0 },
    { type: "CANCEL" }, // interrupt during sentence 1
    { type: "PLAY_ENDED", index: 0 }, // element 'ended' arrives late
  );
  assert.equal(nextPlayable(s), null); // sentence 2 must never start
});

// ---------------------------------------------------------------------------
// Completion states are distinct
// ---------------------------------------------------------------------------

test("generation-complete does NOT mean playback-complete", () => {
  let s = run(
    initialStreamQueueState,
    { type: "ADD_SENTENCE", index: 0, text: "One." },
    { type: "GENERATION_DONE" },
  );
  assert.equal(s.generationDone, true);
  assert.equal(isPlaybackComplete(s), false); // audio hasn't even been fetched
  s = run(
    s,
    { type: "FETCH_STARTED", index: 0 },
    { type: "AUDIO_READY", index: 0 }, // TTS complete...
  );
  assert.equal(isPlaybackComplete(s), false); // ...but nothing played yet
  s = run(s, { type: "PLAY_STARTED", index: 0 });
  assert.equal(isPlaybackComplete(s), false); // still audibly speaking
  s = run(s, { type: "PLAY_ENDED", index: 0 });
  assert.equal(isPlaybackComplete(s), true);
});

test("playback is not complete while later sentences are still generating", () => {
  let s = run(
    initialStreamQueueState,
    { type: "ADD_SENTENCE", index: 0, text: "One." },
    { type: "FETCH_STARTED", index: 0 },
    { type: "AUDIO_READY", index: 0 },
    { type: "PLAY_STARTED", index: 0 },
    { type: "PLAY_ENDED", index: 0 },
  );
  assert.equal(isPlaybackComplete(s), false); // generation still running
  s = run(s, { type: "GENERATION_DONE" });
  assert.equal(isPlaybackComplete(s), true);
});

// ---------------------------------------------------------------------------
// ElevenLabs failure -> browser fallback ordering
// ---------------------------------------------------------------------------

test("first TTS failure flips voiceFailed and stops further fetches", () => {
  let s = run(
    initialStreamQueueState,
    { type: "ADD_SENTENCE", index: 0, text: "One." },
    { type: "ADD_SENTENCE", index: 1, text: "Two." },
    { type: "FETCH_STARTED", index: 0 },
    { type: "AUDIO_FAILED", index: 0 },
  );
  assert.equal(s.voiceFailed, true);
  assert.equal(pendingFetches(s).length, 0); // no more ElevenLabs requests
  // The failed sentence still plays (browser TTS), in order.
  assert.equal(nextPlayable(s).index, 0);
  s = run(s, { type: "PLAY_STARTED", index: 0 }, { type: "PLAY_ENDED", index: 0 });
  // The next queued sentence is playable via the browser path.
  assert.equal(nextPlayable(s).index, 1);
});

test("stale AUDIO_READY for a non-fetching sentence is ignored", () => {
  const s = run(
    initialStreamQueueState,
    { type: "ADD_SENTENCE", index: 0, text: "One." },
    { type: "AUDIO_READY", index: 0 }, // never fetched: stale/duplicate
  );
  assert.equal(s.sentences[0].status, "queued");
});
