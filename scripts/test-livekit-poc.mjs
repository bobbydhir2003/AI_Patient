/**
 * Phase 1 LiveKit POC regression tests for livekitPocEngine.ts.
 *
 * Completely isolated from `npm run test:voice`'s shared build directory and
 * patch-loop list (own `.test-build/livekit` output, own script, own npm
 * command) - this engine is NOT part of the legacy voice path and must never
 * be able to destabilize its tests or vice versa.
 *
 * Covers:
 *   STATIC  - the compiled/source engine never references MediaSource, Blob
 *             object URLs, `new Audio()`, or `speechSynthesis` (the whole
 *             point of routing patient audio over a persistent WebRTC track
 *             instead of the legacy per-turn player).
 *   DYNAMIC - the same, but proven live: MediaSource/Audio/speechSynthesis
 *             are poisoned globals that throw if ever touched, and the full
 *             start -> turn -> end flow still completes cleanly.
 *   STATE   - the IDLE -> CONNECTING -> LISTENING -> THINKING -> SPEAKING ->
 *             LISTENING -> ENDED transition sequence, driven by a fake
 *             `livekit-client` Room (real SDK never contacted).
 *
 * Run via: npm run test:livekitpoc
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test, { mock } from "node:test";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(scriptDir, "..");
const buildDir = path.join(repoRoot, ".test-build", "livekit", "services");

// ---------------------------------------------------------------------------
// STATIC: source-level proof the engine never references the legacy
// MediaSource/Blob/Audio/speechSynthesis playback primitives.
// ---------------------------------------------------------------------------
test("STATIC: livekitPocEngine.ts source never references MediaSource/Blob/new Audio/speechSynthesis", () => {
  const rawSource = fs.readFileSync(
    path.join(repoRoot, "src", "services", "livekit", "livekitPocEngine.ts"),
    "utf8",
  );
  // Strip comments first - the file's own docstring explains (in prose) which
  // legacy APIs it deliberately avoids, which would otherwise self-match.
  const source = rawSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  for (const forbidden of ["new MediaSource(", "new Audio(", "new Blob(", "createObjectURL(", "speechSynthesis."]) {
    assert.ok(!source.includes(forbidden), `livekitPocEngine.ts must never use "${forbidden}"`);
  }
  // Positive control: it DOES use the WebRTC track-attach path.
  assert.ok(source.includes("track.attach()"));
});

// ---------------------------------------------------------------------------
// Patch compiled relative imports (mirrors scripts/test-atomic-voice-fallback.mjs's
// approach exactly, applied to this test's own isolated build dir only).
// ---------------------------------------------------------------------------
for (const file of [
  path.join("livekit", "livekitPocEngine.js"),
  "api.js",
  "speechRecognitionService.js",
  "voiceDiagnostics.js",
  "mobileAudio.js",
]) {
  const p = path.join(buildDir, file);
  if (!fs.existsSync(p)) continue;
  let code = fs.readFileSync(p, "utf8");
  code = code.replaceAll("import.meta.env", "(globalThis.__viteEnv ?? {})");
  code = code.replace(/from "(\.{1,2}\/[^"]+?)(\.js)?"/g, 'from "$1.js"');
  fs.writeFileSync(p, code);
}

// ---------------------------------------------------------------------------
// DYNAMIC: poison the legacy playback primitives so any accidental use fails
// loudly instead of silently "working" in a way this test wouldn't notice.
// ---------------------------------------------------------------------------
function poison(name) {
  return class {
    constructor() {
      throw new Error(`${name} must never be used by the LiveKit POC path`);
    }
  };
}
globalThis.MediaSource = poison("MediaSource");
globalThis.Audio = poison("Audio");
if (typeof globalThis.URL === "function") {
  globalThis.URL.createObjectURL = () => {
    throw new Error("URL.createObjectURL must never be used by the LiveKit POC path");
  };
}

// ---------------------------------------------------------------------------
// Fake browser SpeechRecognition (the ONLY STT this Phase 1 POC uses -
// deliberately reused unchanged from the legacy engine, see
// livekitPocEngine.ts's docstring).
// ---------------------------------------------------------------------------
class FakeSpeechRecognition {
  constructor() {
    FakeSpeechRecognition.instances.push(this);
    this.onstart = null;
    this.onresult = null;
    this.onerror = null;
    this.onend = null;
  }
  start() {}
  stop() {}
  abort() {
    this.onend?.();
  }
  emitFinal(text) {
    this.onresult?.({
      resultIndex: 0,
      results: [{ isFinal: true, 0: { transcript: text } }],
    });
  }
}
FakeSpeechRecognition.instances = [];

let timerId = 1;
const pendingTimers = new Map();

globalThis.window = {
  SpeechRecognition: FakeSpeechRecognition,
  speechSynthesis: {
    speak: () => {
      throw new Error("speechSynthesis must never be used by the LiveKit POC path");
    },
  },
  setTimeout: (fn, _ms) => {
    const id = timerId++;
    pendingTimers.set(id, fn);
    return id;
  },
  clearTimeout: (id) => {
    pendingTimers.delete(id);
  },
};

// ---------------------------------------------------------------------------
// Fake `livekit-client` module (real SDK never contacted in this test).
// ---------------------------------------------------------------------------
const RoomEvent = {
  Disconnected: "disconnected",
  Reconnecting: "reconnecting",
  Reconnected: "reconnected",
  TrackSubscribed: "trackSubscribed",
  ParticipantConnected: "participantConnected",
  DataReceived: "dataReceived",
};
const Track = { Kind: { Audio: "audio" } };

const createdRooms = [];
class FakeRoom {
  constructor() {
    this._handlers = {};
    this.remoteParticipants = new Map();
    this.connectCalls = [];
    this.disconnectCalls = 0;
    this.publishedData = [];
    this.localParticipant = {
      setMicrophoneEnabled: async () => {},
      publishData: async (payload, options) => {
        this.publishedData.push({ payload, options });
      },
    };
    createdRooms.push(this);
  }
  on(event, cb) {
    (this._handlers[event] ??= []).push(cb);
    return this;
  }
  emit(event, ...args) {
    for (const cb of this._handlers[event] ?? []) cb(...args);
  }
  async connect(url, token) {
    this.connectCalls.push({ url, token });
  }
  async disconnect() {
    this.disconnectCalls += 1;
  }
}

mock.module("livekit-client", {
  exports: { Room: FakeRoom, RoomEvent, Track },
});

// ---------------------------------------------------------------------------
// Fake token endpoint.
// ---------------------------------------------------------------------------
const fetchCalls = [];
globalThis.fetch = async (url, init) => {
  fetchCalls.push({ url: String(url), init });
  return {
    ok: true,
    status: 200,
    json: async () => ({
      token: "fake.jwt.token",
      url: "wss://fake-project.livekit.cloud",
      roomName: "ptai-poc-session-1",
      participantIdentity: "user-1",
    }),
  };
};

const { LiveKitPocEngine, fetchAdminPocLiveKitToken, fetchStudentLiveKitToken } = await import(
  path.join(buildDir, "livekit", "livekitPocEngine.js")
);

function makeCallbackRecorder() {
  const states = [];
  const errors = [];
  const completedTurns = [];
  const diagnostics = [];
  const roomNames = [];
  return {
    states,
    errors,
    completedTurns,
    diagnostics,
    roomNames,
    callbacks: {
      onStateChange: (s) => states.push(s),
      onStudentTranscript: () => {},
      onError: (m) => errors.push(m),
      onTurnCompleted: (n) => completedTurns.push(n),
      onDiagnostics: (d) => diagnostics.push({ ...d }),
      onRoomName: (r) => roomNames.push(r),
    },
  };
}

async function flushMicrotasks() {
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
}

// ---------------------------------------------------------------------------
// Phase C1 helpers: telemetry inspection (voiceDiagnostics.ts ships every
// logVoiceEvent() call to the SAME fetch mock used for the token endpoint
// above, filtered by URL) and manual fake-timer control (the thinking-timeout
// watchdog is armed via the fake `window.setTimeout` at the top of this file,
// whose pendingTimers Map is reused directly here rather than duplicated).
// ---------------------------------------------------------------------------
function telemetryEvents() {
  return fetchCalls
    .filter((c) => c.url.includes("/voice/telemetry"))
    .map((c) => JSON.parse(c.init.body));
}

function latestTimerId() {
  const keys = [...pendingTimers.keys()];
  return keys.at(-1);
}

function fireTimerById(id) {
  const fn = pendingTimers.get(id);
  assert.ok(fn, `expected a pending fake timer with id ${id}`);
  fn();
}

/** A minimal fake HTMLMediaElement for the patient audio track - supports
 * exactly what livekitPocEngine.ts's TrackSubscribed handler touches
 * (autoplay, error/onerror, play()) so audio-diagnostics tests can control
 * whether play() resolves or rejects (autoplay-policy block). */
