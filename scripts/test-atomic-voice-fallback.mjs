/**
 * Atomic-path (non-streaming) voice fallback regression tests for
 * patientVoiceService.ts. This is the path production actually uses today
 * (OPENAI_PATIENT_STREAMING_ENABLED=false) - see VOICE_RELIABILITY_AUDIT_*.md.
 *
 * Runs the REAL compiled service against DOM shims (MediaSource, SourceBuffer,
 * Audio, fetch, window timers, speechSynthesis), same approach as
 * test-progressive-playback.mjs, with a controllable fetch/play mock so each
 * test can force a specific failure at a specific stage:
 *
 *   A. GET /voice/status has a transient network error
 *      -> NOT treated as confirmed-unavailable; ElevenLabs is still attempted.
 *   B. status OK, synth OK, first audio.play() rejects (recoverable)
 *      -> classified as a PLAYBACK failure, Blob recovery attempted, browser
 *         TTS is never reached.
 *   C. synth returns a real HTTP failure (502)
 *      -> classified as a TTS_HTTP_ERROR (generation failure); browser TTS
 *         fallback happens directly, no pointless recovery re-fetch.
 *   D. one turn fails over to browser TTS; the next turn succeeds via
 *      ElevenLabs -> no persistent poisoning across turns.
 *   E. status confirms the case has no voice (available:false, 200 OK)
 *      -> browser TTS immediately, no /synthesize request at all.
 *
 * A second block ("CAPACITY RETRY") covers the bounded single retry of a 409
 * ("no TTS capacity slot") response specifically - see
 * TTS_CAPACITY_RETRY_DELAY_MS in patientVoiceService.ts. 502/5xx/timeout/
 * empty-audio deliberately get NO frontend retry (test C above already
 * proves this); only 409 does, and never more than once.
 *
 * A third block ("MOBILE RECOVERY") covers the device-aware buffered-first
 * strategy (isIOSDevice, mobileAudio.ts) and the user-gesture "tap to hear
 * patient" recovery built into playBuffered (patientVoiceService.ts) - see
 * the mobile voice reliability audit and its follow-up implementation.
 *
 * Run via: npm run test:voice
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

// ---------------------------------------------------------------------------
// 1. Reuse the same compiled-module patch test-progressive-playback.mjs does
//    (it is idempotent - re-patching already-patched output is a no-op).
// ---------------------------------------------------------------------------
const buildDir = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", ".test-build", "services");
for (const file of [
  "patientVoiceService.js", "api.js", "textToSpeechService.js",
  "voicePlaybackState.js", "voiceDiagnostics.js", "mobileAudio.js",
]) {
  const p = path.join(buildDir, file);
  if (!fs.existsSync(p)) continue; // some files may have no output if fully type-erased
  let code = fs.readFileSync(p, "utf8");
  code = code.replaceAll("import.meta.env", "(globalThis.__viteEnv ?? {})");
  code = code.replace(/from "(\.{1,2}\/[^"]+?)(\.js)?"/g, 'from "$1.js"');
  fs.writeFileSync(p, code);
}

// ---------------------------------------------------------------------------
// Device simulation: Node's built-in `navigator` is non-writable by default,
// so make it configurable once, then tests can freely swap it to simulate
// iOS vs desktop for isIOSDevice() (mobileAudio.ts).
// ---------------------------------------------------------------------------
Object.defineProperty(globalThis, "navigator", {
  value: { userAgent: "desktop-test", platform: "Win32", maxTouchPoints: 0 },
  configurable: true,
  writable: true,
});
function setDeviceIOS() {
  globalThis.navigator = { userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit", platform: "iPhone", maxTouchPoints: 5 };
}
function setDeviceDesktop() {
  globalThis.navigator = { userAgent: "desktop-test", platform: "Win32", maxTouchPoints: 0 };
}

// ---------------------------------------------------------------------------
// 2. DOM shims.
// ---------------------------------------------------------------------------
const timers = new Map();
let nextTimerId = 1;
const spokenByBrowserTts = [];

globalThis.window = {
  setTimeout(fn, ms) {
    const id = nextTimerId++;
    timers.set(id, { fn, ms });
    return id;
  },
  clearTimeout(id) {
    timers.delete(id);
  },
  speechSynthesis: {
    getVoices: () => [],
    cancel: () => {},
    speak(utterance) {
      spokenByBrowserTts.push(utterance.text);
      utterance.onstart?.();
      utterance.onend?.();
    },
  },
};
function fireTimers(predicate) {
  for (const [id, t] of [...timers.entries()]) {
    if (predicate(t.ms)) {
      timers.delete(id);
      t.fn();
    }
  }
}
const fireDelayTimers = () => fireTimers((ms) => ms <= 1500);

globalThis.SpeechSynthesisUtterance = class {
  constructor(text) {
    this.text = text;
  }
};

class FakeSourceBuffer {
  constructor() {
    this.updating = false;
    this.appended = [];
    this._listeners = {};
  }
  addEventListener(event, cb) {
    (this._listeners[event] ??= []).push(cb);
  }
  appendBuffer(chunk) {
    this.appended.push(chunk);
    this.updating = true;
    queueMicrotask(() => {
      this.updating = false;
      for (const cb of this._listeners.updateend ?? []) cb();
    });
  }
}

class FakeMediaSource {
  static instances = [];
  static isTypeSupported(type) {
    return type === "audio/mpeg";
  }
  constructor() {
    this.readyState = "closed";
    this.sourceBuffer = null;
    this.endOfStreamCalled = false;
    this._listeners = {};
    FakeMediaSource.instances.push(this);
  }
  addEventListener(event, cb) {
    (this._listeners[event] ??= []).push(cb);
  }
  addSourceBuffer() {
    this.sourceBuffer = new FakeSourceBuffer();
    return this.sourceBuffer;
  }
  endOfStream() {
    this.endOfStreamCalled = true;
    this.readyState = "ended";
  }
  triggerSourceOpen() {
    this.readyState = "open";
    for (const cb of this._listeners.sourceopen ?? []) cb();
  }
}
globalThis.MediaSource = FakeMediaSource;

/** Queue of forced outcomes for the NEXT Audio instances' play() calls, in
 * construction order. Each entry is either "resolve" or an Error to reject
 * with. Instances beyond the queue default to "resolve". */
