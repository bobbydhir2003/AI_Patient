/**
 * Progressive-playback integration tests for patientVoiceService.
 *
 * Runs the REAL compiled service against DOM shims (MediaSource, SourceBuffer,
 * Audio, fetch, URL, window timers, speechSynthesis) so the actual wiring is
 * observed - including the `audio.src = objectURL` MediaSource attachment that
 * a pure-logic test cannot see. This is a DOM shim, not a real browser: final
 * verification in Chrome is still a manual step.
 *
 * Run via: npm run test:voice
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

// ---------------------------------------------------------------------------
// 1. Make the compiled Vite modules loadable in Node (import.meta.env shim).
// ---------------------------------------------------------------------------
const buildDir = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", ".test-build", "services");
for (const file of ["patientVoiceService.js", "api.js", "textToSpeechService.js", "voicePlaybackState.js"]) {
  const p = path.join(buildDir, file);
  let code = fs.readFileSync(p, "utf8");
  code = code.replaceAll("import.meta.env", "(globalThis.__viteEnv ?? {})");
  // Node ESM needs explicit extensions on relative imports (tsc leaves them bare).
  code = code.replace(/from "(\.{1,2}\/[^"]+?)(\.js)?"/g, 'from "$1.js"');
  fs.writeFileSync(p, code);
}

// ---------------------------------------------------------------------------
// 2. DOM shims (installed BEFORE importing the service).
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

/** Fire fake timers matching a predicate on their delay (ms). */
function fireTimers(predicate) {
  for (const [id, t] of [...timers.entries()]) {
    if (predicate(t.ms)) {
      timers.delete(id);
      t.fn();
    }
  }
}
const fireDelayTimers = () => fireTimers((ms) => ms <= 1500); // pauseBeforeMs
const fireWatchdog = () => fireTimers((ms) => ms >= 10_000);

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
  /** Simulates the browser: sourceopen fires only for an ATTACHED MediaSource. */
  triggerSourceOpen() {
    this.readyState = "open";
    for (const cb of this._listeners.sourceopen ?? []) cb();
  }
}
globalThis.MediaSource = FakeMediaSource;