function makeFakeAudioElement({ playResult } = {}) {
  return {
    autoplay: false,
    error: null,
    onerror: null,
    pause() {},
    play() {
      if (playResult === "reject") {
        const err = new Error("play() was not allowed");
        err.name = "NotAllowedError";
        return Promise.reject(err);
      }
      return Promise.resolve();
    },
  };
}

// ---------------------------------------------------------------------------
// STATE: full turn lifecycle.
// ---------------------------------------------------------------------------
test("STATE: IDLE -> CONNECTING -> LISTENING -> THINKING -> SPEAKING -> LISTENING -> ENDED", async () => {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  assert.equal(engine.getState(), "idle");

  await engine.start("session-1");
  await flushMicrotasks();

  // Token was fetched with the real session id, never a client-chosen room name.
  // (Other fetch calls in this list are fire-and-forget voiceDiagnostics.ts
  // telemetry pings - not part of this assertion.)
  const tokenCalls = fetchCalls.filter((c) => c.url.endsWith("/api/livekit/token"));
  assert.equal(tokenCalls.length, 1);
  assert.equal(JSON.parse(tokenCalls[0].init.body).sessionId, "session-1");

  const room = createdRooms[0];
  assert.ok(room, "engine must construct exactly one Room");
  assert.equal(room.connectCalls.length, 1);
  assert.equal(room.connectCalls[0].url, "wss://fake-project.livekit.cloud");

  assert.deepEqual(rec.states, ["connecting", "listening"]);
  assert.deepEqual(rec.roomNames, ["ptai-poc-session-1"]);
  assert.equal(engine.getState(), "listening");
  assert.equal(rec.diagnostics.at(-1).roomConnected, true);
  assert.equal(rec.diagnostics.at(-1).micPublished, true);

  // Simulate the agent's patient-audio track arriving.
  let attachCalls = 0;
  const fakeTrack = {
    kind: Track.Kind.Audio,
    attach: () => (attachCalls += 1, { pause: () => {}, play: () => Promise.resolve() }),
  };
  room.emit(RoomEvent.TrackSubscribed, fakeTrack);
  assert.equal(attachCalls, 1);
  assert.equal(rec.diagnostics.at(-1).patientTrackSubscribed, true);

  // Simulate the agent process joining.
  room.emit(RoomEvent.ParticipantConnected, { identity: "patient-agent" });
  assert.equal(rec.diagnostics.at(-1).agentConnected, true);

  // Student speaks -> a final transcript is recognized and sent.
  assert.equal(FakeSpeechRecognition.instances.length, 1);
  FakeSpeechRecognition.instances[0].emitFinal("How long has this been going on?");
  await flushMicrotasks();

  assert.equal(engine.getState(), "thinking");
  assert.equal(room.publishedData.length, 1);
  assert.equal(room.publishedData[0].options.topic, "student_text");
  const sentPayload = JSON.parse(new TextDecoder().decode(room.publishedData[0].payload));
  assert.equal(sentPayload.text, "How long has this been going on?");
  assert.ok(sentPayload.clientTurnId);
  const clientTurnId = sentPayload.clientTurnId;

  // Agent signals it started speaking (data channel, not media-element events).
  const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));
  room.emit(RoomEvent.DataReceived, encode({ clientTurnId, status: "speaking_started" }), undefined, undefined, "patient_turn_status");
  assert.equal(engine.getState(), "speaking");

  // A stale/foreign turn status must be ignored.
  room.emit(RoomEvent.DataReceived, encode({ clientTurnId: "not-the-current-turn", status: "speaking_ended" }), undefined, undefined, "patient_turn_status");
  assert.equal(engine.getState(), "speaking");

  // Agent signals the turn is complete.
  room.emit(RoomEvent.DataReceived, encode({ clientTurnId, status: "speaking_ended" }), undefined, undefined, "patient_turn_status");
  assert.equal(engine.getState(), "listening");
  assert.deepEqual(rec.completedTurns, [1]);
  assert.equal(engine.getTurnCount(), 1);
  assert.deepEqual(rec.errors, []);

  await engine.end();
  assert.equal(room.disconnectCalls, 1);
  assert.equal(engine.getState(), "ended");
  assert.equal(rec.diagnostics.at(-1).roomConnected, false);
});