let playOutcomeQueue = [];
class FakeAudio {
  static instances = [];
  constructor(src) {
    this.src = src ?? "";
    this.paused = true;
    this.playCalled = false;
    this._outcome = playOutcomeQueue.length > 0 ? playOutcomeQueue.shift() : "resolve";
    FakeAudio.instances.push(this);
  }
  play() {
    this.playCalled = true;
    if (this._outcome !== "resolve") {
      const err = this._outcome;
      return Promise.reject(err);
    }
    this.paused = false;
    queueMicrotask(() => this.onplay?.());
    return Promise.resolve();
  }
  pause() {
    this.paused = true;
  }
  removeAttribute(name) {
    if (name === "src") this.src = "";
  }
  load() {}
  triggerEnded() {
    this.onended?.();
  }
}
globalThis.Audio = FakeAudio;

globalThis.URL.createObjectURL = (obj) => {
  void obj;
  return `blob:fake-${Math.random().toString(36).slice(2)}`;
};
globalThis.URL.revokeObjectURL = () => {};

/** Controllable streamed response body (single chunk then done). */
function makeFakeBody(chunk) {
  let delivered = false;
  return {
    cancelled: false,
    getReader() {
      return {
        read: () =>
          new Promise((resolve) => {
            queueMicrotask(() => {
              if (!delivered) {
                delivered = true;
                resolve({ done: false, value: chunk });
              } else {
                resolve({ done: true, value: undefined });
              }
            });
          }),
        cancel: () => Promise.resolve(),
      };
    },
  };
}
const CHUNK = new Uint8Array([0xff, 0xfb, 0x90, 0x00]);

// ---------------------------------------------------------------------------
// 3. Configurable fetch mock. Each test sets `statusBehavior` /
//    `synthesizeBehavior` before calling speakPatientResponse.
// ---------------------------------------------------------------------------
let statusBehavior = "available"; // "available" | "unavailable" | "transient_error"
// "ok" | "http_502" | "empty" | "409_once" (first synth call only) | "409_always"
let synthesizeBehavior = "ok";
const fetchCalls = { status: 0, synthesize: 0, telemetry: 0 };
/** Every telemetry payload POSTed in the current test, parsed, for TEST L's
 * "no sensitive content" assertions. */
const telemetryPayloads = [];

