/**
 * Unit tests for the pure playback bookkeeping (voicePlaybackState.ts):
 * stale-response rejection, cancellation, provider choice, and pause clamping.
 * Run via: npm run test:voice
 */
import assert from "node:assert/strict";
import test from "node:test";
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
} from "../.test-build/services/voicePlaybackState.js";

test("only the latest generation is current", () => {
  const guard = createPlaybackGuard();
  const first = guard.begin();
  assert.equal(guard.isCurrent(first), true);
  const second = guard.begin();
  // A late/out-of-order completion of the first request must be ignored.
  assert.equal(guard.isCurrent(first), false);
  assert.equal(guard.isCurrent(second), true);
});

test("cancellation invalidates every outstanding generation", () => {
  const guard = createPlaybackGuard();
  const g = guard.begin();
  guard.invalidateAll();
  // A network response arriving after cancellation must not play.
  assert.equal(guard.isCurrent(g), false);
});

test("cancelled audio does not resume: a new request wins after cancel", () => {
  const guard = createPlaybackGuard();
  const oldGen = guard.begin();
  guard.invalidateAll(); // student interrupted
  const newGen = guard.begin(); // next question's response
  assert.equal(guard.isCurrent(oldGen), false);
  assert.equal(guard.isCurrent(newGen), true);
});

test("two requests completing out of order: only the newest may play", () => {
  const guard = createPlaybackGuard();
  const a = guard.begin();
  const b = guard.begin();
  // Simulate: b's audio arrives first and plays; then a's late audio arrives.
  assert.equal(guard.isCurrent(b), true);
  assert.equal(guard.isCurrent(a), false); // stale: silently dropped
});

test("provider choice: elevenlabs only when explicitly available", () => {
  assert.equal(chooseProvider(true), "elevenlabs");
  assert.equal(chooseProvider(false), "browser");
  assert.equal(chooseProvider(undefined), "browser"); // unknown/unreachable
});

test("pause clamp: safe range and malformed input", () => {
  assert.equal(clampPauseMs("450"), 450);
  assert.equal(clampPauseMs(99999), 1500); // upper clamp
  assert.equal(clampPauseMs(-50), 0); // lower clamp
  assert.equal(clampPauseMs("not-a-number"), 150); // fallback
  assert.equal(clampPauseMs(null), 150);
  assert.equal(clampPauseMs(undefined), 150);
});

// ---------------------------------------------------------------------------
// Pause overlap: pauseBeforeMs starts at TTS request start; only the remainder
// is waited before playback.
// ---------------------------------------------------------------------------

test("pause overlap: audio ready after the pause elapsed → no extra wait", () => {
  // Required pause 500 ms, audio ready after 800 ms → 0 ms additional wait.
  assert.equal(remainingPauseMs(500, 800), 0);
});

test("pause overlap: audio ready early → wait only the remainder", () => {
  // Required pause 500 ms, audio ready after 200 ms → wait 300 ms more.
  assert.equal(remainingPauseMs(500, 200), 300);
});

test("pause overlap: clamps pause like the backend and tolerates bad input", () => {
  assert.equal(remainingPauseMs(99999, 0), 1500); // pause clamped to 1500
  assert.equal(remainingPauseMs(-100, 0), 0); // negative pause → none
  assert.equal(remainingPauseMs("bad", 50), 100); // fallback 150 − 50 elapsed
  assert.equal(remainingPauseMs(500, -50), 500); // negative elapsed ignored
  assert.equal(remainingPauseMs(500, Number.NaN), 500);
});

// ---------------------------------------------------------------------------
// Progressive playback reducer: cancellation must be terminal in EVERY phase
// and stale chunks/events must never change the outcome.
// ---------------------------------------------------------------------------

const seq = (events, from = initialProgressiveState) =>
  events.reduce((s, type) => reduceProgressive(s, { type }), from);

test("progressive: playback cannot start before the first chunk", () => {
  assert.equal(canStartPlayback(initialProgressiveState), false);
  const buffered = seq(["CHUNK"]);
  assert.equal(buffered.phase, "streaming");
  assert.equal(canStartPlayback(buffered), true);
});

test("progressive: normal flow requested → streaming → playing → ended", () => {
  const s = seq(["CHUNK", "PLAYBACK_STARTED", "CHUNK", "CHUNK", "STREAM_ENDED", "PLAYBACK_ENDED"]);
  assert.equal(s.phase, "ended");
  assert.equal(s.chunksReceived, 3); // chunks kept arriving DURING playback
  assert.equal(s.streamComplete, true);
});

test("progressive: interrupt BEFORE the first chunk", () => {
  const s = seq(["CANCEL"]);
  assert.equal(s.phase, "cancelled");
  // A chunk arriving after cancellation is stale and must be dropped.
  const after = reduceProgressive(s, { type: "CHUNK" });
  assert.equal(after.phase, "cancelled");
  assert.equal(after.chunksReceived, 0);
  assert.equal(canAppendChunk(after), false);
});

test("progressive: interrupt DURING buffering (before playback)", () => {
  const s = seq(["CHUNK", "CHUNK", "CANCEL"]);
  assert.equal(s.phase, "cancelled");
  assert.equal(canStartPlayback(s), false); // cancelled audio never starts
  // Late PLAYBACK_STARTED (e.g. a pause timer that already fired) is ignored.
  assert.equal(reduceProgressive(s, { type: "PLAYBACK_STARTED" }).phase, "cancelled");
});

test("progressive: interrupt DURING active playback", () => {
  const s = seq(["CHUNK", "PLAYBACK_STARTED", "CANCEL"]);
  assert.equal(s.phase, "cancelled");
  // Cancelled audio never resumes: no event can leave the cancelled state.
  for (const type of ["CHUNK", "PLAYBACK_STARTED", "STREAM_ENDED", "PLAYBACK_ENDED"]) {
    assert.equal(reduceProgressive(s, { type }).phase, "cancelled", type);
  }
});

test("progressive: interrupt NEAR the end (stream complete, still playing)", () => {
  const s = seq(["CHUNK", "PLAYBACK_STARTED", "STREAM_ENDED", "CANCEL"]);
  assert.equal(s.phase, "cancelled");
  assert.equal(reduceProgressive(s, { type: "PLAYBACK_ENDED" }).phase, "cancelled");
});

test("progressive: failure is terminal and triggers no further transitions", () => {
  const s = seq(["CHUNK", "FAIL"]);
  assert.equal(s.phase, "failed");
  assert.equal(isTerminal(s), true);
  assert.equal(reduceProgressive(s, { type: "CHUNK" }).chunksReceived, 1); // unchanged
});

test("progressive: 'ended' from a detached element while only buffering is stale", () => {
  const s = seq(["CHUNK", "PLAYBACK_ENDED"]); // never started playing
  assert.equal(s.phase, "streaming"); // ignored - not a real completion
});

test("progressive + guard: previous turn cannot play after a new turn begins", () => {
  const guard = createPlaybackGuard();
  const oldGen = guard.begin();
  let oldState = seq(["CHUNK"]); // old turn was buffering
  const newGen = guard.begin(); // new patient response arrives
  // The service checks guard.isCurrent before every append/play; simulate it:
  if (!guard.isCurrent(oldGen)) oldState = reduceProgressive(oldState, { type: "CANCEL" });
  assert.equal(oldState.phase, "cancelled");
  assert.equal(guard.isCurrent(newGen), true);
});