test("STATE: a failed turn surfaces an explicit error, never a silent fallback", async () => {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-2");
  await flushMicrotasks();

  const room = createdRooms[0];
  FakeSpeechRecognition.instances[0].emitFinal("Any numbness in your foot?");
  await flushMicrotasks();
  const sentPayload = JSON.parse(new TextDecoder().decode(room.publishedData[0].payload));

  const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));
  room.emit(
    RoomEvent.DataReceived,
    encode({ clientTurnId: sentPayload.clientTurnId, status: "failed" }),
    undefined,
    undefined,
    "patient_turn_status",
  );

  assert.equal(engine.getState(), "error");
  assert.equal(rec.errors.length, 1);
  assert.match(rec.errors[0], /failed/i);
  await engine.end();
});

// ---------------------------------------------------------------------------
// REGRESSION: 10 consecutive turns, each getting its own fresh recognizer.
//
// Reproduces the exact real-device bug this test guards against: turn 1
// worked, but nothing ever restarted recognition after speaking_ended, and
// even a naive "restart the same instance" fix would silently swallow every
// later final transcript (browser SpeechRecognition + createRecognizer's
// "finalDelivered" latch deliver at most one final per instance - see
// speechRecognitionService.ts). This test proves BOTH halves of the fix:
// a NEW instance is created for every listening cycle, and a stale/retired
// instance can never trigger a later turn.
// ---------------------------------------------------------------------------
test("STATE: 10 consecutive turns - fresh recognizer per cycle, no dupes, no stale triggers", async () => {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-10turns");
  await flushMicrotasks();

  const room = createdRooms[0];
  const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));
  const usedInstances = [];

  for (let turn = 1; turn <= 10; turn += 1) {
    // (a) every listening cycle gets a NEW SpeechRecognition instance.
    assert.equal(engine.getState(), "listening", `turn ${turn}: engine must be listening before speaking`);
    const activeInstance = FakeSpeechRecognition.instances.at(-1);
    assert.ok(activeInstance, `turn ${turn}: a recognizer instance must exist`);
    assert.ok(
      !usedInstances.includes(activeInstance),
      `turn ${turn}: recognizer instance was reused from an earlier turn`,
    );
    usedInstances.push(activeInstance);

    const publishedBefore = room.publishedData.filter((p) => p.options.topic === "student_text").length;

    // (b) each student final transcript is received exactly once.
    activeInstance.emitFinal(`Question number ${turn}?`);
    await flushMicrotasks();

    const studentTextPublishes = room.publishedData.filter((p) => p.options.topic === "student_text");
    assert.equal(
      studentTextPublishes.length, publishedBefore + 1,
      `turn ${turn}: exactly one student_text message must be published`,
    );

    // (c) state transitions LISTENING -> THINKING -> SPEAKING -> LISTENING.
    assert.equal(engine.getState(), "thinking", `turn ${turn}: must enter thinking after speech`);
    const sentPayload = JSON.parse(new TextDecoder().decode(studentTextPublishes.at(-1).payload));
    assert.equal(sentPayload.text, `Question number ${turn}?`);
    const clientTurnId = sentPayload.clientTurnId;

    room.emit(RoomEvent.DataReceived, encode({ clientTurnId, status: "speaking_started" }), undefined, undefined, "patient_turn_status");
    assert.equal(engine.getState(), "speaking", `turn ${turn}: must enter speaking on speaking_started`);

    room.emit(RoomEvent.DataReceived, encode({ clientTurnId, status: "speaking_ended" }), undefined, undefined, "patient_turn_status");
    assert.equal(engine.getState(), "listening", `turn ${turn}: must return to listening on speaking_ended`);

    // (d) patient turn count increments exactly once per turn.
    assert.equal(engine.getTurnCount(), turn, `turn ${turn}: turn count must increment exactly once`);
    assert.deepEqual(rec.completedTurns, Array.from({ length: turn }, (_, i) => i + 1));

    // No manual restart/click needed: a fresh recognizer is already active,
    // synchronously, by the time speaking_ended's handler returns.
    const nextInstance = FakeSpeechRecognition.instances.at(-1);
    assert.notEqual(nextInstance, activeInstance, `turn ${turn}: a fresh recognizer must exist after speaking_ended`);
  }

  // Exactly 10 distinct recognizer instances were used (this turn's + one
  // extra created after turn 10's speaking_ended, still unused).
  assert.equal(new Set(usedInstances).size, 10, "all 10 turns must have used distinct recognizer instances");
  assert.equal(rec.errors.length, 0, "no errors across 10 consecutive turns");

  // (e) explicit regression: the recognizer instance used on turn N is NOT
  // reused for turn N+1, and a stale instance can never trigger a later turn.
  const turn5Instance = usedInstances[4];
  const turn6Instance = usedInstances[5];
  assert.notEqual(turn5Instance, turn6Instance, "turn 5 and turn 6 must use different recognizer instances");

  const publishedBeforeStaleAttempt = room.publishedData.filter((p) => p.options.topic === "student_text").length;
  turn5Instance.emitFinal("A stale turn 5 recognizer trying to speak after turn 6 has already started");
  await flushMicrotasks();
  const publishedAfterStaleAttempt = room.publishedData.filter((p) => p.options.topic === "student_text").length;
  assert.equal(
    publishedAfterStaleAttempt, publishedBeforeStaleAttempt,
    "a stale/retired recognizer instance must never trigger a new turn",
  );
  assert.equal(engine.getTurnCount(), 10, "turn count must be unaffected by a stale recognizer firing late");

  await engine.end();
});