globalThis.fetch = (input, init) => {
  const url = String(input);
  if (url.includes("/api/voice/telemetry")) {
    fetchCalls.telemetry += 1;
    try {
      telemetryPayloads.push(JSON.parse(init?.body ?? "{}"));
    } catch {
      telemetryPayloads.push({ __parseError: true });
    }
    return Promise.resolve({ ok: true, json: async () => ({ ok: true }) });
  }
  if (url.includes("/api/voice/status/")) {
    fetchCalls.status += 1;
    if (statusBehavior === "transient_error") {
      return Promise.reject(new TypeError("Failed to fetch"));
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({
        caseId: "carly",
        available: statusBehavior === "available",
        provider: statusBehavior === "available" ? "elevenlabs" : "browser",
        fallbackRate: 0.97,
      }),
    });
  }
  if (url.includes("/api/voice/synthesize")) {
    fetchCalls.synthesize += 1;
    if (synthesizeBehavior === "409_always" || (synthesizeBehavior === "409_once" && fetchCalls.synthesize === 1)) {
      return Promise.resolve({ ok: false, status: 409, headers: { get: () => null } });
    }
    if (synthesizeBehavior === "http_502") {
      return Promise.resolve({ ok: false, status: 502, headers: { get: () => null } });
    }
    if (synthesizeBehavior === "empty") {
      return Promise.resolve({
        ok: true,
        headers: { get: () => null },
        body: makeFakeBody(CHUNK),
        blob: async () => ({ size: 0 }),
      });
    }
    return Promise.resolve({
      ok: true,
      headers: { get: (h) => (h === "X-Pause-Before-Ms" ? "0" : null) },
      body: makeFakeBody(CHUNK),
      blob: async () => ({ size: CHUNK.byteLength }),
    });
  }
  return Promise.reject(new Error(`unexpected fetch: ${url}`));
};

async function flush(times = 8) {
  for (let i = 0; i < times; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

// ---------------------------------------------------------------------------
// 4. Import the REAL compiled service.
// ---------------------------------------------------------------------------
const svc = await import("../.test-build/services/patientVoiceService.js");

/** Drive a progressive attempt through sourceopen + the overlapped-pause
 * timer, matching how a real turn actually reaches audio.play(). */
async function driveProgressiveToPlay() {
  await flush();
  const mediaSource = FakeMediaSource.instances.at(-1);
  await flush();
  mediaSource.triggerSourceOpen();
  await flush();
  fireDelayTimers();
  await flush();
}

/** Invoke a recovery attempt function AND let its newly-created Audio element
 * settle (a fresh element is constructed inside `recover()`, so it can only
 * be reached AFTER giving the microtask queue a turn - unlike the synchronous
 * `driveProgressiveToPlay` helper, this must interleave the call with flushes). */
async function resolveViaRecovery(recover, { succeed = true } = {}) {
  const promise = recover();
  await flush();
  const audio = FakeAudio.instances.at(-1);
  if (succeed) audio.triggerEnded();
  return promise;
}

function resetPerTest() {
  statusBehavior = "available";
  synthesizeBehavior = "ok";
  fetchCalls.status = 0;
  fetchCalls.synthesize = 0;
  fetchCalls.telemetry = 0;
  telemetryPayloads.length = 0;
  playOutcomeQueue = [];
  spokenByBrowserTts.length = 0;
  setDeviceDesktop();
  svc.clearVoiceStatusCache();
}

// ---------------------------------------------------------------------------
// TEST A - transient status-probe failure must NOT poison the turn.
// ---------------------------------------------------------------------------
test("TEST A: status probe network error -> ElevenLabs is still attempted, not skipped", async () => {
  resetPerTest();
  statusBehavior = "transient_error";
  const p = svc.speakPatientResponse({ caseId: "carly", text: "Probe hiccup, ElevenLabs should still be tried." });
  await driveProgressiveToPlay();
  assert.equal(fetchCalls.synthesize, 1, "ElevenLabs /synthesize WAS attempted despite the probe error");
  assert.equal(spokenByBrowserTts.length, 0, "browser TTS not used while ElevenLabs is still playing");
  const audio = FakeAudio.instances.at(-1);
  audio.triggerEnded();
  await p;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "browser TTS never used - ElevenLabs played successfully");
});

// ---------------------------------------------------------------------------
// TEST B - playback failure (not generation failure) triggers Blob recovery,
// not an immediate drop to browser TTS.
// ---------------------------------------------------------------------------
test("TEST B: audio.play() rejects once -> Blob recovery preserves ElevenLabs voice", async () => {
  resetPerTest();
  const text = "This exact approved sentence must survive via Blob recovery.";
  playOutcomeQueue = [Object.assign(new Error("play blocked"), { name: "NotAllowedError" }), "resolve"];
  const p = svc.speakPatientResponse({ caseId: "carly", text });
  await driveProgressiveToPlay(); // first Audio() instance: play() rejects
  await flush();
  assert.equal(fetchCalls.synthesize, 2, "a SECOND /synthesize call was made for Blob recovery");
  assert.equal(spokenByBrowserTts.length, 0, "browser TTS was NOT used - recovery is still in flight/succeeded");
  const recoveryAudio = FakeAudio.instances.at(-1);
  assert.notEqual(recoveryAudio, undefined);
  recoveryAudio.triggerEnded(); // Blob playback completes normally
  await p;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "ElevenLabs voice (via Blob recovery) was used, never the robotic fallback");
});