class FakeAudio {
  static instances = [];
  constructor(src) {
    this.src = src ?? "";
    this.paused = true;
    this.playCalled = false;
    FakeAudio.instances.push(this);
  }
  play() {
    this.playCalled = true;
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

const objectUrls = new Map();
const revokedUrls = [];
let nextUrlId = 1;
globalThis.URL.createObjectURL = (obj) => {
  const url = `blob:fake-${nextUrlId++}`;
  objectUrls.set(url, obj);
  return url;
};
globalThis.URL.revokeObjectURL = (url) => {
  revokedUrls.push(url);
};

/** Controllable streamed response body. */
function makeFakeBody() {
  const queue = [];
  let pendingRead = null;
  let ended = false;
  let cancelled = false;
  const tryFlush = () => {
    if (!pendingRead) return;
    if (queue.length > 0) {
      const value = queue.shift();
      const { resolve } = pendingRead;
      pendingRead = null;
      resolve({ done: false, value });
    } else if (ended || cancelled) {
      const { resolve } = pendingRead;
      pendingRead = null;
      resolve({ done: true, value: undefined });
    }
  };
  return {
    pushChunk(bytes) {
      queue.push(bytes);
      tryFlush();
    },
    endStream() {
      ended = true;
      tryFlush();
    },
    get cancelled() {
      return cancelled;
    },
    getReader() {
      return {
        read: () =>
          new Promise((resolve) => {
            pendingRead = { resolve };
            tryFlush();
          }),
        cancel: () => {
          cancelled = true;
          tryFlush();
          return Promise.resolve();
        },
      };
    },
  };
}

let currentBody = null;
globalThis.fetch = (input) => {
  const url = String(input);
  if (url.includes("/api/voice/status/")) {
    return Promise.resolve({
      ok: true,
      json: async () => ({ caseId: "carly", available: true, provider: "elevenlabs", fallbackRate: 0.97 }),
    });
  }
  if (url.includes("/api/voice/synthesize")) {
    currentBody = makeFakeBody();
    return Promise.resolve({
      ok: true,
      headers: { get: (h) => (h === "X-Pause-Before-Ms" ? "300" : null) },
      body: currentBody,
    });
  }
  return Promise.reject(new Error(`unexpected fetch: ${url}`));
};

/** Let queued microtasks/promise chains run. */
async function flush(times = 6) {
  for (let i = 0; i < times; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

// ---------------------------------------------------------------------------
// 3. Import the REAL compiled service and drive full scenarios.
// ---------------------------------------------------------------------------
const svc = await import("../.test-build/services/patientVoiceService.js");

const CHUNK = new Uint8Array([0xff, 0xfb, 0x90, 0x00]); // MP3-ish frame header bytes

function latest() {
  return {
    mediaSource: FakeMediaSource.instances.at(-1),
    audio: FakeAudio.instances.at(-1),
  };
}

/** Start a speak call and advance to "synthesize response received". */
async function startSpeak(text = "It has been a lot to process, honestly.") {
  const promise = svc.speakPatientResponse({ caseId: "carly", text, turnId: "t1", sessionId: "s1" });
  await flush();
  return promise;
}

function settleTracker(promise) {
  const state = { settled: false, rejected: false };
  promise.then(
    () => {
      state.settled = true;
    },
    () => {
      state.settled = true;
      state.rejected = true;
    },
  );
  return state;
}

test("MediaSource object URL is attached to the audio element before sourceopen", async () => {
  const promise = startSpeak();
  const speakPromise = await promise;
  void speakPromise;
  const { mediaSource, audio } = latest();
  assert.ok(mediaSource, "MediaSource was created");
  assert.ok(audio, "Audio element was created");
  // THE primary-bug assertion: the element's src IS the MediaSource's URL,
  // assigned before sourceopen has fired.
  assert.match(audio.src, /^blob:fake-/);
  assert.equal(objectUrls.get(audio.src), mediaSource);
  assert.equal(mediaSource.readyState, "closed"); // sourceopen not yet fired
  svc.cancelPatientSpeech();
  await flush();
});

test("full progressive flow: sourceopen → append → play → ended resolves the promise", async () => {
  const p = startSpeak();
  const tracker = settleTracker(p);
  await flush();
  const { mediaSource, audio } = latest();

  currentBody.pushChunk(CHUNK); // first chunk arrives while source still opening
  await flush();
  mediaSource.triggerSourceOpen(); // attachment worked → browser opens the source
  await flush();
  assert.equal(mediaSource.sourceBuffer.appended.length, 1, "first chunk appended");

  fireDelayTimers(); // remaining overlapped pause elapses
  await flush();
  assert.equal(audio.playCalled, true, "audio.play() was called before download completed");

  currentBody.pushChunk(CHUNK); // more audio arrives DURING playback
  currentBody.endStream();
  await flush();
  assert.equal(mediaSource.sourceBuffer.appended.length, 2);
  assert.equal(mediaSource.endOfStreamCalled, true);

  audio.triggerEnded();
  await flush();
  assert.equal(tracker.settled, true, "speaking promise resolved on ended");
  assert.equal(tracker.rejected, false);
  assert.ok(revokedUrls.includes(audio.src) || audio.src === "", "object URL cleaned up");
});

test("interruption BEFORE sourceopen settles the promise; late sourceopen is ignored", async () => {
  const p = startSpeak();
  const tracker = settleTracker(p);
  await flush();
  const { mediaSource, audio } = latest();

  svc.cancelPatientSpeech(); // interrupt before any buffering
  await flush();
  assert.equal(tracker.settled, true, "promise settled by cancellation");
  assert.equal(tracker.rejected, false, "cancellation resolves, not rejects");

  mediaSource.triggerSourceOpen(); // late event after cancellation
  currentBody.pushChunk(CHUNK);
  await flush();
  assert.equal(mediaSource.sourceBuffer, null, "no SourceBuffer created after cancel");
  assert.equal(audio.playCalled, false, "cancelled audio never plays");
});

test("interruption DURING pauseBeforeMs (buffering done) settles; audio never plays", async () => {
  const p = startSpeak();
  const tracker = settleTracker(p);
  await flush();
  const { mediaSource, audio } = latest();
  currentBody.pushChunk(CHUNK);
  await flush();
  mediaSource.triggerSourceOpen();
  await flush();
  assert.equal(mediaSource.sourceBuffer.appended.length, 1);
  // Delay timer is pending (overlapped pause). Interrupt NOW.
  svc.cancelPatientSpeech();
  await flush();
  assert.equal(tracker.settled, true, "promise settled despite pending delay");
  fireDelayTimers(); // stale timer fires later
  await flush();
  assert.equal(audio.playCalled, false, "cancelled audio never resumes");
});

test("interruption DURING active playback settles and stops the audio", async () => {
  const p = startSpeak();
  const tracker = settleTracker(p);
  await flush();
  const { mediaSource, audio } = latest();
  currentBody.pushChunk(CHUNK);
  await flush();
  mediaSource.triggerSourceOpen();
  await flush();
  fireDelayTimers();
  await flush();
  assert.equal(audio.playCalled, true);

  svc.cancelPatientSpeech(); // student interrupts mid-sentence
  await flush();
  assert.equal(tracker.settled, true);
  assert.equal(audio.paused, true, "audio element stopped");
  audio.triggerEnded(); // stale ended from a detached element
  currentBody.pushChunk(CHUNK); // stale chunk
  await flush();
  assert.equal(tracker.rejected, false);
});

test("watchdog: playback that never starts fails over to browser TTS with the SAME text", async () => {
  spokenByBrowserTts.length = 0;
  const text = "This exact approved sentence must be spoken by the fallback.";
  const p = startSpeak(text);
  const tracker = settleTracker(p);
  await flush();
  const { mediaSource, audio } = latest();
  currentBody.pushChunk(CHUNK);
  await flush();
  // sourceopen NEVER fires (simulates the original hang). Watchdog fires:
  fireWatchdog();
  await flush();
  assert.equal(tracker.settled, true, "speaking promise settled after watchdog");
  assert.equal(tracker.rejected, false, "speakPatientResponse never rejects outward");
  assert.deepEqual(spokenByBrowserTts, [text], "browser TTS spoke the same approved text");
  // Timed-out progressive stream must never start later:
  mediaSource.triggerSourceOpen();
  currentBody.pushChunk(CHUNK);
  fireDelayTimers();
  await flush();
  assert.equal(mediaSource.sourceBuffer, null, "late sourceopen ignored after timeout");
  assert.equal(audio.playCalled, false, "timed-out stream never plays");
});

test("watchdog is cleared once playback starts (no spurious failover)", async () => {
  spokenByBrowserTts.length = 0;
  const p = startSpeak();
  const tracker = settleTracker(p);
  await flush();
  const { mediaSource, audio } = latest();
  currentBody.pushChunk(CHUNK);
  await flush();
  mediaSource.triggerSourceOpen();
  await flush();
  fireDelayTimers();
  await flush();
  assert.equal(audio.playCalled, true);
  fireWatchdog(); // would have fired later; must be a no-op now
  await flush();
  assert.equal(spokenByBrowserTts.length, 0, "no fallback after successful start");
  assert.equal(tracker.settled, false, "still playing");
  currentBody.endStream();
  await flush();
  audio.triggerEnded();
  await flush();
  assert.equal(tracker.settled, true);
});

test("a new turn supersedes the previous one; the old promise settles and old audio never plays", async () => {
  const first = startSpeak("First reply.");
  const firstTracker = settleTracker(first);
  await flush();
  const firstAudio = FakeAudio.instances.at(-1);

  const second = startSpeak("Second reply.");
  const secondTracker = settleTracker(second);
  await flush();
  assert.equal(firstTracker.settled, true, "previous speaking promise settled");
  assert.equal(firstAudio.playCalled, false, "previous turn's audio never played");

  const { mediaSource, audio } = latest();
  currentBody.pushChunk(CHUNK);
  await flush();
  mediaSource.triggerSourceOpen();
  await flush();
  fireDelayTimers();
  await flush();
  assert.equal(audio.playCalled, true, "only the latest turn plays");
  svc.cancelPatientSpeech();
  await flush();
  assert.equal(secondTracker.settled, true);
});

test("stream reader is cancelled on interruption (backend stops being read)", async () => {
  const p = startSpeak();
  const tracker = settleTracker(p);
  await flush();
  const body = currentBody;
  svc.cancelPatientSpeech();
  await flush();
  assert.equal(body.cancelled, true, "reader.cancel() reached the stream");
  assert.equal(tracker.settled, true);
});