// ===========================================================================
// Phase B: token-source injection (admin POC vs real student InterviewPage)
// and the public sendText() entry point (typed input while LiveKit mode is
// active - see useLiveKitInterviewVoice.ts's submitExternal).
// ===========================================================================

test("PHASE B: fetchAdminPocLiveKitToken posts to the admin-only endpoint (regression guard for LiveKitTestPage.tsx)", async () => {
  fetchCalls.length = 0;
  await fetchAdminPocLiveKitToken("session-admin-1");
  const calls = fetchCalls.filter((c) => c.url.includes("/livekit/token") || c.url.includes("livekit-token"));
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.endsWith("/api/livekit/token"), calls[0].url);
  assert.equal(JSON.parse(calls[0].init.body).sessionId, "session-admin-1");
});

test("PHASE B: fetchStudentLiveKitToken posts to the student-safe interviews endpoint, never the admin one", async () => {
  fetchCalls.length = 0;
  await fetchStudentLiveKitToken("session-student-1");
  const calls = fetchCalls.filter((c) => c.url.includes("/livekit/token") || c.url.includes("livekit-token"));
  assert.equal(calls.length, 1);
  assert.ok(
    calls[0].url.endsWith("/api/interviews/session-student-1/livekit-token"),
    calls[0].url,
  );
  assert.ok(!calls[0].url.includes("/api/livekit/token"), "must never hit the admin-only endpoint");
});