// ---------------------------------------------------------------------------
// TEST C - a REAL generation failure (502) is not worth retrying; browser
// fallback happens directly.
// ---------------------------------------------------------------------------
test("TEST C: ElevenLabs HTTP failure (502) -> classified as generation failure, direct browser fallback", async () => {
  resetPerTest();
  const text = "ElevenLabs really failed this time.";
  synthesizeBehavior = "http_502";
  await svc.speakPatientResponse({ caseId: "carly", text });
  await flush();
  assert.equal(fetchCalls.synthesize, 1, "no pointless second attempt for a confirmed generation failure");
  assert.deepEqual(spokenByBrowserTts, [text], "browser TTS spoke the same approved text");
});

// ---------------------------------------------------------------------------
// TEST D - one turn falls back to browser; the NEXT turn still uses
// ElevenLabs normally (no persistent poisoning across turns).
// ---------------------------------------------------------------------------
test("TEST D: a failed turn does not permanently disable ElevenLabs for the next turn", async () => {
  resetPerTest();
  synthesizeBehavior = "http_502";
  await svc.speakPatientResponse({ caseId: "carly", text: "First turn fails." });
  await flush();
  assert.deepEqual(spokenByBrowserTts, ["First turn fails."]);

  spokenByBrowserTts.length = 0;
  synthesizeBehavior = "ok";
  const p2 = svc.speakPatientResponse({ caseId: "carly", text: "Second turn should use ElevenLabs." });
  await driveProgressiveToPlay();
  assert.equal(spokenByBrowserTts.length, 0, "second turn is playing via ElevenLabs, not browser TTS");
  const audio = FakeAudio.instances.at(-1);
  audio.triggerEnded();
  await p2;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "second turn completed via ElevenLabs - no lingering poisoning");
});

// ---------------------------------------------------------------------------
// TEST E - a CONFIRMED "no voice for this case" skips ElevenLabs entirely.
// ---------------------------------------------------------------------------
test("TEST E: confirmed unavailable case -> immediate browser fallback, zero /synthesize calls", async () => {
  resetPerTest();
  statusBehavior = "unavailable";
  const text = "This case has no realistic voice configured.";
  await svc.speakPatientResponse({ caseId: "carly", text });
  await flush();
  assert.equal(fetchCalls.synthesize, 0, "ElevenLabs was never contacted - the case is confirmed to have no voice");
  assert.deepEqual(spokenByBrowserTts, [text]);
});

// ===========================================================================
// CAPACITY RETRY - bounded single retry of a 409 (no TTS slot) response only.
// See TTS_CAPACITY_RETRY_DELAY_MS in patientVoiceService.ts. Every test here
// fires the retry-delay timer via the SAME shared fireDelayTimers() helper
// used elsewhere in this file (the 400ms delay is well under its 1500ms
// threshold) - no new timing utility was introduced.
// ===========================================================================

