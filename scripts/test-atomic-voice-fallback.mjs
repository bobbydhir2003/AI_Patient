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
for (const file of ["patientVoiceService.js", "api.js", "textToSpeechService.js", "voicePlaybackState.js", "voiceDiagnostics.js"]) {
  const p = path.join(buildDir, file);
  if (!fs.existsSync(p)) continue; // voiceDiagnostics.js may already be inlined/absent depending on tsc output layout
  let code = fs.readFileSync(p, "utf8");
  code = code.replaceAll("import.meta.env", "(globalThis.__viteEnv ?? {})");
  code = code.replace(/from "(\.{1,2}\/[^"]+?)(\.js)?"/g, 'from "$1.js"');
  fs.writeFileSync(p, code);
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
let synthesizeBehavior = "ok"; // "ok" | "http_502" | "empty"
const fetchCalls = { status: 0, synthesize: 0 };

globalThis.fetch = (input) => {
  const url = String(input);
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

function resetPerTest() {
  statusBehavior = "available";
  synthesizeBehavior = "ok";
  fetchCalls.status = 0;
  fetchCalls.synthesize = 0;
  playOutcomeQueue = [];
  spokenByBrowserTts.length = 0;
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