test("PHASE B: engine.start() uses an INJECTED token fetcher instead of the default admin one - same engine serves both POC and real interview", async () => {
  createdRooms.length = 0;
  fetchCalls.length = 0;

  const injectedCalls = [];
  const customFetchToken = async (sessionId) => {
    injectedCalls.push(sessionId);
    return {
      token: "fake.jwt.token", url: "wss://fake-project.livekit.cloud",
      roomName: `ptai-interview-${sessionId}`, participantIdentity: "user-1",
    };
  };

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("real-session-1", customFetchToken);
  await flushMicrotasks();

  assert.deepEqual(injectedCalls, ["real-session-1"], "the injected fetcher must be called, not the default");
  assert.deepEqual(rec.roomNames, ["ptai-interview-real-session-1"]);
  // The global fetch mock (used by the DEFAULT admin fetcher) must never
  // have been hit for the token itself - only the injected function was.
  const tokenLikeCalls = fetchCalls.filter((c) => c.url.includes("livekit-token") || c.url.includes("/livekit/token"));
  assert.equal(tokenLikeCalls.length, 0);

  await engine.end();
});

test("PHASE B: a failing injected token fetcher still surfaces a clean error state (not just the default fetcher's error path)", async () => {
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const failingFetch = async () => {
    throw new Error("network down");
  };
  await engine.start("real-session-2", failingFetch);
  assert.equal(engine.getState(), "error");
  assert.equal(rec.errors.length, 1);
});

test("PHASE B: sendText() is public and sends student_text while listening (typed input, no speech required)", async () => {
  createdRooms.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-typed-1");
  await flushMicrotasks();
  const room = createdRooms[0];

  await engine.sendText("What brings you in today?");
  await flushMicrotasks();

  const studentTextCalls = room.publishedData.filter((p) => p.options.topic === "student_text");
  assert.equal(studentTextCalls.length, 1);
  const payload = JSON.parse(new TextDecoder().decode(studentTextCalls[0].payload));
  assert.equal(payload.text, "What brings you in today?");
  assert.equal(engine.getState(), "thinking");

  await engine.end();
});

test("PHASE B: sendText() is a no-op while not listening (thinking/speaking) - no barge-in via typed input either", async () => {
  createdRooms.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-typed-2");
  await flushMicrotasks();
  const room = createdRooms[0];

  await engine.sendText("First question");
  await flushMicrotasks();
  assert.equal(engine.getState(), "thinking");
  const countAfterFirst = room.publishedData.filter((p) => p.options.topic === "student_text").length;

  // Attempting a SECOND send while still "thinking" must be dropped, exactly
  // like a spoken utterance would be (see the recognizer's own guard).
  await engine.sendText("Second question while still thinking");
  await flushMicrotasks();
  const countAfterSecond = room.publishedData.filter((p) => p.options.topic === "student_text").length;
  assert.equal(countAfterSecond, countAfterFirst, "a typed message sent while not listening must be dropped");

  await engine.end();
});

// ===========================================================================
// Phase C1: proven timeout-state bug fix + readiness/timeout telemetry (see
// the 4-device production-test inspection - server logs proved all four
// sessions succeeded end-to-end, yet two devices showed "no response from
// the agent", so this phase makes the client-side turn/timeout lifecycle
// fully reconstructable from logs alone, plus fixes the one PROVEN client
// bug found along the way: a late speaking_started for an already-timed-out
// turn could silently move ERROR back to SPEAKING).
// ===========================================================================

test("PHASE C1: a late speaking_started after the thinking-timeout fires can never move ERROR back to SPEAKING (proven bug fix)", async () => {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-timeout-1");
  await flushMicrotasks();
  const room = createdRooms[0];

  FakeSpeechRecognition.instances[0].emitFinal("Does it hurt when I press here?");
  await flushMicrotasks();
  assert.equal(engine.getState(), "thinking");

  const sentPayload = JSON.parse(new TextDecoder().decode(room.publishedData.at(-1).payload));
  const clientTurnId = sentPayload.clientTurnId;

  fireTimerById(latestTimerId());
  assert.equal(engine.getState(), "error");
  assert.equal(rec.errors.length, 1);

  // The agent's response finally arrives AFTER the timeout already fired -
  // this is EXACTLY the scenario proven from server logs (all four sessions
  // succeeded server-side; two devices' UI still showed the timeout error).
  const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));
  room.emit(
    RoomEvent.DataReceived,
    encode({ clientTurnId, status: "speaking_started" }),
    undefined, undefined, "patient_turn_status",
  );

  assert.equal(
    engine.getState(), "error",
    "a late speaking_started for a timed-out turn must never move ERROR -> SPEAKING",
  );
  assert.equal(rec.errors.length, 1, "the ignored late message must not add a second error");

  await engine.end();
});