test("CAPACITY RETRY 1: first call 409, retry succeeds -> ElevenLabs plays, zero browser fallback", async () => {
  resetPerTest();
  synthesizeBehavior = "409_once";
  const text = "Capacity freed up on the retry.";
  const p = svc.speakPatientResponse({ caseId: "carly", text });
  await flush(); // first /synthesize resolves 409; code is now waiting in the retry delay
  assert.equal(fetchCalls.synthesize, 1, "only the first call has happened so far");
  fireDelayTimers(); // fires the bounded retry delay -> second /synthesize call
  await flush();
  assert.equal(fetchCalls.synthesize, 2, "exactly one retry was made");
  const mediaSource = FakeMediaSource.instances.at(-1);
  assert.ok(mediaSource, "the retry's successful response proceeded into normal ElevenLabs playback");
  mediaSource.triggerSourceOpen();
  await flush();
  fireDelayTimers(); // overlapped pause-before-speech timer
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "ElevenLabs is playing - browser fallback not used");
  const audio = FakeAudio.instances.at(-1);
  audio.triggerEnded();
  await p;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "turn completed entirely via ElevenLabs");
});

test("CAPACITY RETRY 2: first call 409, retry ALSO 409 -> stop retrying, existing browser fallback occurs", async () => {
  resetPerTest();
  synthesizeBehavior = "409_always";
  const text = "Capacity stayed full on both tries.";
  const p = svc.speakPatientResponse({ caseId: "carly", text });
  await flush(); // first 409
  assert.equal(fetchCalls.synthesize, 1);
  fireDelayTimers(); // the one allowed retry
  await flush();
  assert.equal(fetchCalls.synthesize, 2, "exactly one retry was made, no third attempt");
  await p;
  await flush();
  assert.equal(fetchCalls.synthesize, 2, "still exactly 2 - no further retries were triggered");
  assert.deepEqual(spokenByBrowserTts, [text], "existing final browser fallback occurred as before");
});

test("CAPACITY RETRY 3: a real generation failure (502) still gets NO frontend retry", async () => {
  resetPerTest();
  synthesizeBehavior = "http_502";
  const text = "This is a real provider failure, not a capacity issue.";
  await svc.speakPatientResponse({ caseId: "carly", text });
  await flush();
  assert.equal(fetchCalls.synthesize, 1, "502 is never retried at the frontend - only 409 is");
  assert.deepEqual(spokenByBrowserTts, [text]);
});

test("CAPACITY RETRY 4: a playback failure does not trigger the new capacity retry", async () => {
  resetPerTest();
  const text = "Playback fails, not generation - Blob recovery only, no capacity retry.";
  playOutcomeQueue = [Object.assign(new Error("play blocked"), { name: "NotAllowedError" }), "resolve"];
  const p = svc.speakPatientResponse({ caseId: "carly", text });
  await driveProgressiveToPlay(); // first Audio() instance: play() rejects (NOT a 409)
  await flush();
  // Exactly 2 calls: the original attempt + the EXISTING Blob-recovery
  // re-fetch (test B). Neither of these is the NEW 409-retry path - the
  // response was 200 OK both times; only playback failed.
  assert.equal(fetchCalls.synthesize, 2, "same call count as the pre-existing Blob-recovery behavior");
  const recoveryAudio = FakeAudio.instances.at(-1);
  recoveryAudio.triggerEnded();
  await p;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "ElevenLabs voice preserved via existing Blob recovery, unaffected by the 409 change");
});

test("CAPACITY RETRY 5: cancellation during the retry delay stops the retry, no stale playback", async () => {
  resetPerTest();
  synthesizeBehavior = "409_always"; // irrelevant what the retry would have returned - it must never fire
  const text = "This should never actually play or fall back.";
  const p = svc.speakPatientResponse({ caseId: "carly", text });
  await flush(); // first 409; now waiting in the retry delay
  assert.equal(fetchCalls.synthesize, 1);
  svc.cancelPatientSpeech(); // interrupt DURING the bounded retry delay
  await flush();
  assert.equal(fetchCalls.synthesize, 1, "the retry fetch never happened - cancelled during the delay");
  fireDelayTimers(); // the (already cleared) timer firing again must be a safe no-op
  await flush();
  assert.equal(fetchCalls.synthesize, 1, "still no second call after cancellation");
  await p;
  assert.equal(spokenByBrowserTts.length, 0, "no stale browser fallback either - cancellation just settles the turn");
});

