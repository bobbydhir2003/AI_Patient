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

const { LiveKitPocEngine } = await import(
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
  const fakeTrack = { kind: Track.Kind.Audio, attach: () => (attachCalls += 1, { pause: () => {} }) };
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