test("PHASE C1: a matched speaking_started cancels the thinking watchdog", async () => {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-timeout-2");
  await flushMicrotasks();
  const room = createdRooms[0];

  FakeSpeechRecognition.instances[0].emitFinal("Any fever?");
  await flushMicrotasks();
  const sentPayload = JSON.parse(new TextDecoder().decode(room.publishedData.at(-1).payload));
  const watchdogId = latestTimerId();

  const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));
  room.emit(
    RoomEvent.DataReceived,
    encode({ clientTurnId: sentPayload.clientTurnId, status: "speaking_started" }),
    undefined, undefined, "patient_turn_status",
  );
  assert.equal(engine.getState(), "speaking");
  assert.ok(!pendingTimers.has(watchdogId), "the watchdog timer must be cleared once its turn is matched");

  await engine.end();
});

test("PHASE C1: a mismatched clientTurnId status message does not cancel the thinking watchdog", async () => {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-timeout-3");
  await flushMicrotasks();
  const room = createdRooms[0];

  FakeSpeechRecognition.instances[0].emitFinal("Any swelling?");
  await flushMicrotasks();
  const watchdogId = latestTimerId();

  const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));
  room.emit(
    RoomEvent.DataReceived,
    encode({ clientTurnId: "some-other-turn", status: "speaking_started" }),
    undefined, undefined, "patient_turn_status",
  );

  assert.equal(engine.getState(), "thinking", "a foreign turn's status must not move state out of thinking");
  assert.ok(pendingTimers.has(watchdogId), "the watchdog must remain armed for a mismatched turn id");

  fireTimerById(watchdogId);
  assert.equal(engine.getState(), "error", "the watchdog must still fire normally for the actual pending turn");

  await engine.end();
});

test("PHASE C1: telemetry captures the full thinking-timeout arm/cancel/fire lifecycle", async () => {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-telemetry-1");
  await flushMicrotasks();
  const room = createdRooms[0];
  const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));

  // Turn 1 completes normally: started, then cancelled (matched) - never fired.
  FakeSpeechRecognition.instances.at(-1).emitFinal("Turn one");
  await flushMicrotasks();
  const payload1 = JSON.parse(new TextDecoder().decode(room.publishedData.at(-1).payload));
  room.emit(RoomEvent.DataReceived, encode({ clientTurnId: payload1.clientTurnId, status: "speaking_started" }), undefined, undefined, "patient_turn_status");
  room.emit(RoomEvent.DataReceived, encode({ clientTurnId: payload1.clientTurnId, status: "speaking_ended" }), undefined, undefined, "patient_turn_status");
  await flushMicrotasks();

  let events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_thinking_timeout_started"), "turn 1: watchdog must be logged as armed");
  assert.ok(events.includes("livekit_thinking_timeout_cancelled"), "turn 1: watchdog must be logged as cancelled");
  assert.ok(!events.includes("livekit_thinking_timeout_fired"), "turn 1 completed normally - the watchdog must never fire");

  // Turn 2 times out: started, then fired - plus the failure + catch-all error events.
  fetchCalls.length = 0;
  FakeSpeechRecognition.instances.at(-1).emitFinal("Turn two");
  await flushMicrotasks();
  fireTimerById(latestTimerId());
  await flushMicrotasks();

  events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_thinking_timeout_started"), "turn 2: watchdog must be logged as armed");
  assert.ok(events.includes("livekit_thinking_timeout_fired"), "turn 2: watchdog must be logged as fired");
  assert.ok(events.includes("livekit_patient_audio_failed"), "turn 2: the specific failure category must be logged");
  assert.ok(events.includes("livekit_engine_error"), "turn 2: the generic catch-all error event must be logged");

  await engine.end();
});