test("CAPACITY RETRY 6: after a turn that exhausted its one 409 retry, the NEXT turn starts fresh", async () => {
  resetPerTest();
  synthesizeBehavior = "409_always";
  const p1 = svc.speakPatientResponse({ caseId: "carly", text: "First turn: capacity stayed full." });
  await flush();
  fireDelayTimers();
  await flush();
  await p1;
  assert.equal(fetchCalls.synthesize, 2, "first turn used exactly its one allowed retry, then fell back");
  assert.equal(spokenByBrowserTts.length, 1, "first turn fell back to browser TTS");

  spokenByBrowserTts.length = 0;
  synthesizeBehavior = "ok"; // capacity is free again for the next turn
  const p2 = svc.speakPatientResponse({ caseId: "carly", text: "Second turn should succeed via ElevenLabs." });
  await driveProgressiveToPlay();
  assert.equal(spokenByBrowserTts.length, 0, "second turn is using ElevenLabs, not browser TTS");
  const audio = FakeAudio.instances.at(-1);
  audio.triggerEnded();
  await p2;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "second turn completed via ElevenLabs - the prior failed retry did not poison it");
});

test("CAPACITY RETRY 7: retry state is per-turn, not module-global - a new turn is unaffected by another's pending retry", async () => {
  resetPerTest();
  synthesizeBehavior = "409_once"; // turn A's first call -> 409; turn A is superseded before it can retry
  const p1 = svc.speakPatientResponse({ caseId: "carly", text: "Turn A - will be superseded mid-retry-delay." });
  await flush();
  assert.equal(fetchCalls.synthesize, 1, "turn A's first call happened and is now waiting in its retry delay");

  // An independent turn starts (e.g. the next question) BEFORE turn A's retry ever fires.
  synthesizeBehavior = "ok";
  const p2 = svc.speakPatientResponse({ caseId: "carly", text: "Turn B - independent of turn A's pending retry." });
  await driveProgressiveToPlay();
  assert.equal(spokenByBrowserTts.length, 0, "turn B is playing normally via ElevenLabs");
  const audio = FakeAudio.instances.at(-1);
  audio.triggerEnded();
  await p2;
  await p1; // turn A's superseded promise also settles harmlessly (existing guard behavior)
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "turn B completed cleanly - turn A's pending retry never leaked into it");
});

// ===========================================================================
// MOBILE RECOVERY - device-aware buffered-first strategy (isIOSDevice,
// mobileAudio.ts) and the user-gesture "tap to hear patient" recovery built
// into playBuffered (patientVoiceService.ts). See the mobile voice
// reliability audit and its follow-up implementation.
// ===========================================================================

test("MOBILE 1: iOS chooses buffered playback first, not progressive MediaSource", async () => {
  resetPerTest();
  setDeviceIOS();
  const msCountBefore = FakeMediaSource.instances.length;
  const p = svc.speakPatientResponse({ caseId: "carly", text: "iOS should use buffered playback." });
  await flush();
  assert.equal(FakeMediaSource.instances.length, msCountBefore, "no MediaSource was constructed - buffered path used");
  const audio = FakeAudio.instances.at(-1);
  assert.ok(audio, "an Audio element was created for buffered playback");
  audio.triggerEnded();
  await p;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "ElevenLabs voice played via the buffered path");
});

test("MOBILE 2: desktop still uses progressive MediaSource first (unchanged)", async () => {
  resetPerTest(); // device is desktop by default
  const p = svc.speakPatientResponse({ caseId: "carly", text: "Desktop should use progressive playback." });
  await driveProgressiveToPlay();
  assert.ok(FakeMediaSource.instances.length > 0, "MediaSource WAS constructed - progressive path used, exactly as before");
  const audio = FakeAudio.instances.at(-1);
  audio.triggerEnded();
  await p;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0);
});

test("MOBILE 3: ElevenLabs succeeds but play() is NotAllowedError -> recovery offered, no immediate robotic fallback", async () => {
  resetPerTest();
  setDeviceIOS();
  playOutcomeQueue = [Object.assign(new Error("blocked"), { name: "NotAllowedError" })];
  let offeredRecover = null;
  let resolvedCalled = false;
  const p = svc.speakPatientResponse({
    caseId: "carly",
    text: "Needs a tap.",
    onPlaybackRecoveryAvailable: (fn) => { offeredRecover = fn; },
    onPlaybackRecoveryResolved: () => { resolvedCalled = true; },
  });
  await flush();
  assert.ok(offeredRecover, "recovery was offered instead of an immediate fallback");
  assert.equal(spokenByBrowserTts.length, 0, "no immediate robotic fallback");
  assert.equal(resolvedCalled, false, "still waiting for the tap - not resolved yet");
  // Settle the turn cleanly so it doesn't leak into the next test.
  const ok = await resolveViaRecovery(offeredRecover);
  assert.equal(ok, true);
  await p;
});

test("MOBILE 4: tapping recovery replays the SAME already-downloaded audio - no new /synthesize call", async () => {
  resetPerTest();
  setDeviceIOS();
  playOutcomeQueue = [Object.assign(new Error("blocked"), { name: "NotAllowedError" })];
  let recover = null;
  const p = svc.speakPatientResponse({
    caseId: "carly",
    text: "Tap to replay.",
    onPlaybackRecoveryAvailable: (fn) => { recover = fn; },
    onPlaybackRecoveryResolved: () => {},
  });
  await flush();
  assert.ok(recover, "recovery offered");
  assert.equal(fetchCalls.synthesize, 1, "only the ORIGINAL /synthesize call happened so far");
  const ok = await resolveViaRecovery(recover); // simulates the real click handler, using a fresh gesture
  assert.equal(ok, true, "recovery attempt reports success");
  assert.equal(fetchCalls.synthesize, 1, "STILL only 1 - the tap replayed the SAME downloaded audio, no re-synthesis, no OpenAI call");
  await p;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "ElevenLabs voice heard - browser TTS never used");
});

test("MOBILE 5: recovery tap ALSO fails -> existing browser fallback runs exactly once", async () => {
  resetPerTest();
  setDeviceIOS();
  playOutcomeQueue = [
    Object.assign(new Error("blocked"), { name: "NotAllowedError" }),
    Object.assign(new Error("still blocked"), { name: "NotAllowedError" }),
  ];
  let recover = null;
  let resolvedCount = 0;
  const p = svc.speakPatientResponse({
    caseId: "carly",
    text: "Tap fails too.",
    onPlaybackRecoveryAvailable: (fn) => { recover = fn; },
    onPlaybackRecoveryResolved: () => { resolvedCount += 1; },
  });
  await flush();
  assert.ok(recover, "recovery offered");
  const ok = await recover();
  assert.equal(ok, false, "second attempt also failed");
  assert.equal(resolvedCount, 1, "the resolved callback fired exactly once");
  await p;
  await flush();
  assert.deepEqual(spokenByBrowserTts, ["Tap fails too."], "existing browser fallback ran exactly once");
});

test("MOBILE 6: a real provider failure (502) never offers Tap-to-play - no valid audio exists", async () => {
  resetPerTest();
  setDeviceIOS();
  synthesizeBehavior = "http_502";
  let offered = false;
  await svc.speakPatientResponse({
    caseId: "carly",
    text: "Provider really failed.",
    onPlaybackRecoveryAvailable: () => { offered = true; },
    onPlaybackRecoveryResolved: () => {},
  });
  await flush();
  assert.equal(offered, false, "recovery was never offered - no valid audio was ever downloaded");
  assert.deepEqual(spokenByBrowserTts, ["Provider really failed."], "existing provider-failure fallback behavior preserved");
});

test("MOBILE 7: the 409 capacity retry is unaffected by the iOS buffered-first change", async () => {
  resetPerTest();
  setDeviceIOS();
  synthesizeBehavior = "409_once";
  const p = svc.speakPatientResponse({ caseId: "carly", text: "iOS + capacity retry." });
  await flush();
  assert.equal(fetchCalls.synthesize, 1);
  fireDelayTimers(); // the one allowed 409 retry delay
  await flush();
  assert.equal(fetchCalls.synthesize, 2, "exactly one 409 retry, same behavior as desktop");
  const audio = FakeAudio.instances.at(-1);
  audio.triggerEnded();
  await p;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0);
});