test("PHASE C1: DataReceived telemetry distinguishes received vs matched vs ignored - logged before any parsing/correlation filtering", async () => {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-datareceived-1");
  await flushMicrotasks();
  const room = createdRooms[0];
  const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));

  FakeSpeechRecognition.instances[0].emitFinal("Question");
  await flushMicrotasks();
  const payload = JSON.parse(new TextDecoder().decode(room.publishedData.at(-1).payload));
  const clientTurnId = payload.clientTurnId;

  // (a) malformed payload -> received, then ignored(parse_error) - never matched.
  fetchCalls.length = 0;
  room.emit(RoomEvent.DataReceived, new TextEncoder().encode("not json"), undefined, undefined, "patient_turn_status");
  await flushMicrotasks();
  assert.deepEqual(
    telemetryEvents().map((e) => e.event),
    ["livekit_turn_status_received", "livekit_turn_status_ignored"],
  );

  // (b) mismatched clientTurnId -> received, then ignored(mismatch).
  fetchCalls.length = 0;
  room.emit(RoomEvent.DataReceived, encode({ clientTurnId: "foreign-turn", status: "speaking_started" }), undefined, undefined, "patient_turn_status");
  await flushMicrotasks();
  assert.deepEqual(
    telemetryEvents().map((e) => e.event),
    ["livekit_turn_status_received", "livekit_turn_status_ignored"],
  );

  // (c) matched -> received, then matched, then the specific status event.
  fetchCalls.length = 0;
  room.emit(RoomEvent.DataReceived, encode({ clientTurnId, status: "speaking_started" }), undefined, undefined, "patient_turn_status");
  await flushMicrotasks();
  const matchedEventNames = telemetryEvents().map((e) => e.event);
  assert.equal(matchedEventNames[0], "livekit_turn_status_received");
  assert.equal(matchedEventNames[1], "livekit_turn_status_matched");
  assert.ok(matchedEventNames.includes("livekit_patient_audio_started"));

  await engine.end();
});

test("PHASE C1: an unsupported turn-status value is logged as ignored, not treated as a failure", async () => {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-unsupported-1");
  await flushMicrotasks();
  const room = createdRooms[0];
  const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));

  FakeSpeechRecognition.instances[0].emitFinal("Question");
  await flushMicrotasks();
  const payload = JSON.parse(new TextDecoder().decode(room.publishedData.at(-1).payload));

  fetchCalls.length = 0;
  room.emit(
    RoomEvent.DataReceived,
    encode({ clientTurnId: payload.clientTurnId, status: "some_future_status" }),
    undefined, undefined, "patient_turn_status",
  );
  await flushMicrotasks();

  assert.equal(engine.getState(), "thinking", "an unrecognized status must not move state out of thinking");
  assert.equal(rec.errors.length, 0, "an unrecognized status must not surface as an error");
  const events = telemetryEvents();
  const ignoredEvent = events.find((e) => e.event === "livekit_turn_status_ignored");
  assert.ok(ignoredEvent, "an unsupported status must still be logged as ignored");

  await engine.end();
});

test("PHASE C1: telemetry never carries patient/student text, even for a distinctive spoken phrase", async () => {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-notext-1");
  await flushMicrotasks();
  const room = createdRooms[0];

  const secretPhrase = "I have had crushing chest pain radiating to my left arm for three days";
  FakeSpeechRecognition.instances[0].emitFinal(secretPhrase);
  await flushMicrotasks();
  const payload = JSON.parse(new TextDecoder().decode(room.publishedData.at(-1).payload));
  const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));
  room.emit(RoomEvent.DataReceived, encode({ clientTurnId: payload.clientTurnId, status: "speaking_started" }), undefined, undefined, "patient_turn_status");
  room.emit(RoomEvent.DataReceived, encode({ clientTurnId: payload.clientTurnId, status: "speaking_ended" }), undefined, undefined, "patient_turn_status");
  await flushMicrotasks();

  const bodies = fetchCalls.filter((c) => c.url.includes("/voice/telemetry")).map((c) => c.init.body);
  assert.ok(bodies.length > 0, "expected at least one telemetry ping during this turn");
  for (const body of bodies) {
    assert.ok(!body.includes(secretPhrase));
    assert.ok(!body.includes("crushing chest pain"));
    assert.ok(!body.includes("left arm"));
  }

  await engine.end();
});

test("PHASE C1: a successfully playing patient-audio element logs attached + playing, never play_failed", async () => {
  createdRooms.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-audio-1");
  await flushMicrotasks();
  const room = createdRooms[0];

  fetchCalls.length = 0;
  const fakeTrack = { kind: Track.Kind.Audio, attach: () => makeFakeAudioElement() };
  room.emit(RoomEvent.TrackSubscribed, fakeTrack);
  await flushMicrotasks();

  const events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_audio_element_attached"));
  assert.ok(events.includes("livekit_audio_playing"));
  assert.ok(!events.includes("livekit_audio_play_failed"));

  await engine.end();
});