test("MOBILE 8: cancellation while waiting for the tap prevents a stale replay", async () => {
  resetPerTest();
  setDeviceIOS();
  playOutcomeQueue = [Object.assign(new Error("blocked"), { name: "NotAllowedError" })];
  let recover = null;
  let resolvedCount = 0;
  const p = svc.speakPatientResponse({
    caseId: "carly",
    text: "Cancelled before tap.",
    onPlaybackRecoveryAvailable: (fn) => { recover = fn; },
    onPlaybackRecoveryResolved: () => { resolvedCount += 1; },
  });
  await flush();
  assert.ok(recover, "recovery was offered");
  svc.cancelPatientSpeech(); // interrupt WHILE waiting for the tap
  await flush();
  assert.equal(resolvedCount, 1, "the recovery affordance was cleared by the cancellation");
  const ok = await recover(); // a stale click arriving after cancellation
  assert.equal(ok, false, "a stale recovery attempt cannot replay old audio");
  await p;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "no fallback fired either - cancellation just ends the turn");
});

test("MOBILE 9: after a recovery-resolved turn, the next turn starts fresh with no sticky recovery state", async () => {
  resetPerTest();
  setDeviceIOS();
  playOutcomeQueue = [Object.assign(new Error("blocked"), { name: "NotAllowedError" })];
  let recover1 = null;
  const p1 = svc.speakPatientResponse({
    caseId: "carly",
    text: "First turn.",
    onPlaybackRecoveryAvailable: (fn) => { recover1 = fn; },
    onPlaybackRecoveryResolved: () => {},
  });
  await flush();
  assert.ok(recover1);
  await resolveViaRecovery(recover1); // tap succeeds
  await p1;
  await flush();

  spokenByBrowserTts.length = 0;
  let offeredAgain = false;
  const p2 = svc.speakPatientResponse({
    caseId: "carly",
    text: "Second turn.",
    onPlaybackRecoveryAvailable: () => { offeredAgain = true; },
    onPlaybackRecoveryResolved: () => {},
  });
  await flush();
  assert.equal(offeredAgain, false, "second turn's playback succeeds cleanly - fresh attempt, no leftover recovery state");
  const audio = FakeAudio.instances.at(-1);
  audio.triggerEnded();
  await p2;
  await flush();
  assert.equal(spokenByBrowserTts.length, 0);
});

test("MOBILE 10: recovery state is per-turn - a new turn during another's pending recovery wait is unaffected", async () => {
  resetPerTest();
  setDeviceIOS();
  playOutcomeQueue = [Object.assign(new Error("blocked"), { name: "NotAllowedError" })];
  let resolvedA = 0;
  const p1 = svc.speakPatientResponse({
    caseId: "carly",
    text: "Turn A - superseded while waiting for a tap.",
    onPlaybackRecoveryAvailable: () => {},
    onPlaybackRecoveryResolved: () => { resolvedA += 1; },
  });
  await flush();
  assert.equal(resolvedA, 0, "turn A is still waiting for its tap");

  // Turn B starts before turn A's recovery is ever tapped.
  const p2 = svc.speakPatientResponse({ caseId: "carly", text: "Turn B - independent." });
  await flush();
  assert.equal(resolvedA, 1, "turn A's pending recovery was cleared when turn B superseded it");
  const audioB = FakeAudio.instances.at(-1);
  audioB.triggerEnded();
  await p2;
  await p1; // turn A's superseded promise also settles harmlessly
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "turn B completed cleanly via ElevenLabs - turn A's recovery never leaked into it");
});

test("MOBILE 11: telemetry events carry only safe operational fields, never patient text or secrets", async () => {
  resetPerTest();
  setDeviceIOS();
  const patientText = "This exact sentence must never appear in telemetry.";
  const p = svc.speakPatientResponse({ caseId: "carly", text: patientText });
  await flush();
  FakeAudio.instances.at(-1).triggerEnded();
  await p;
  await flush();
  assert.ok(fetchCalls.telemetry > 0, "at least one telemetry event was shipped");
  for (const payload of telemetryPayloads) {
    const json = JSON.stringify(payload);
    assert.equal(json.includes(patientText), false, "patient text must never appear in a telemetry payload");
    assert.equal(Object.prototype.hasOwnProperty.call(payload, "audio"), false, "no audio field exists");
    assert.equal(json.toLowerCase().includes("apikey"), false);
    assert.equal(json.toLowerCase().includes("token"), false);
  }
  const sample = telemetryPayloads.find((p) => p.event === "mobile_buffered_first");
  assert.ok(sample, "the new mobile_buffered_first event was shipped");
  assert.equal(typeof sample.correlationId, "string");
  assert.equal(typeof sample.deviceCategory, "string");
  assert.equal(sample.deviceCategory, "ios");
});