test("PHASE C1: a rejected play() promise (autoplay blocked) logs audio_play_failed with no user-facing error and no fallback UI", async () => {
  createdRooms.length = 0;
  fetchCalls.length = 0;

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-audio-2");
  await flushMicrotasks();
  const room = createdRooms[0];

  fetchCalls.length = 0;
  const fakeTrack = { kind: Track.Kind.Audio, attach: () => makeFakeAudioElement({ playResult: "reject" }) };
  room.emit(RoomEvent.TrackSubscribed, fakeTrack);
  await flushMicrotasks();

  const events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_audio_element_attached"));
  assert.ok(events.includes("livekit_audio_play_failed"));
  assert.ok(!events.includes("livekit_audio_playing"));

  // Deliberately NOT a user-facing error and NOT any fallback (no browser TTS,
  // no "Tap to hear patient") - this phase adds diagnostics only.
  assert.equal(rec.errors.length, 0, "a blocked autoplay must not surface a user-facing error or trigger a fallback");

  await engine.end();
});

test("PHASE C1: the STATIC source scan also covers no browser-TTS-fallback / tap-to-hear UI was introduced", () => {
  const rawSource = fs.readFileSync(
    path.join(repoRoot, "src", "services", "livekit", "livekitPocEngine.ts"),
    "utf8",
  );
  const source = rawSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  for (const forbidden of ["speechSynthesis.", "SpeechSynthesisUtterance", "Tap to hear", "tap-to-hear", "tapToHear"]) {
    assert.ok(!source.includes(forbidden), `livekitPocEngine.ts must never introduce "${forbidden}"`);
  }
});

// ---------------------------------------------------------------------------
// Phase B: useLiveKitInterviewVoice.ts must never reference the legacy
// playback stack - same static-scan technique already proven above for
// livekitPocEngine.ts itself.
// ---------------------------------------------------------------------------
test("STATIC: useLiveKitInterviewVoice.ts source never references speechSynthesis or patientVoiceService", () => {
  const rawSource = fs.readFileSync(
    path.join(repoRoot, "src", "hooks", "useLiveKitInterviewVoice.ts"),
    "utf8",
  );
  const source = rawSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  for (const forbidden of ["speechSynthesis.", "SpeechSynthesisUtterance", "patientVoiceService", "new MediaSource(", "new Audio("]) {
    assert.ok(!source.includes(forbidden), `useLiveKitInterviewVoice.ts must never use "${forbidden}"`);
  }
  // Positive control: it DOES wrap the real, shared LiveKitPocEngine.
  assert.ok(source.includes("LiveKitPocEngine"));
});

// ---------------------------------------------------------------------------
// Phase B: InterviewPage.tsx's engine-selection guards, checked statically.
// This is a TEXT-LEVEL regression guard (this project has no React
// component-rendering test harness - see the Phase B report), not a
// behavioral proof that the guards execute correctly at runtime. It exists
// to catch an accidental removal of a guard, not to replace real-device/
// manual verification of the actual rendered UI.
// ---------------------------------------------------------------------------
test("STATIC (regression guard, not behavioral proof): InterviewPage.tsx textually guards every patientVoiceService call site behind voiceEngine !== \"livekit\"", () => {
  const source = fs.readFileSync(
    path.join(repoRoot, "src", "pages", "InterviewPage.tsx"),
    "utf8",
  );

  // Every cancelPatientSpeech() call must be preceded on the SAME line by
  // the voiceEngine guard, EXCEPT the "Speak patient replies" checkbox
  // toggle - that call site sits inside the Audio Settings panel, which is
  // itself entirely hidden when voiceEngine === "livekit" (checked below),
  // so it can never execute in LiveKit mode despite having no guard of its
  // own on that line.
  const UNGUARDED_BUT_UNREACHABLE = "if (!e.target.checked) cancelPatientSpeech();";
  const cancelLines = source.split("\n").map((l) => l.trim()).filter((l) => l.includes("cancelPatientSpeech()"));
  assert.ok(cancelLines.length >= 3, "expected multiple cancelPatientSpeech() call sites");
  for (const line of cancelLines) {
    if (line === UNGUARDED_BUT_UNREACHABLE) continue;
    assert.ok(
      line.includes('voiceEngine !== "livekit"'),
      `cancelPatientSpeech() call site must be guarded: "${line}"`,
    );
  }

  // The typed-send speakPatientResponse() call must be gated in the same
  // condition as the other legacy-only checks.
  const typedSendGuardIndex = source.indexOf('voiceEnabled && ttsAvailable && caseId && voiceEngine !== "livekit"');
  assert.ok(typedSendGuardIndex !== -1, "typed-send speakPatientResponse() must be gated on voiceEngine !== \"livekit\"");

  // The legacy recovery banner must be gated the same way.
  assert.ok(
    source.includes('recoveryAction && voiceEngine !== "livekit"'),
    "the legacy recovery banner must be hidden when voiceEngine === \"livekit\"",
  );

  // The Audio Settings panel (speak-replies/auto-interrupt/sensitivity - all
  // legacy-only concepts) must also be hidden in LiveKit mode.
  assert.ok(
    source.includes('(ttsAvailable || voice.supported) && voiceEngine !== "livekit"'),
    "the legacy-only Audio Settings panel must be hidden when voiceEngine === \"livekit\"",
  );
});
