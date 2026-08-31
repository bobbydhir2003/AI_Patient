/**
 * LiveKit voice engine regression tests for livekitPocEngine.ts.
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
 *   STATE   - the IDLE -> CONNECTING -> WAITING_FOR_AGENT -> LISTENING ->
 *             THINKING -> SPEAKING -> LISTENING -> ENDED transition
 *             sequence, driven by a fake `livekit-client` Room (real SDK
 *             never contacted).
 *   PHASE C - the production reliability protocol: agent-ready handshake,
 *             targeted data messages, turn-delivery ACK + bounded automatic
 *             retry, idempotency-adjacent client behavior, and the four
 *             separate timeout categories - see livekitPocEngine.ts's module
 *             docstring for the confirmed production incident this answers.
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

/**
 * Phase D1: once useLiveKitInterviewVoice.ts (which imports "react") joins
 * this compile, tsc stops appending ".js" to ALL relative import specifiers
 * in the whole compilation unit - including livekitPocEngine.js's own
 * ("../api", "../voiceDiagnostics", etc), which otherwise resolve fine
 * without the hook present. Plain Node ESM cannot resolve an extensionless
 * relative specifier, so this walks the compiled output and appends ".js"
 * to any relative import/export lacking one - a test-build-only fixup, not
 * a change to any production source file or its authored import style.
 */
function fixRelativeImportExtensions(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      fixRelativeImportExtensions(full);
      continue;
    }
    if (!entry.name.endsWith(".js")) continue;
    const original = fs.readFileSync(full, "utf8");
    const fixed = original.replace(
      /(from\s+"|import\s*\(\s*")(\.\.?\/[^".]+)(")/g,
      (match, prefix, spec, suffix) => `${prefix}${spec}.js${suffix}`,
    );
    if (fixed !== original) fs.writeFileSync(full, fixed);
  }
}
fixRelativeImportExtensions(path.join(repoRoot, ".test-build", "livekit"));

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
  for (const forbidden of [
    "new MediaSource(", "new Audio(", "new Blob(", "createObjectURL(", "speechSynthesis.",
    "Tap to hear", "tap-to-hear", "tapToHear",
  ]) {
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
// Fake browser SpeechRecognition (the ONLY STT this voice path uses -
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

/** Phase C2: configurable microphone behavior for the NEXT constructed
 * FakeRoom, consumed exactly once at construction time (so tests can arrange
 * it BEFORE calling engine.start(), which is what actually constructs the
 * room). Receives the 1-based call count so a test can behave differently on
 * a retry (e.g. "fail once, then succeed") while still exercising the SAME
 * room instance, matching Phase C2's requirement 5 ("same LiveKit room").
 * Defaults to resolving immediately, matching pre-Phase-C2 test behavior. */
let nextMicBehavior = null;

class FakeRoom {
  constructor() {
    this._handlers = {};
    this.remoteParticipants = new Map();
    this.connectCalls = [];
    this.disconnectCalls = 0;
    this.publishedData = [];
    this.micCallCount = 0;
    this.micDisableCallCount = 0;
    const micBehavior = nextMicBehavior ?? (() => Promise.resolve());
    nextMicBehavior = null;
    this.localParticipant = {
      setMicrophoneEnabled: async (enabled) => {
        // Only enable(true) calls count as acquisition "attempts" - the
        // engine's own exhausted-retries cleanup fires a fire-and-forget
        // setMicrophoneEnabled(false), which must NOT be mistaken for a
        // third attempt.
        if (enabled === false) {
          this.micDisableCallCount += 1;
          return undefined;
        }
        this.micCallCount += 1;
        return micBehavior(this.micCallCount, enabled);
      },
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
// Phase D1: a minimal fake React runtime, just enough to drive
// useLiveKitInterviewVoice.ts's own hook body directly (useState/useRef/
// useCallback/useEffect only - no JSX, no concurrent scheduling, no
// batching). This repo has no React rendering/hook-testing harness (no
// react-test-renderer/@testing-library, see package.json), so this proves
// the Phase D1 stale-callback fix BEHAVIORALLY - not just via source-scan -
// using the SAME real LiveKitPocEngine + FakeRoom already set up above.
// ---------------------------------------------------------------------------
function createHookTester(hookFn) {
  const stateValues = [];
  const stateSetters = [];
  const refs = [];
  const callbackSlots = [];
  const effectSlots = [];
  let pendingEffects = [];
  let cursor = 0;
  let latest;
  let currentProps;

  function depsChanged(prev, next) {
    if (!prev) return true;
    if (prev.length !== next.length) return true;
    return prev.some((v, i) => !Object.is(v, next[i]));
  }

  function useState(initial) {
    const i = cursor++;
    if (i >= stateValues.length) {
      stateValues[i] = typeof initial === "function" ? initial() : initial;
    }
    if (!stateSetters[i]) {
      stateSetters[i] = (next) => {
        const value = typeof next === "function" ? next(stateValues[i]) : next;
        if (!Object.is(value, stateValues[i])) {
          stateValues[i] = value;
          rerender();
        }
      };
    }
    return [stateValues[i], stateSetters[i]];
  }

  function useRef(initial) {
    const i = cursor++;
    if (i >= refs.length) refs[i] = { current: initial };
    return refs[i];
  }

  function useCallback(fn, deps) {
    const i = cursor++;
    const prev = callbackSlots[i];
    if (prev && !depsChanged(prev.deps, deps)) return prev.fn;
    callbackSlots[i] = { fn, deps };
    return fn;
  }

  function useEffect(create, deps) {
    const i = cursor++;
    const prev = effectSlots[i];
    if (!prev || depsChanged(prev.deps, deps)) {
      pendingEffects.push(i);
      effectSlots[i] = { deps, cleanup: prev ? prev.cleanup : undefined };
    }
    effectSlots[i]._pendingCreate = create;
  }

  function flushEffects() {
    const queue = pendingEffects;
    pendingEffects = [];
    for (const i of queue) {
      const slot = effectSlots[i];
      if (slot.cleanup) slot.cleanup();
      const cleanup = slot._pendingCreate();
      slot.cleanup = typeof cleanup === "function" ? cleanup : undefined;
    }
  }

  function render(props) {
    currentProps = props;
    cursor = 0;
    globalThis.__fakeReactHooks = { useState, useRef, useCallback, useEffect };
    latest = hookFn(props);
    flushEffects();
    return latest;
  }

  function rerender() {
    render(currentProps);
  }

  return {
    render,
    getResult: () => latest,
    unmount: () => {
      for (const slot of effectSlots) {
        if (slot && slot.cleanup) slot.cleanup();
      }
    },
  };
}

mock.module("react", {
  exports: {
    useState: (...args) => globalThis.__fakeReactHooks.useState(...args),
    useRef: (...args) => globalThis.__fakeReactHooks.useRef(...args),
    useCallback: (...args) => globalThis.__fakeReactHooks.useCallback(...args),
    useEffect: (...args) => globalThis.__fakeReactHooks.useEffect(...args),
  },
});

// ---------------------------------------------------------------------------
// Fake token endpoint.
// ---------------------------------------------------------------------------
const fetchCalls = [];
// Phase C3: counts only ACTUAL token-mint requests (never telemetry pings,
// which flow through this SAME fetch mock) so tests can prove "every Start
// gets a fresh room" - the first mint keeps the EXACT pre-Phase-C3 room name
// (existing tests assert this literal value), every subsequent mint for the
// SAME default token gets a distinct roomName/connectionId, mirroring the
// real backend's student_room_name(session_id, connection_id) behavior.
let tokenMintCount = 0;
globalThis.fetch = async (url, init) => {
  const urlStr = String(url);
  fetchCalls.push({ url: urlStr, init });
  const isTokenRequest = urlStr.includes("/livekit/token") || urlStr.includes("livekit-token");
  if (!isTokenRequest) {
    return { ok: true, status: 200, json: async () => ({}) };
  }
  tokenMintCount += 1;
  const connectionId = `conn-${tokenMintCount}`;
  return {
    ok: true,
    status: 200,
    json: async () => ({
      token: "fake.jwt.token",
      url: "wss://fake-project.livekit.cloud",
      roomName: tokenMintCount === 1 ? "ptai-poc-session-1" : `ptai-poc-session-1-${connectionId}`,
      participantIdentity: "user-1",
      connectionId,
    }),
  };
};

const { LiveKitPocEngine, fetchAdminPocLiveKitToken, fetchStudentLiveKitToken } = await import(
  path.join(buildDir, "livekit", "livekitPocEngine.js")
);

// Phase D1: the REAL hook, compiled alongside the engine (see
// package.json's test:livekitpoc tsc entry) - exercised through
// createHookTester() above against the SAME FakeRoom/fetch mocks the engine
// tests already use, so the stale-callback fix is proven against real async
// timing, not a stubbed engine.
const { useLiveKitInterviewVoice } = await import(
  path.join(repoRoot, ".test-build", "livekit", "hooks", "useLiveKitInterviewVoice.js")
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
// Protocol/telemetry helpers shared by every test below.
// ---------------------------------------------------------------------------
const encode = (obj) => new TextEncoder().encode(JSON.stringify(obj));

function sendAgentReady(room, extra = {}) {
  room.emit(
    RoomEvent.DataReceived, encode({ type: "agent_ready", ...extra }), undefined, undefined, "agent_control",
  );
}

/** Phase 4 turn-ID sync fix convenience wrapper - identical to sendAgentReady
 * except `semanticTurnControl: true`, matching worker.py's own additive
 * agent_ready field. */
function sendAgentReadySemantic(room) {
  sendAgentReady(room, { semanticTurnControl: true });
}

function sendTurnAck(room, clientTurnId, extra = {}) {
  room.emit(
    RoomEvent.DataReceived, encode({ type: "turn_ack", clientTurnId, ...extra }), undefined, undefined, "agent_control",
  );
}

/** Phase 4 turn-ID sync fix: the server's "a real semantic turn is now
 * processing" signal - see worker.py's _handle_semantic_turn_end. */
function sendSemanticTurnStarted(room, clientTurnId) {
  room.emit(
    RoomEvent.DataReceived, encode({ type: "semantic_turn_started", clientTurnId }), undefined, undefined, "agent_control",
  );
}

function sendSemanticFallback(room, reason = "test_fallback") {
  room.emit(
    RoomEvent.DataReceived, encode({ type: "semantic_fallback", reason }), undefined, undefined, "agent_control",
  );
}

function sendTurnStatus(room, clientTurnId, status) {
  room.emit(RoomEvent.DataReceived, encode({ clientTurnId, status }), undefined, undefined, "patient_turn_status");
}

function studentTextPublishes(room) {
  return room.publishedData.filter((p) => p.options.topic === "student_text");
}

function decodedStudentTextPublishes(room) {
  return studentTextPublishes(room).map((p) => JSON.parse(new TextDecoder().decode(p.payload)));
}

function latestStudentTurnId(room) {
  const calls = studentTextPublishes(room);
  return JSON.parse(new TextDecoder().decode(calls.at(-1).payload)).clientTurnId;
}

/** Phase D2: every interrupt_patient control message this room has
 * received, decoded - reuses the SAME agent_control topic/targeting as
 * agent_ready/turn_ack, never a new channel. */
function interruptPublishes(room) {
  return room.publishedData
    .filter((p) => p.options.topic === "agent_control")
    .map((p) => ({ ...p, body: JSON.parse(new TextDecoder().decode(p.payload)) }))
    .filter((p) => p.body.type === "interrupt_patient");
}

/** Reaches SPEAKING the same way the STATE happy-path test does (agent
 * ready -> student speaks -> ack -> speaking_started) - the baseline setup
 * every Phase D2 interrupt test needs. */
async function reachSpeaking(engine, sessionId, fetchToken) {
  const room = await startReady(engine, sessionId, fetchToken);
  FakeSpeechRecognition.instances.at(-1).emitFinal("How long has this been going on?");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);
  sendTurnAck(room, clientTurnId);
  sendTurnStatus(room, clientTurnId, "speaking_started");
  return { room, clientTurnId };
}

/** Starts the engine AND completes the agent-ready handshake - the baseline
 * setup nearly every test below needs, since sendText()/recognition are
 * gated behind WAITING_FOR_AGENT until agent_ready arrives (Part 1). */
async function startReady(engine, sessionId, fetchToken) {
  await engine.start(sessionId, fetchToken);
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  sendAgentReady(room);
  await flushMicrotasks();
  return room;
}

/** Phase 4 turn-ID sync fix: identical to startReady, but the session
 * advertises semantic turn control as active (semanticTurnControl: true),
 * matching how a real semantic-control-enabled worker.py session's
 * agent_ready looks. */
async function startReadySemantic(engine, sessionId, fetchToken) {
  await engine.start(sessionId, fetchToken);
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  sendAgentReadySemantic(room);
  await flushMicrotasks();
  return room;
}

function telemetryEvents() {
  return fetchCalls
    .filter((c) => c.url.includes("/voice/telemetry"))
    .map((c) => JSON.parse(c.init.body));
}

function latestTimerId() {
  const keys = [...pendingTimers.keys()];
  return keys.at(-1);
}

/** The N most-recently-armed still-pending fake timer ids, oldest first.
 * Phase C2's start() arms the agent-ready watchdog THEN the mic timeout
 * (armAgentReadyWatchdog before runMicrophoneAcquisition, in that order) -
 * used by tests that need to distinguish the two while BOTH are still
 * pending (neither agent_ready nor mic has resolved yet). */
function latestTimerIds(n) {
  const keys = [...pendingTimers.keys()];
  return keys.slice(-n);
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

function resetFixtures() {
  createdRooms.length = 0;
  FakeSpeechRecognition.instances.length = 0;
  fetchCalls.length = 0;
  tokenMintCount = 0;
}

/** Count of fetch calls that were actual token-mint requests (never
 * telemetry pings) - Phase C3 restart/lifecycle tests use this to prove
 * exactly how many fresh tokens were requested. */
function tokenMintCalls() {
  return fetchCalls.filter((c) => c.url.includes("/livekit/token") || c.url.includes("livekit-token"));
}

// ---------------------------------------------------------------------------
// STATE: full turn lifecycle.
// ---------------------------------------------------------------------------
test("STATE: IDLE -> CONNECTING -> WAITING_FOR_AGENT -> LISTENING -> THINKING -> SPEAKING -> LISTENING -> ENDED", async () => {
  resetFixtures();

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

  // Room connected + mic published is NOT enough to reach LISTENING (Part 1).
  assert.deepEqual(rec.states, ["connecting", "waiting_for_agent"]);
  assert.deepEqual(rec.roomNames, ["ptai-poc-session-1"]);
  assert.equal(engine.getState(), "waiting_for_agent");
  assert.equal(rec.diagnostics.at(-1).roomConnected, true);
  assert.equal(rec.diagnostics.at(-1).micPublished, true);
  assert.equal(FakeSpeechRecognition.instances.length, 0, "recognition must not start before agent_ready");

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

  // The agent announces readiness - only NOW does the engine enter LISTENING.
  sendAgentReady(room);
  assert.equal(engine.getState(), "listening");
  assert.equal(FakeSpeechRecognition.instances.length, 1, "recognition starts exactly once agent_ready arrives");

  // Student speaks -> a final transcript is recognized and sent, targeted at
  // the patient agent identity (Part 2) - never a blind broadcast.
  FakeSpeechRecognition.instances[0].emitFinal("How long has this been going on?");
  await flushMicrotasks();

  assert.equal(engine.getState(), "thinking");
  assert.equal(studentTextPublishes(room).length, 1);
  const sentCall = studentTextPublishes(room)[0];
  assert.deepEqual(sentCall.options.destinationIdentities, ["patient-agent"]);
  assert.equal(sentCall.options.reliable, true);
  const sentPayload = JSON.parse(new TextDecoder().decode(sentCall.payload));
  assert.equal(sentPayload.text, "How long has this been going on?");
  assert.ok(sentPayload.clientTurnId);
  const clientTurnId = sentPayload.clientTurnId;

  // Agent acknowledges delivery (Part 3) before doing any OpenAI/TTS work.
  sendTurnAck(room, clientTurnId);
  assert.equal(engine.getState(), "thinking", "still thinking - ack only confirms delivery, not a reply");

  // Agent signals it started speaking (data channel, not media-element events).
  sendTurnStatus(room, clientTurnId, "speaking_started");
  assert.equal(engine.getState(), "speaking");

  // A stale/foreign turn status must be ignored.
  sendTurnStatus(room, "not-the-current-turn", "speaking_ended");
  assert.equal(engine.getState(), "speaking");

  // Agent signals the turn is complete.
  sendTurnStatus(room, clientTurnId, "speaking_ended");
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
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-2");

  FakeSpeechRecognition.instances[0].emitFinal("Any numbness in your foot?");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);

  sendTurnAck(room, clientTurnId);
  sendTurnStatus(room, clientTurnId, "failed");

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
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-10turns");

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

    const publishedBefore = studentTextPublishes(room).length;

    // (b) each student final transcript is received exactly once.
    activeInstance.emitFinal(`Question number ${turn}?`);
    await flushMicrotasks();

    const studentTextCalls = studentTextPublishes(room);
    assert.equal(
      studentTextCalls.length, publishedBefore + 1,
      `turn ${turn}: exactly one student_text message must be published`,
    );

    // (c) state transitions LISTENING -> THINKING -> SPEAKING -> LISTENING.
    assert.equal(engine.getState(), "thinking", `turn ${turn}: must enter thinking after speech`);
    const sentPayload = JSON.parse(new TextDecoder().decode(studentTextCalls.at(-1).payload));
    assert.equal(sentPayload.text, `Question number ${turn}?`);
    const clientTurnId = sentPayload.clientTurnId;

    sendTurnAck(room, clientTurnId);
    sendTurnStatus(room, clientTurnId, "speaking_started");
    assert.equal(engine.getState(), "speaking", `turn ${turn}: must enter speaking on speaking_started`);

    sendTurnStatus(room, clientTurnId, "speaking_ended");
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

  const publishedBeforeStaleAttempt = studentTextPublishes(room).length;
  turn5Instance.emitFinal("A stale turn 5 recognizer trying to speak after turn 6 has already started");
  await flushMicrotasks();
  const publishedAfterStaleAttempt = studentTextPublishes(room).length;
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
  resetFixtures();

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
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-typed-1");

  await engine.sendText("What brings you in today?");
  await flushMicrotasks();

  const studentTextCalls = studentTextPublishes(room);
  assert.equal(studentTextCalls.length, 1);
  const payload = JSON.parse(new TextDecoder().decode(studentTextCalls[0].payload));
  assert.equal(payload.text, "What brings you in today?");
  assert.equal(engine.getState(), "thinking");

  await engine.end();
});

test("PHASE B: sendText() is a no-op while not listening (thinking/speaking) - no barge-in via typed input either", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-typed-2");

  await engine.sendText("First question");
  await flushMicrotasks();
  assert.equal(engine.getState(), "thinking");
  const countAfterFirst = studentTextPublishes(room).length;

  // Attempting a SECOND send while still "thinking" must be dropped, exactly
  // like a spoken utterance would be (see the recognizer's own guard).
  await engine.sendText("Second question while still thinking");
  await flushMicrotasks();
  const countAfterSecond = studentTextPublishes(room).length;
  assert.equal(countAfterSecond, countAfterFirst, "a typed message sent while not listening must be dropped");

  await engine.end();
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

  // Phase C product requirement: no student-facing retry button for LiveKit.
  assert.ok(
    source.includes('retryDisabled={voiceEngine === "livekit"}'),
    "ConversationControl must be told to suppress its retry action in LiveKit mode",
  );
});

// ===========================================================================
// PHASE C: production reliability protocol - agent-ready handshake, targeted
// data messages, turn-delivery ACK + bounded automatic retry, and separate
// timeout categories. See livekitPocEngine.ts's module docstring for the
// confirmed production incident (a student_text packet could be published
// and resolve successfully while reaching zero recipients, because the
// agent had not yet joined the room) this protocol answers.
// ===========================================================================

// --------------------------------------------------------------- Part 1: agent-ready handshake

test("PHASE C: sendText() is a no-op while WAITING_FOR_AGENT (before agent_ready)", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-waiting-1");
  await flushMicrotasks();
  const room = createdRooms.at(-1);

  assert.equal(engine.getState(), "waiting_for_agent");
  await engine.sendText("Can you hear me?");
  await flushMicrotasks();

  assert.equal(studentTextPublishes(room).length, 0, "no turn may be sent before the agent is ready");
  assert.equal(engine.getState(), "waiting_for_agent");

  await engine.end();
});

test("PHASE C: recognition does not start until agent_ready is received", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-waiting-2");
  await flushMicrotasks();
  assert.equal(FakeSpeechRecognition.instances.length, 0);

  const room = createdRooms.at(-1);
  sendAgentReady(room);
  assert.equal(FakeSpeechRecognition.instances.length, 1);

  await engine.end();
});

test("PHASE C: agent_ready transitions WAITING_FOR_AGENT -> LISTENING and enables sendText", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-waiting-3");

  assert.equal(engine.getState(), "listening");
  await engine.sendText("Now I can speak");
  await flushMicrotasks();
  assert.equal(studentTextPublishes(room).length, 1);

  await engine.end();
});

test("PHASE C: a late/duplicate agent_ready after already listening is ignored", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-waiting-4");

  const statesBefore = rec.states.length;
  sendAgentReady(room); // duplicate
  assert.equal(rec.states.length, statesBefore, "a duplicate agent_ready must not re-trigger a state transition");
  assert.equal(engine.getState(), "listening");

  await engine.end();
});

test("PHASE C: agent_ready never arriving surfaces an internal agent_not_ready error, not a manual dead end", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-noready-1");
  await flushMicrotasks();
  assert.equal(engine.getState(), "waiting_for_agent");

  fireTimerById(latestTimerId()); // the agent-ready watchdog
  assert.equal(engine.getState(), "error");
  assert.equal(rec.errors.length, 1);

  const events = telemetryEvents().filter((e) => e.event === "livekit_engine_error");
  assert.ok(events.some((e) => e.reason === "agent_not_ready"));

  await engine.end();
});

// --------------------------------------------------------------- Part 2: targeted data messages

test("PHASE C: student_text is always targeted at the patient agent identity, never a blind broadcast", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-targeted-1");

  await engine.sendText("Targeted turn");
  await flushMicrotasks();

  const call = studentTextPublishes(room)[0];
  assert.deepEqual(call.options.destinationIdentities, ["patient-agent"]);
  assert.equal(call.options.reliable, true);
  assert.equal(call.options.topic, "student_text");

  await engine.end();
});

// --------------------------------------------------------------- Part 3/4: turn-ACK + automatic retry

test("PHASE C: publishData resolving alone does not count as delivered - the engine stays awaiting ack", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-ack-1");

  await engine.sendText("Are you there?");
  await flushMicrotasks(); // publishData's promise has resolved by now

  const events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_turn_publish_started"));
  assert.ok(events.includes("livekit_turn_publish_resolved"));
  assert.ok(!events.includes("livekit_turn_ack_received"), "publish resolving must never itself count as an ack");
  assert.equal(engine.getState(), "thinking", "still awaiting ack - no progress beyond publishData resolving");

  await engine.end();
});

test("PHASE C: turn_ack cancels the delivery watchdog and arms the processing watchdog", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-ack-2");

  await engine.sendText("Question");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);
  const deliveryWatchdogId = latestTimerId();

  sendTurnAck(room, clientTurnId);

  assert.ok(!pendingTimers.has(deliveryWatchdogId), "the delivery watchdog must be cleared once acked");
  const processingWatchdogId = latestTimerId();
  assert.notEqual(processingWatchdogId, deliveryWatchdogId, "a NEW (processing) watchdog must now be armed");
  assert.ok(pendingTimers.has(processingWatchdogId));

  const events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_turn_ack_received"));
  assert.ok(events.includes("livekit_thinking_timeout_started"));

  await engine.end();
});

test("PHASE C: missing turn_ack triggers an automatic resend using the SAME clientTurnId, with no UI prompt", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-retry-1");

  await engine.sendText("Please respond");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);
  assert.equal(studentTextPublishes(room).length, 1);

  fireTimerById(latestTimerId()); // delivery watchdog times out - no ack ever arrived
  await flushMicrotasks();

  const resent = studentTextPublishes(room);
  assert.equal(resent.length, 2, "exactly one automatic resend after a single missed ack");
  const secondPayload = JSON.parse(new TextDecoder().decode(resent[1].payload));
  assert.equal(secondPayload.clientTurnId, clientTurnId, "the resend must reuse the SAME clientTurnId");
  assert.equal(secondPayload.text, "Please respond", "the resend must carry the identical text");

  // Purely internal - never surfaced as a user-facing error while retries remain.
  assert.equal(rec.errors.length, 0, "a single missed ack with retries remaining must not surface an error");
  assert.equal(engine.getState(), "thinking");

  const events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_turn_ack_timeout"));
  assert.ok(events.includes("livekit_turn_auto_retry"));

  await engine.end();
});

test("PHASE C: delivery retries are bounded, then produce an explicit internal error - no infinite retry loop", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-retry-2");

  await engine.sendText("Anyone there?");
  await flushMicrotasks();

  // Never ack - fire the delivery watchdog repeatedly until it gives up.
  for (let attempt = 0; attempt < 5 && engine.getState() !== "error"; attempt += 1) {
    fireTimerById(latestTimerId());
    await flushMicrotasks();
  }

  assert.equal(engine.getState(), "error", "bounded retries must eventually give up, not retry forever");
  // Exactly ONE original publish + MAX_DELIVERY_RETRIES (2) resends = 3 total.
  assert.equal(studentTextPublishes(room).length, 3);
  assert.equal(rec.errors.length, 1, "exactly one user-facing error once retries are exhausted");

  const events = telemetryEvents();
  const retryEvents = events.filter((e) => e.event === "livekit_turn_auto_retry");
  assert.equal(retryEvents.length, 2, "exactly MAX_DELIVERY_RETRIES automatic retries");
  assert.ok(events.some((e) => e.event === "livekit_turn_delivery_failed"));
  assert.ok(events.some((e) => e.event === "livekit_engine_error" && e.reason === "turn_delivery_failed"));

  await engine.end();
});

test("PHASE C: an ack that arrives after retries already started still completes the turn normally", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-retry-3");

  await engine.sendText("Slow agent");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);

  fireTimerById(latestTimerId()); // one missed ack -> one automatic retry
  await flushMicrotasks();
  assert.equal(studentTextPublishes(room).length, 2);

  // The (retried) message's ack finally arrives.
  sendTurnAck(room, clientTurnId);
  sendTurnStatus(room, clientTurnId, "speaking_started");
  sendTurnStatus(room, clientTurnId, "speaking_ended");

  assert.equal(engine.getState(), "listening");
  assert.equal(engine.getTurnCount(), 1);
  assert.equal(rec.errors.length, 0);

  await engine.end();
});

// --------------------------------------------------------------- Part 6: separate timeout categories

test("PHASE C: a late speaking_started after the processing-timeout fires can never move ERROR back to SPEAKING (proven bug fix)", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-timeout-1");

  FakeSpeechRecognition.instances[0].emitFinal("Does it hurt when I press here?");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);
  sendTurnAck(room, clientTurnId); // move past delivery - now awaiting processing
  assert.equal(engine.getState(), "thinking");

  fireTimerById(latestTimerId()); // the processing (thinking) watchdog
  assert.equal(engine.getState(), "error");
  assert.equal(rec.errors.length, 1);

  // The agent's response finally arrives AFTER the timeout already fired -
  // this is EXACTLY the scenario proven from server logs in a prior
  // production incident (server succeeded end-to-end; the client still
  // showed the timeout error).
  sendTurnStatus(room, clientTurnId, "speaking_started");

  assert.equal(
    engine.getState(), "error",
    "a late speaking_started for a timed-out turn must never move ERROR -> SPEAKING",
  );
  assert.equal(rec.errors.length, 1, "the ignored late message must not add a second error");

  await engine.end();
});

test("PHASE C: a mismatched clientTurnId status message does not cancel the processing watchdog", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-timeout-2");

  FakeSpeechRecognition.instances[0].emitFinal("Any swelling?");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);
  sendTurnAck(room, clientTurnId);
  const watchdogId = latestTimerId();

  sendTurnStatus(room, "some-other-turn", "speaking_started");

  assert.equal(engine.getState(), "thinking", "a foreign turn's status must not move state out of thinking");
  assert.ok(pendingTimers.has(watchdogId), "the processing watchdog must remain armed for a mismatched turn id");

  fireTimerById(watchdogId);
  assert.equal(engine.getState(), "error", "the watchdog must still fire normally for the actual pending turn");

  await engine.end();
});

test("PHASE C: telemetry captures the full processing-timeout arm/cancel/fire lifecycle", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-telemetry-1");

  // Turn 1 completes normally: started, then cancelled (matched) - never fired.
  FakeSpeechRecognition.instances.at(-1).emitFinal("Turn one");
  await flushMicrotasks();
  const turn1Id = latestStudentTurnId(room);
  sendTurnAck(room, turn1Id);
  sendTurnStatus(room, turn1Id, "speaking_started");
  sendTurnStatus(room, turn1Id, "speaking_ended");

  let events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_thinking_timeout_started"), "turn 1: watchdog must be logged as armed");
  assert.ok(events.includes("livekit_thinking_timeout_cancelled"), "turn 1: watchdog must be logged as cancelled");
  assert.ok(!events.includes("livekit_thinking_timeout_fired"), "turn 1 completed normally - the watchdog must never fire");

  // Turn 2 times out: started, then fired - plus the failure + catch-all error events.
  fetchCalls.length = 0;
  FakeSpeechRecognition.instances.at(-1).emitFinal("Turn two");
  await flushMicrotasks();
  const turn2Id = latestStudentTurnId(room);
  sendTurnAck(room, turn2Id);
  fireTimerById(latestTimerId());
  await flushMicrotasks();

  events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_thinking_timeout_started"), "turn 2: watchdog must be logged as armed");
  assert.ok(events.includes("livekit_thinking_timeout_fired"), "turn 2: watchdog must be logged as fired");
  assert.ok(events.includes("livekit_patient_audio_failed"), "turn 2: the specific failure category must be logged");
  assert.ok(events.includes("livekit_engine_error"), "turn 2: the generic catch-all error event must be logged");

  await engine.end();
});

test("PHASE C: DataReceived telemetry distinguishes received vs matched vs ignored - logged before any parsing/correlation filtering", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-datareceived-1");

  FakeSpeechRecognition.instances[0].emitFinal("Question");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);
  sendTurnAck(room, clientTurnId);

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
  sendTurnStatus(room, "foreign-turn", "speaking_started");
  await flushMicrotasks();
  assert.deepEqual(
    telemetryEvents().map((e) => e.event),
    ["livekit_turn_status_received", "livekit_turn_status_ignored"],
  );

  // (c) matched -> received, then matched, then the specific status event.
  fetchCalls.length = 0;
  sendTurnStatus(room, clientTurnId, "speaking_started");
  await flushMicrotasks();
  const matchedEventNames = telemetryEvents().map((e) => e.event);
  assert.equal(matchedEventNames[0], "livekit_turn_status_received");
  assert.equal(matchedEventNames[1], "livekit_turn_status_matched");
  assert.ok(matchedEventNames.includes("livekit_patient_audio_started"));

  await engine.end();
});

test("PHASE C: an unsupported turn-status value is logged as ignored, not treated as a failure", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-unsupported-1");

  FakeSpeechRecognition.instances[0].emitFinal("Question");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);
  sendTurnAck(room, clientTurnId);

  fetchCalls.length = 0;
  sendTurnStatus(room, clientTurnId, "some_future_status");
  await flushMicrotasks();

  assert.equal(engine.getState(), "thinking", "an unrecognized status must not move state out of thinking");
  assert.equal(rec.errors.length, 0, "an unrecognized status must not surface as an error");
  const events = telemetryEvents();
  const ignoredEvent = events.find((e) => e.event === "livekit_turn_status_ignored");
  assert.ok(ignoredEvent, "an unsupported status must still be logged as ignored");

  await engine.end();
});

test("PHASE C: telemetry never carries patient/student text, even for a distinctive spoken phrase", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-notext-1");

  const secretPhrase = "I have had crushing chest pain radiating to my left arm for three days";
  FakeSpeechRecognition.instances[0].emitFinal(secretPhrase);
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);
  sendTurnAck(room, clientTurnId);
  sendTurnStatus(room, clientTurnId, "speaking_started");
  sendTurnStatus(room, clientTurnId, "speaking_ended");
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

test("PHASE C: a stuck SPEAKING state (no speaking_ended) eventually surfaces audio_transport_failed", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-speaking-timeout-1");

  await engine.sendText("Tell me more");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room);
  sendTurnAck(room, clientTurnId);
  sendTurnStatus(room, clientTurnId, "speaking_started");
  assert.equal(engine.getState(), "speaking");

  fireTimerById(latestTimerId()); // the speaking watchdog - no speaking_ended ever arrived
  assert.equal(engine.getState(), "error");
  assert.equal(rec.errors.length, 1);

  const events = telemetryEvents().filter((e) => e.event === "livekit_engine_error");
  assert.ok(events.some((e) => e.reason === "audio_transport_failed"));

  // A late speaking_ended for the same (now timed-out) turn must be ignored,
  // same invalidate-before-error discipline as every other watchdog here.
  sendTurnStatus(room, clientTurnId, "speaking_ended");
  assert.equal(engine.getState(), "error");
  assert.equal(rec.errors.length, 1);

  await engine.end();
});

// --------------------------------------------------------------- audio-element diagnostics (no fallback UI)

test("PHASE C: a successfully playing patient-audio element logs attached + playing, never play_failed", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-audio-1");

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

test("PHASE C: a rejected play() promise (autoplay blocked) logs audio_play_failed with no user-facing error and no fallback UI", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-audio-2");

  fetchCalls.length = 0;
  const fakeTrack = { kind: Track.Kind.Audio, attach: () => makeFakeAudioElement({ playResult: "reject" }) };
  room.emit(RoomEvent.TrackSubscribed, fakeTrack);
  await flushMicrotasks();

  const events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_audio_element_attached"));
  assert.ok(events.includes("livekit_audio_play_failed"));
  assert.ok(!events.includes("livekit_audio_playing"));

  // Deliberately NOT a user-facing error and NOT any fallback (no browser TTS,
  // no "Tap to hear patient") - this engine adds diagnostics only.
  assert.equal(rec.errors.length, 0, "a blocked autoplay must not surface a user-facing error or trigger a fallback");

  await engine.end();
});

// --------------------------------------------------------------- Part 5-adjacent / isolation

test("PHASE C: two concurrent engine instances (simulated sessions) remain fully isolated", async () => {
  resetFixtures();

  const recA = makeCallbackRecorder();
  const recB = makeCallbackRecorder();
  const engineA = new LiveKitPocEngine(recA.callbacks);
  const engineB = new LiveKitPocEngine(recB.callbacks);

  const roomA = await startReady(engineA, "session-iso-a");
  const roomB = await startReady(engineB, "session-iso-b");
  assert.notEqual(roomA, roomB);

  // Interleave: A sends first, then B sends, then A's turn completes while
  // B's is still awaiting ack, then B's completes - at no point should
  // either engine's state/turnCount reflect the OTHER session's messages.
  await engineA.sendText("Question from session A");
  await flushMicrotasks();
  const turnIdA = latestStudentTurnId(roomA);
  assert.equal(studentTextPublishes(roomB).length, 0, "session B must be unaffected by A sending a turn");
  assert.equal(engineB.getState(), "listening");

  await engineB.sendText("Question from session B");
  await flushMicrotasks();
  const turnIdB = latestStudentTurnId(roomB);
  assert.notEqual(turnIdA, turnIdB);

  sendTurnAck(roomA, turnIdA);
  sendTurnStatus(roomA, turnIdA, "speaking_started");
  sendTurnStatus(roomA, turnIdA, "speaking_ended");
  assert.equal(engineA.getState(), "listening");
  assert.equal(engineA.getTurnCount(), 1);
  // B's turn is still mid-flight (never ack'd yet) - completely unaffected
  // by A finishing its own, unrelated turn on a different room/engine.
  assert.equal(engineB.getState(), "thinking");
  assert.equal(engineB.getTurnCount(), 0);

  sendTurnAck(roomB, turnIdB);
  sendTurnStatus(roomB, turnIdB, "speaking_started");
  sendTurnStatus(roomB, turnIdB, "speaking_ended");
  assert.equal(engineB.getState(), "listening");
  assert.equal(engineB.getTurnCount(), 1);
  assert.equal(engineA.getTurnCount(), 1, "A's count must be unaffected by B finishing its own turn");

  await engineA.end();
  await engineB.end();
});

test("PHASE C: STATIC - ConversationControl suppresses the retry action when retryDisabled is set", () => {
  const rawSource = fs.readFileSync(
    path.join(repoRoot, "src", "components", "interview", "ConversationControl.tsx"),
    "utf8",
  );
  const source = rawSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  assert.ok(source.includes("retryDisabled"), "ConversationControl must accept a retryDisabled prop");
  assert.ok(
    /if\s*\(retryDisabled\)\s*\{[\s\S]*?mainAction\s*=\s*null/.test(source),
    "the ERROR-state branch must null out the main action (no onRetry) when retryDisabled is set",
  );
  for (const forbidden of ["Tap to hear", "tap-to-hear", "tapToHear"]) {
    assert.ok(!source.includes(forbidden), `ConversationControl.tsx must never introduce "${forbidden}"`);
  }
});

// ===========================================================================
// PHASE C2: mobile startup race fix - order-independent microphone + agent
// readiness coordination, bounded mic timeout/retry, and startup-generation
// protection against stale async work. See livekitPocEngine.ts's module
// docstring for the confirmed production incident (iOS stuck indefinitely
// on "Requesting microphone...", with a real agent_ready silently discarded
// because the engine never reached WAITING_FOR_AGENT) this phase answers.
// ===========================================================================

test("PHASE C2 A: agent_ready arrives before mic resolves - remembered, then LISTENING once mic finishes", async () => {
  resetFixtures();
  let resolveMic;
  nextMicBehavior = () => new Promise((resolve) => { resolveMic = resolve; });

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseA");
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  assert.equal(engine.getState(), "waiting_for_agent");

  sendAgentReady(room);
  assert.equal(engine.getState(), "waiting_for_agent", "must not enter LISTENING until mic is also ready");
  assert.ok(telemetryEvents().some((e) => e.event === "livekit_agent_ready_received"));
  assert.equal(FakeSpeechRecognition.instances.length, 0, "recognition must not start on agent_ready alone");

  resolveMic();
  await flushMicrotasks();
  assert.equal(engine.getState(), "listening");
  assert.equal(FakeSpeechRecognition.instances.length, 1, "recognition starts once BOTH signals are in");

  await engine.end();
});

test("PHASE C2 B: mic resolves before agent_ready - waits, then LISTENING once agent_ready arrives", async () => {
  resetFixtures();
  // Default nextMicBehavior (resolves immediately) - deliberately not overridden.
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseB");
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  assert.equal(engine.getState(), "waiting_for_agent", "mic readiness alone must not be enough to enter LISTENING");
  assert.equal(FakeSpeechRecognition.instances.length, 0);
  assert.ok(telemetryEvents().some((e) => e.event === "livekit_mic_ready"));

  sendAgentReady(room);
  assert.equal(engine.getState(), "listening");
  assert.equal(FakeSpeechRecognition.instances.length, 1);

  await engine.end();
});

test("PHASE C2 C: an early agent_ready survives a full microphone retry cycle - never discarded, never needs resending", async () => {
  resetFixtures();
  let resolveSecondAttempt;
  nextMicBehavior = (callCount) => {
    if (callCount === 1) return new Promise(() => {}); // first attempt hangs
    return new Promise((resolve) => { resolveSecondAttempt = resolve; });
  };

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseC");
  await flushMicrotasks();
  const room = createdRooms.at(-1);

  // agent_ready arrives WHILE the (hanging) first mic attempt is still pending.
  sendAgentReady(room);
  assert.equal(engine.getState(), "waiting_for_agent");

  // Time out the first mic attempt -> automatic retry (SAME room).
  fireTimerById(latestTimerId());
  await flushMicrotasks();
  assert.equal(room.micCallCount, 2, "the retry must reuse the SAME room/localParticipant");

  // The retried attempt finally succeeds - LISTENING must follow IMMEDIATELY,
  // without any second agent_ready ever being sent.
  resolveSecondAttempt();
  await flushMicrotasks();
  assert.equal(engine.getState(), "listening", "the early agent_ready must still count after the mic retry cycle");

  await engine.end();
});

test("PHASE C2 D: mic promise times out - automatic retry occurs", async () => {
  resetFixtures();
  nextMicBehavior = (callCount) => (callCount === 1 ? new Promise(() => {}) : Promise.resolve());

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseD");
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  assert.equal(room.micCallCount, 1);

  fireTimerById(latestTimerId()); // mic timeout - armed AFTER the agent-ready watchdog, so it is the latest
  await flushMicrotasks();

  assert.equal(room.micCallCount, 2, "exactly one automatic retry");
  const events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_mic_request_timeout"));
  assert.ok(events.includes("livekit_mic_retry_started"));
  assert.ok(events.includes("livekit_mic_ready"), "the retried attempt succeeded");
  assert.equal(rec.errors.length, 0, "a successful retry must never surface a user-facing error");

  await engine.end();
});

test("PHASE C2 E: first mic attempt rejects - automatic retry occurs", async () => {
  resetFixtures();
  nextMicBehavior = (callCount) =>
    callCount === 1
      ? Promise.reject(Object.assign(new Error("mic busy"), { name: "NotReadableError" }))
      : Promise.resolve();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseE");
  await flushMicrotasks();
  await flushMicrotasks(); // two sequential immediate settlements need extra ticks

  const room = createdRooms.at(-1);
  assert.equal(room.micCallCount, 2, "the rejection must trigger an immediate automatic retry");
  const events = telemetryEvents().map((e) => e.event);
  assert.ok(events.includes("livekit_mic_request_failed"));
  assert.ok(events.includes("livekit_mic_retry_started"));

  await engine.end();
});

test("PHASE C2 F: after a failed first attempt, a successful second attempt reaches LISTENING normally", async () => {
  resetFixtures();
  nextMicBehavior = (callCount) => (callCount === 1 ? Promise.reject(new Error("boom")) : Promise.resolve());

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseF");
  await flushMicrotasks();
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  assert.equal(room.micCallCount, 2);
  assert.equal(engine.getState(), "waiting_for_agent");

  sendAgentReady(room);
  assert.equal(engine.getState(), "listening");

  await engine.end();
});

test("PHASE C2 G: both microphone attempts failing produces an explicit, distinct ERROR - never the generic agent-timeout message", async () => {
  resetFixtures();
  nextMicBehavior = () => Promise.reject(Object.assign(new Error("denied"), { name: "NotAllowedError" }));

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseG");
  await flushMicrotasks();
  await flushMicrotasks();

  const room = createdRooms.at(-1);
  assert.equal(room.micCallCount, 2, "exactly the bounded attempt count (1 + MAX_MIC_RETRIES)");
  assert.equal(engine.getState(), "error");
  assert.equal(rec.errors.length, 1);
  assert.match(rec.errors[0], /microphone/i);
  assert.doesNotMatch(rec.errors[0], /no response from the agent/i);

  const errorEvents = telemetryEvents().filter((e) => e.event === "livekit_engine_error");
  assert.ok(errorEvents.some((e) => e.reason === "microphone_start_failed"));
  await flushMicrotasks();
  assert.equal(room.micDisableCallCount, 1, "must ask the SDK to disable the mic once retries are exhausted");

  await engine.end();
});

test("PHASE C2 H: microphone attempts never overlap - a retry starts only after the prior attempt's outcome is known", async () => {
  resetFixtures();
  nextMicBehavior = (callCount) => (callCount === 1 ? new Promise(() => {}) : Promise.resolve());

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseH");
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  assert.equal(room.micCallCount, 1, "only ONE attempt while the first is still pending - no overlap");

  fireTimerById(latestTimerId());
  await flushMicrotasks();
  assert.equal(room.micCallCount, 2, "the retry starts only after the first attempt's timeout was resolved");

  await engine.end();
});

test("PHASE C2 I: a stale mic completion after end() is ignored (never resurrects/mutates the ended engine)", async () => {
  resetFixtures();
  let resolveMic;
  nextMicBehavior = () => new Promise((resolve) => { resolveMic = resolve; });

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseI");
  await flushMicrotasks();
  assert.equal(engine.getState(), "waiting_for_agent");

  await engine.end();
  assert.equal(engine.getState(), "ended");

  // The "real" getUserMedia()-equivalent finally settles AFTER end().
  resolveMic();
  await flushMicrotasks();
  assert.equal(engine.getState(), "ended", "a stale mic resolution must never move the engine out of ended");

  await engine.end(); // must remain idempotent, must not throw
});

test("PHASE C2 J: a stale agent_ready from a previous startup generation/room is ignored", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);

  await engine.start("session-caseJ-1");
  await flushMicrotasks();
  const oldRoom = createdRooms.at(-1);
  await engine.end();

  await engine.start("session-caseJ-2");
  await flushMicrotasks();
  const newRoom = createdRooms.at(-1);
  assert.notEqual(oldRoom, newRoom);
  assert.equal(engine.getState(), "waiting_for_agent");

  // A late agent_ready arriving on the OLD room (e.g. a message its
  // WebSocket hadn't finished delivering before teardown) must never affect
  // the NEW engine/generation.
  sendAgentReady(oldRoom);
  assert.equal(engine.getState(), "waiting_for_agent", "a stale agent_ready from an old generation must be ignored");

  sendAgentReady(newRoom);
  assert.equal(engine.getState(), "listening", "the CURRENT room's agent_ready must still work normally");

  await engine.end();
});

test("PHASE C2 K: the LISTENING transition happens exactly once even with a duplicate agent_ready right after", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseK");
  await flushMicrotasks(); // mic ready (default behavior)
  const room = createdRooms.at(-1);

  const listeningCountBefore = rec.states.filter((s) => s === "listening").length;
  sendAgentReady(room);
  sendAgentReady(room); // duplicate/late resend - must be a no-op
  const listeningCountAfter = rec.states.filter((s) => s === "listening").length;
  assert.equal(listeningCountAfter, listeningCountBefore + 1, "must transition to listening exactly once");

  await engine.end();
});

test("PHASE C2 L: recognition starts only once BOTH readiness conditions are met, never on either alone", async () => {
  resetFixtures();
  let resolveMic;
  nextMicBehavior = () => new Promise((resolve) => { resolveMic = resolve; });

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await engine.start("session-caseL");
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  assert.equal(FakeSpeechRecognition.instances.length, 0, "neither signal has arrived yet");

  sendAgentReady(room);
  assert.equal(FakeSpeechRecognition.instances.length, 0, "agent_ready alone must not start recognition");

  resolveMic();
  await flushMicrotasks();
  assert.equal(FakeSpeechRecognition.instances.length, 1, "recognition starts once both are satisfied");

  await engine.end();
});

test("PHASE C2: mic acquisition never touches the legacy playback stack (no new Audio/MediaSource/speechSynthesis)", () => {
  const rawSource = fs.readFileSync(
    path.join(repoRoot, "src", "services", "livekit", "livekitPocEngine.ts"),
    "utf8",
  );
  const source = rawSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  for (const forbidden of [
    "new MediaSource(", "new Audio(", "new Blob(", "createObjectURL(", "speechSynthesis.",
    "SpeechSynthesisUtterance", "patientVoiceService",
  ]) {
    assert.ok(!source.includes(forbidden), `livekitPocEngine.ts must never use "${forbidden}"`);
  }
  // Positive control: microphone acquisition still goes through the SAME
  // persistent Room/localParticipant, never a second/parallel media path.
  assert.ok(source.includes("room.localParticipant.setMicrophoneEnabled(true)"));
});

// ===========================================================================
// PHASE C3: unique LiveKit room per intentional voice connection - fixes the
// confirmed Stop/refresh/leave-return restart race (a deterministic room
// name could reconnect to a room still shutting down in LiveKit Cloud,
// silently skipping a fresh worker dispatch/agent_ready). See
// livekit_token_service.py's student_room_name for the backend half; here
// the engine simply threads through whatever connectionId/roomName a fresh
// token response provides, for every Start, with no special-casing of
// "is this a restart" anywhere in this file's code under test.
// ===========================================================================

test("PHASE C3: the engine threads the server-provided connectionId into connection telemetry, never invents it", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await startReady(engine, "session-connid-1");

  const createdEvent = telemetryEvents().find((e) => e.event === "livekit_voice_connection_created");
  assert.ok(createdEvent, "expected a livekit_voice_connection_created telemetry event");
  assert.equal(createdEvent.connectionId, "conn-1");

  const readyEvent = telemetryEvents().find((e) => e.event === "livekit_agent_ready_received");
  assert.equal(readyEvent.connectionId, "conn-1", "connectionId must also appear on other lifecycle events");

  fetchCalls.length = 0;
  await engine.end();
  const endedEvent = telemetryEvents().find((e) => e.event === "livekit_voice_connection_ended");
  assert.ok(endedEvent, "expected a livekit_voice_connection_ended telemetry event");
  assert.equal(endedEvent.connectionId, "conn-1");
});

test("PHASE C3: the frontend never sends anything beyond sessionId when requesting a token - no client-chosen room/connection", () => {
  const rawSource = fs.readFileSync(
    path.join(repoRoot, "src", "services", "livekit", "livekitPocEngine.ts"),
    "utf8",
  );
  const source = rawSource.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
  // postForToken's request body is exactly `{ sessionId }` - proven
  // textually since this is the ONLY place a token request is constructed;
  // there is no code path anywhere in this file that builds a request body
  // containing a room name or connection id.
  assert.ok(source.includes("body: JSON.stringify({ sessionId })"));
});

test("PHASE C3 D: first Start still reaches LISTENING (unaffected by connectionId plumbing)", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-first-start");
  assert.equal(engine.getState(), "listening");
  assert.equal(tokenMintCalls().length, 1);
  assert.equal(room.micCallCount, 1);
  await engine.end();
});

test("PHASE C3 E: Stop then immediate Start again requests a fresh token, gets a NEW room, and reaches LISTENING again", async () => {
  resetFixtures();
  const rec1 = makeCallbackRecorder();
  const engine1 = new LiveKitPocEngine(rec1.callbacks);
  await startReady(engine1, "session-restart-1");
  assert.equal(engine1.getState(), "listening");
  const firstRoomName = rec1.roomNames[0];

  await engine1.end();
  assert.equal(engine1.getState(), "ended");

  // Start #2: a BRAND-NEW engine instance, exactly matching
  // useLiveKitInterviewVoice.ts's buildEngine() pattern (the hook never
  // reuses an engine across Stop -> Start).
  const rec2 = makeCallbackRecorder();
  const engine2 = new LiveKitPocEngine(rec2.callbacks);
  await startReady(engine2, "session-restart-1");
  assert.equal(engine2.getState(), "listening");
  const secondRoomName = rec2.roomNames[0];

  assert.equal(tokenMintCalls().length, 2, "a fresh token must be requested for every Start");
  assert.notEqual(firstRoomName, secondRoomName, "Start #2 must get a DIFFERENT room than Start #1");
  assert.equal(createdRooms.length, 2, "two separate Room objects were constructed - no room reuse");

  const readyEvents = telemetryEvents().filter((e) => e.event === "livekit_agent_ready_received");
  assert.equal(readyEvents.length, 2);
  assert.notEqual(readyEvents[0].connectionId, readyEvents[1].connectionId, "each Start's agent_ready has its own connectionId");

  await engine2.end();
});

test("PHASE C3 F: Stop then immediate Start again, then 5 turns, all succeed in the NEW connection", async () => {
  resetFixtures();
  const engine1 = new LiveKitPocEngine(makeCallbackRecorder().callbacks);
  await startReady(engine1, "session-restart-turns");
  await engine1.end();

  const rec2 = makeCallbackRecorder();
  const engine2 = new LiveKitPocEngine(rec2.callbacks);
  const room2 = await startReady(engine2, "session-restart-turns");

  for (let turn = 1; turn <= 5; turn += 1) {
    assert.equal(engine2.getState(), "listening", `turn ${turn}: must be listening before speaking`);
    FakeSpeechRecognition.instances.at(-1).emitFinal(`Question ${turn}`);
    await flushMicrotasks();
    const clientTurnId = latestStudentTurnId(room2);
    sendTurnAck(room2, clientTurnId);
    sendTurnStatus(room2, clientTurnId, "speaking_started");
    sendTurnStatus(room2, clientTurnId, "speaking_ended");
    assert.equal(engine2.getState(), "listening", `turn ${turn}: must return to listening`);
  }

  assert.equal(engine2.getTurnCount(), 5);
  assert.equal(rec2.errors.length, 0, "no errors across 5 turns in the restarted connection");

  await engine2.end();
});

test("PHASE C3 H: refresh simulation - a fresh engine instance for the SAME session gets its own fresh room", async () => {
  resetFixtures();
  // Simulates a full browser refresh: no reference to any prior engine
  // exists at all (unlike Stop, there is no engine1.end() call here) - the
  // new engine/hook mount is the FIRST thing this "page load" ever does.
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await startReady(engine, "session-refresh-1");

  assert.equal(engine.getState(), "listening");
  assert.equal(tokenMintCalls().length, 1, "post-refresh Start still requests exactly one fresh token");
  assert.ok(rec.roomNames[0], "a room name was assigned by the (simulated) fresh token response");

  await engine.end();
});

test("PHASE C3 I: leave-and-return simulation - Start after a fresh engine mount gets a fresh room, same sessionId", async () => {
  resetFixtures();
  const sessionId = "session-leave-return-1";

  const engineBeforeLeaving = new LiveKitPocEngine(makeCallbackRecorder().callbacks);
  await startReady(engineBeforeLeaving, sessionId);
  // Leaving the page: useLiveKitInterviewVoice.ts's unmount effect calls
  // end() exactly like Stop does.
  await engineBeforeLeaving.end();

  // Returning and pressing Start: a brand-new engine/hook instance, SAME
  // sessionId (the interview session itself never changed).
  const recAfterReturn = makeCallbackRecorder();
  const engineAfterReturn = new LiveKitPocEngine(recAfterReturn.callbacks);
  await startReady(engineAfterReturn, sessionId);

  assert.equal(engineAfterReturn.getState(), "listening");
  const tokenRequests = tokenMintCalls();
  assert.equal(tokenRequests.length, 2);
  assert.equal(
    JSON.parse(tokenRequests[0].init.body).sessionId,
    JSON.parse(tokenRequests[1].init.body).sessionId,
    "the SAME interview session id is used for every token request across leave/return",
  );
  assert.notEqual(recAfterReturn.roomNames[0], undefined);

  await engineAfterReturn.end();
});

test("PHASE C3 J: LiveKit's own Reconnecting/Reconnected during an active call never requests a new token, connectionId, or room", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-reconnect-1");
  assert.equal(engine.getState(), "listening");

  const tokenCallsBefore = tokenMintCalls().length;
  const roomsBefore = createdRooms.length;

  room.emit(RoomEvent.Reconnecting);
  assert.equal(engine.getState(), "reconnecting");
  room.emit(RoomEvent.Reconnected);
  assert.equal(engine.getState(), "listening");

  assert.equal(tokenMintCalls().length, tokenCallsBefore, "no new token request during an active-call reconnect");
  assert.equal(createdRooms.length, roomsBefore, "no new Room object constructed during an active-call reconnect");
  assert.equal(createdRooms[createdRooms.length - 1], room, "still the SAME room instance throughout");

  await engine.end();
});

test("PHASE C3 N: microphone publishes again in the SECOND engine/room after Stop-then-Start", async () => {
  resetFixtures();
  const engine1 = new LiveKitPocEngine(makeCallbackRecorder().callbacks);
  await startReady(engine1, "session-mic-restart");
  const room1 = createdRooms[0];
  assert.equal(room1.micCallCount, 1);
  await engine1.end();

  const rec2 = makeCallbackRecorder();
  const engine2 = new LiveKitPocEngine(rec2.callbacks);
  await startReady(engine2, "session-mic-restart");
  const room2 = createdRooms[1];

  assert.notEqual(room1, room2, "a brand-new Room object for the second connection");
  assert.equal(room2.micCallCount, 1, "the NEW room/connection independently publishes its own mic");
  assert.ok(telemetryEvents().some((e) => e.event === "livekit_mic_ready"));

  await engine2.end();
});

test("PHASE C3: a stale agent_ready delivered to an OLD (ended) engine's room never affects a NEW engine for the same session", async () => {
  resetFixtures();
  const engine1 = new LiveKitPocEngine(makeCallbackRecorder().callbacks);
  await engine1.start("session-stale-restart");
  await flushMicrotasks();
  const oldRoom = createdRooms.at(-1);
  await engine1.end();

  const rec2 = makeCallbackRecorder();
  const engine2 = new LiveKitPocEngine(rec2.callbacks);
  await engine2.start("session-stale-restart");
  await flushMicrotasks();
  const newRoom = createdRooms.at(-1);
  assert.notEqual(oldRoom, newRoom);

  // A late agent_ready for the OLD (now-torn-down) connection/room must
  // never reach or mutate the NEW engine - proven by emitting it on the OLD
  // room object (which engine1's own generation guard already renders inert
  // for engine1) and confirming engine2 is COMPLETELY unaffected, since it
  // never even registered a listener on oldRoom in the first place.
  sendAgentReady(oldRoom);
  assert.equal(engine2.getState(), "waiting_for_agent", "engine2 must be unaffected by a message on a different room object");

  sendAgentReady(newRoom);
  assert.equal(engine2.getState(), "listening", "engine2's OWN room's agent_ready still works normally");

  await engine2.end();
});

// ---------------------------------------------------------------------------
// PHASE D1: useLiveKitInterviewVoice.ts's stale-callback guard (fixes the
// confirmed "Conversation finished" bug after pressing Stop) and the
// Stop -> Resume lifecycle. Driven through the REAL hook via
// createHookTester() above - not a stub - against the SAME FakeRoom/fetch
// mocks the engine tests use, so these prove the fix against real async
// completion order, not an idealized one.
// ---------------------------------------------------------------------------
function hookOptions(overrides = {}) {
  return {
    sessionId: "session-d1-1",
    enabled: true,
    onInterim: () => {},
    onTurnCompleted: () => {},
    ...overrides,
  };
}

test("PHASE D1 A: Stop sets IDLE, and the old engine's own delayed ended-state callback (the confirmed root cause) never overwrites it", async () => {
  resetFixtures();
  const tester = createHookTester(useLiveKitInterviewVoice);
  tester.render(hookOptions());

  tester.getResult().startConversation();
  await flushMicrotasks();
  const room1 = createdRooms.at(-1);
  sendAgentReady(room1);
  await flushMicrotasks();
  assert.equal(tester.getResult().state, "LISTENING");

  tester.getResult().stopConversation();
  assert.equal(tester.getResult().state, "IDLE", "Stop must synchronously read as IDLE, never FINISHED");

  // Let engine1's own end() chain (await room.disconnect() -> setState("ended"))
  // resolve - this delayed onStateChange("ended") is the exact call that used
  // to silently clobber the IDLE just set above.
  await flushMicrotasks();
  assert.equal(room1.disconnectCalls, 1);
  assert.equal(tester.getResult().state, "IDLE", "a late 'ended' callback from the stopped engine must be ignored");
});

test("PHASE D1 B: reset() (used by End Interview) also reads as IDLE immediately and is protected from the same old-engine late callback", async () => {
  resetFixtures();
  const tester = createHookTester(useLiveKitInterviewVoice);
  tester.render(hookOptions({ sessionId: "session-d1-2" }));

  tester.getResult().startConversation();
  await flushMicrotasks();
  const room1 = createdRooms.at(-1);
  sendAgentReady(room1);
  await flushMicrotasks();

  tester.getResult().reset();
  assert.equal(tester.getResult().state, "IDLE");
  assert.equal(tester.getResult().errorMessage, null);

  await flushMicrotasks();
  assert.equal(tester.getResult().state, "IDLE", "a late 'ended' callback after reset() must be ignored");
});

test("PHASE D1 C: Resume (Start again after Stop) mints a fresh token, joins a brand-new room, and reaches LISTENING again", async () => {
  resetFixtures();
  const tester = createHookTester(useLiveKitInterviewVoice);
  tester.render(hookOptions({ sessionId: "session-d1-3" }));

  tester.getResult().startConversation();
  await flushMicrotasks();
  const room1 = createdRooms.at(-1);
  sendAgentReady(room1);
  await flushMicrotasks();
  tester.getResult().stopConversation();
  await flushMicrotasks();

  tester.getResult().startConversation();
  await flushMicrotasks();
  const room2 = createdRooms.at(-1);
  assert.notEqual(room2, room1, "Resume must join a brand-new room, never rejoin the one just left");
  sendAgentReady(room2);
  await flushMicrotasks();

  assert.equal(tester.getResult().state, "LISTENING");
  assert.equal(tokenMintCalls().length, 2, "Resume mints a fresh token, same as any other Start");
  const bodies = tokenMintCalls().map((c) => JSON.parse(c.init.body));
  assert.ok(bodies.every((b) => b.sessionId === "session-d1-3"), "same session id across Stop -> Resume");
});

test("PHASE D1 D: retry() after an engine error builds a new engine, unaffected by the old (errored) engine's late completions", async () => {
  resetFixtures();
  const tester = createHookTester(useLiveKitInterviewVoice);
  tester.render(hookOptions({ sessionId: "session-d1-4" }));

  tester.getResult().startConversation();
  // The mic-timeout watchdog clears itself a few microtask hops after
  // setMicrophoneEnabled(true) resolves (see attemptEnableMicrophone's
  // .then chain) - extra rounds beyond the shared flushMicrotasks() budget
  // are needed here specifically because the fake-React harness's own
  // setState-triggered re-render adds hops before that chain settles.
  // Flushing generously just ensures ONLY the agent-ready watchdog remains
  // pending, matching the direct-engine "agent_ready never arriving" test.
  await flushMicrotasks();
  await flushMicrotasks();
  assert.equal(tester.getResult().state, "REQUESTING_PERMISSION");
  fireTimerById(latestTimerId()); // the agent-ready watchdog never resolves
  assert.equal(tester.getResult().state, "ERROR");
  const room1 = createdRooms.at(-1);

  tester.getResult().retry();
  await flushMicrotasks();
  const room2 = createdRooms.at(-1);
  assert.notEqual(room2, room1, "retry() must build a new engine/room, not reuse the errored one");
  sendAgentReady(room2);
  await flushMicrotasks();
  assert.equal(tester.getResult().state, "LISTENING", "retry succeeds normally in the new engine");

  // A late message on the OLD (errored, abandoned) room must never reach the
  // hook's now-current (engine2-backed) state.
  sendAgentReady(room1);
  assert.equal(tester.getResult().state, "LISTENING", "a stale signal from the abandoned engine must be ignored");
});

test("PHASE D1 E: while an engine is still the active one, its own Reconnecting/Reconnected updates reach the hook normally (guard does not block the CURRENT engine)", async () => {
  resetFixtures();
  const tester = createHookTester(useLiveKitInterviewVoice);
  tester.render(hookOptions({ sessionId: "session-d1-5" }));

  tester.getResult().startConversation();
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  sendAgentReady(room);
  await flushMicrotasks();
  assert.equal(tester.getResult().state, "LISTENING");

  room.emit(RoomEvent.Reconnecting);
  assert.equal(tester.getResult().state, "PROCESSING", "the still-current engine's own reconnecting must still update hook state");
  room.emit(RoomEvent.Reconnected);
  assert.equal(tester.getResult().state, "LISTENING", "and recovers normally once reconnected");
});

test("PHASE D1 F: unmounting the hook fully ends the engine, and any later callback from that engine is ignored", async () => {
  resetFixtures();
  const tester = createHookTester(useLiveKitInterviewVoice);
  tester.render(hookOptions({ sessionId: "session-d1-6" }));

  tester.getResult().startConversation();
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  sendAgentReady(room);
  await flushMicrotasks();

  tester.unmount();
  await flushMicrotasks();
  assert.equal(room.disconnectCalls, 1, "unmount must fully end the engine, same as Stop");

  // Nothing to assert on hook state post-unmount (no component reads it),
  // but the guard must not throw when engineRef.current is already null.
  assert.doesNotThrow(() => room.emit(RoomEvent.Reconnecting));
});

test("PHASE D1 G: a fresh mount (never started) reads IDLE - identical to post-Stop IDLE, so ConversationControl's existing hasConversation check is what distinguishes Resume from Start, with no new state needed", () => {
  resetFixtures();
  const tester = createHookTester(useLiveKitInterviewVoice);
  tester.render(hookOptions({ sessionId: "session-d1-7" }));
  assert.equal(tester.getResult().state, "IDLE");
  assert.equal(tester.getResult().active, false);
});

test("PHASE D1: STATIC - stopConversation/reset never call completeSession, navigate, or any assessment/session-status API - Stop and End Interview remain fully separate", () => {
  const rawSource = fs
    .readFileSync(path.join(repoRoot, "src", "hooks", "useLiveKitInterviewVoice.ts"), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*$/gm, "");
  assert.ok(!/completeSession|navigate\(|assessment/i.test(rawSource),
    "useLiveKitInterviewVoice.ts must stay unaware of session-completion/assessment/navigation concerns - those belong to InterviewPage.tsx's separate End Interview flow");
});

// ---------------------------------------------------------------------------
// PHASE D2: true SPEAKING-only patient interruption ("barge-in"). Driven
// directly against the engine (mirrors every other STATE/PHASE C test in
// this file) plus one combined D1+D2 regression test via the hook harness
// (createHookTester) proving Stop -> Resume still works even when Interrupt
// was mid-flight. See worker.py's PocAgentSession class docstring for the
// matching backend half (covered by test_livekit_phase_d2.py).
// ---------------------------------------------------------------------------

test("PHASE D2 A/B/C: interruptPatient() while SPEAKING sends ONE interrupt_patient, targeted at the agent, with the current clientTurnId", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const { room, clientTurnId } = await reachSpeaking(engine, "session-int-1");
  assert.equal(engine.getState(), "speaking");

  engine.interruptPatient();
  await flushMicrotasks();

  const sent = interruptPublishes(room);
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].options.destinationIdentities, ["patient-agent"]);
  assert.equal(sent[0].options.reliable, true);
  assert.equal(sent[0].body.clientTurnId, clientTurnId);

  await engine.end();
});

test("PHASE D2 D/E/F: interruptPatient() never ends the room, requests a new token, or creates a new Room", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const { room } = await reachSpeaking(engine, "session-int-2");
  const roomCountBefore = createdRooms.length;
  const tokenCallsBefore = tokenMintCalls().length;

  engine.interruptPatient();
  await flushMicrotasks();

  assert.equal(createdRooms.length, roomCountBefore, "no new Room instance");
  assert.equal(tokenMintCalls().length, tokenCallsBefore, "no new token request");
  assert.equal(room.disconnectCalls, 0, "the SAME room stays connected");

  await engine.end();
});

test("PHASE D2 G: interruptPatient() transitions SPEAKING -> INTERRUPTING", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  await reachSpeaking(engine, "session-int-3");

  engine.interruptPatient();
  assert.equal(engine.getState(), "interrupting");

  await engine.end();
});

test("PHASE D2 H/I: an 'interrupted' ack transitions INTERRUPTING -> LISTENING with a FRESH recognizer", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const { room, clientTurnId } = await reachSpeaking(engine, "session-int-4");
  const recognizerCountBefore = FakeSpeechRecognition.instances.length;

  engine.interruptPatient();
  assert.equal(engine.getState(), "interrupting");
  sendTurnStatus(room, clientTurnId, "interrupted");

  assert.equal(engine.getState(), "listening");
  assert.equal(FakeSpeechRecognition.instances.length, recognizerCountBefore + 1, "a fresh recognizer must start");
  assert.deepEqual(rec.completedTurns, [1], "a turn already exists in the transcript regardless of audio length");

  await engine.end();
});

test("PHASE D2 J: a lost interrupt ack times out back to LISTENING - never stuck in INTERRUPTING, never tears down the room", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const { room } = await reachSpeaking(engine, "session-int-5");

  engine.interruptPatient();
  assert.equal(engine.getState(), "interrupting");
  fireTimerById(latestTimerId());
  assert.equal(engine.getState(), "listening");
  assert.equal(room.disconnectCalls, 0, "a lost ack must never tear down the room");

  await engine.end();
});

test("PHASE D2 K: double interruptPatient() while already INTERRUPTING is a safe no-op (only one message ever sent)", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const { room } = await reachSpeaking(engine, "session-int-6");

  engine.interruptPatient();
  engine.interruptPatient();
  engine.interruptPatient();
  await flushMicrotasks();

  assert.equal(interruptPublishes(room).length, 1);

  await engine.end();
});

test("PHASE D2 L: a stale 'interrupted' ack for a DIFFERENT clientTurnId is ignored", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const { room } = await reachSpeaking(engine, "session-int-7");

  engine.interruptPatient();
  assert.equal(engine.getState(), "interrupting");
  sendTurnStatus(room, "some-other-turn-id", "interrupted");
  assert.equal(engine.getState(), "interrupting", "must stay interrupting - not a match for the current turn");

  await engine.end();
});

test("PHASE D2 M: a late speaking_ended for an already-interrupted turn is harmless (never double-counted)", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const { room, clientTurnId } = await reachSpeaking(engine, "session-int-8");

  engine.interruptPatient();
  sendTurnStatus(room, clientTurnId, "interrupted");
  assert.equal(engine.getState(), "listening");
  assert.deepEqual(rec.completedTurns, [1]);

  sendTurnStatus(room, clientTurnId, "speaking_ended");
  assert.equal(engine.getState(), "listening", "must remain listening");
  assert.deepEqual(rec.completedTurns, [1], "must not double-count the same turn");

  await engine.end();
});

test("PHASE D2 N: interruptPatient() is a no-op outside SPEAKING - no interrupt_patient is ever sent while THINKING or LISTENING", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReady(engine, "session-int-9");

  // LISTENING: no active turn at all.
  engine.interruptPatient();
  await flushMicrotasks();
  assert.equal(interruptPublishes(room).length, 0);
  assert.equal(engine.getState(), "listening");

  // THINKING: a real turn is in flight, but audio has not started - this is
  // the exact case Phase D2 deliberately never offers real cancellation for
  // (see worker.py's class docstring: the OpenAI/ElevenLabs call cannot be
  // forcibly stopped once started).
  FakeSpeechRecognition.instances.at(-1).emitFinal("How long?");
  await flushMicrotasks();
  assert.equal(engine.getState(), "thinking");

  engine.interruptPatient();
  await flushMicrotasks();

  assert.equal(interruptPublishes(room).length, 0, "no interrupt_patient message must ever be sent while THINKING");
  assert.equal(engine.getState(), "thinking", "must remain thinking - never a fake interruption");

  await engine.end();
});

test("PHASE D2 O: Stop while INTERRUPTING still fully ends the room, and a late interrupted ack cannot resurrect the ended engine", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const { room } = await reachSpeaking(engine, "session-int-10");

  engine.interruptPatient();
  assert.equal(engine.getState(), "interrupting");
  await engine.end();

  assert.equal(room.disconnectCalls, 1);
  assert.equal(engine.getState(), "ended");

  sendTurnStatus(room, "whatever", "interrupted");
  assert.equal(engine.getState(), "ended", "a late ack after end() must never resurrect the ended engine");
});

test("PHASE D2: race - if speaking_ended wins against a pending interrupt, both converge on LISTENING without double-counting", async () => {
  resetFixtures();
  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const { room, clientTurnId } = await reachSpeaking(engine, "session-int-race");

  engine.interruptPatient();
  assert.equal(engine.getState(), "interrupting");
  // The turn actually finished naturally before the interrupt could land.
  sendTurnStatus(room, clientTurnId, "speaking_ended");
  assert.equal(engine.getState(), "listening");
  assert.deepEqual(rec.completedTurns, [1]);

  // The worker's interrupt ack arrives anyway (lost the race) - must be a
  // harmless no-op, never a second onTurnCompleted for the SAME turn.
  sendTurnStatus(room, clientTurnId, "interrupted");
  assert.equal(engine.getState(), "listening");
  assert.deepEqual(rec.completedTurns, [1], "must not double-count the same turn");

  await engine.end();
});

test("PHASE D1+D2 P: Interrupt mid-flight, then Stop, then Resume still works end-to-end (D1 regression under D2)", async () => {
  resetFixtures();
  const tester = createHookTester(useLiveKitInterviewVoice);
  tester.render(hookOptions({ sessionId: "session-d1d2-1" }));

  tester.getResult().startConversation();
  await flushMicrotasks();
  const room1 = createdRooms.at(-1);
  sendAgentReady(room1);
  await flushMicrotasks();

  FakeSpeechRecognition.instances.at(-1).emitFinal("How long?");
  await flushMicrotasks();
  const clientTurnId = latestStudentTurnId(room1);
  sendTurnAck(room1, clientTurnId);
  sendTurnStatus(room1, clientTurnId, "speaking_started");
  assert.equal(tester.getResult().state, "SPEAKING");

  tester.getResult().interruptPatient();
  assert.equal(tester.getResult().state, "INTERRUPTING");

  // Stop wins even with an interrupt still pending.
  tester.getResult().stopConversation();
  assert.equal(tester.getResult().state, "IDLE");
  await flushMicrotasks();
  assert.equal(tester.getResult().state, "IDLE", "must remain IDLE - never resurrected by a late interrupt/ended callback");

  // Resume: fresh token, fresh room, reaches LISTENING again - unaffected by
  // the interrupt that was in flight before Stop.
  tester.getResult().startConversation();
  await flushMicrotasks();
  const room2 = createdRooms.at(-1);
  assert.notEqual(room2, room1, "Resume must join a brand-new room");
  sendAgentReady(room2);
  await flushMicrotasks();
  assert.equal(tester.getResult().state, "LISTENING");
  assert.equal(tokenMintCalls().length, 2);
});

// ---------------------------------------------------------------------------
// PHASE 4: turn-ID synchronization fix (semantic turn control). Confirmed
// production bug: worker.py's _send_turn_ack unconditionally ack'd every
// browser-originated packet, even ones semantic control was about to
// ignore. The frontend's handleTurnAck treated that ack as authorization to
// claim "thinking" and arm a processing watchdog for an id that would never
// resolve - which left `state` stuck so the REAL semantic_turn_started that
// followed (a different id) was silently dropped by
// handleSemanticTurnStarted's own state==="listening" guard, and every
// subsequent patient_turn_status for the real turn was rejected as
// "foreign." Fix: an additive `semanticIgnored` ack field, source-aware
// sendText() (speech_browser vs manual_typed), and a MANUAL_OVERRIDE
// backend bypass so typed Send keeps working. These tests drive the fix
// through the SAME FakeRoom/fetch mocks every other test in this file uses.
// ---------------------------------------------------------------------------

test("PHASE 4: a browser SpeechRecognition final while semantic control is active never publishes a student_text packet", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReadySemantic(engine, "session-p4-1");

  FakeSpeechRecognition.instances.at(-1).emitFinal("How long has this been going on?");
  await flushMicrotasks();

  assert.equal(studentTextPublishes(room).length, 0, "a speech final must never publish while semantic control is active");
  assert.equal(engine.getState(), "listening", "the engine must stay in listening, never claim thinking for a suppressed final");

  await engine.end();
});

test("PHASE 4: sendText's speech_browser guard is centralized - suppresses regardless of call site, not just onFinal", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReadySemantic(engine, "session-p4-2");

  await engine.sendText("direct call simulating a speech final", { source: "speech_browser" });
  await flushMicrotasks();

  assert.equal(studentTextPublishes(room).length, 0);
  assert.equal(engine.getState(), "listening");

  await engine.end();
});

test("PHASE 4: an ignored ack (semanticIgnored) returns the engine to listening, and the following semantic_turn_started is then adopted", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReadySemantic(engine, "session-p4-3");

  // Establish a pending delivery turn to exercise handleTurnAck's recovery
  // path itself (the mechanism under test), independent of how such a
  // pending turn could arise in production (a race, a legacy frontend
  // build, etc.) - manual_typed is used here purely as the vehicle to reach
  // "thinking" with a real pendingDeliveryTurnId; the assertion is entirely
  // about what happens when the server's ack for THIS id says
  // semanticIgnored: true.
  await engine.sendText("stray turn", { source: "manual_typed" });
  await flushMicrotasks();
  const strayTurnId = latestStudentTurnId(room);
  assert.equal(engine.getState(), "thinking");

  sendTurnAck(room, strayTurnId, { semanticIgnored: true });
  await flushMicrotasks();
  assert.equal(engine.getState(), "listening", "must return to listening, never stay stuck in thinking");

  // The REAL semantic turn now arrives, keyed by a completely different id.
  sendSemanticTurnStarted(room, "semantic-session-p4-3-1");
  await flushMicrotasks();
  assert.equal(engine.getState(), "thinking", "the semantic turn must now be adopted (state==='listening' guard passed)");

  sendTurnStatus(room, "semantic-session-p4-3-1", "speaking_started");
  await flushMicrotasks();
  assert.equal(engine.getState(), "speaking", "speaking_started for the semantic id must be accepted, not rejected as foreign");

  sendTurnStatus(room, "semantic-session-p4-3-1", "speaking_ended");
  await flushMicrotasks();
  assert.equal(engine.getState(), "listening");
  assert.deepEqual(rec.completedTurns, [1], "onTurnCompleted must fire exactly once for the semantic turn");

  await engine.end();
});

test("PHASE 4: manual typed Send (submitExternal) still creates a real, authoritative turn while semantic control is active", async () => {
  resetFixtures();
  const tester = createHookTester(useLiveKitInterviewVoice);
  tester.render(hookOptions({ sessionId: "session-p4-4" }));

  tester.getResult().startConversation();
  await flushMicrotasks();
  const room = createdRooms.at(-1);
  sendAgentReadySemantic(room);
  await flushMicrotasks();
  assert.equal(tester.getResult().state, "LISTENING");

  tester.getResult().submitExternal("I have a question about my medication");
  await flushMicrotasks();

  const published = decodedStudentTextPublishes(room);
  assert.equal(published.length, 1, "typed Send must still publish a student_text packet under semantic control");
  assert.equal(published[0].source, "manual_typed");
  assert.equal(tester.getResult().state, "PROCESSING", "typed Send moves the UI to PROCESSING exactly like before");

  // The backend acks a manual override normally (no semanticIgnored) - see
  // worker.py's TurnSource.MANUAL_OVERRIDE - so the existing turn_ack path
  // completes the turn exactly like any pre-Phase-4 typed Send.
  const clientTurnId = published[0].clientTurnId;
  sendTurnAck(room, clientTurnId);
  sendTurnStatus(room, clientTurnId, "speaking_started");
  await flushMicrotasks();
  assert.equal(tester.getResult().state, "SPEAKING");
  sendTurnStatus(room, clientTurnId, "speaking_ended");
  await flushMicrotasks();
  assert.equal(tester.getResult().state, "LISTENING");
});

test("PHASE 4: semantic_fallback restores normal browser-authoritative behavior for the rest of the session", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReadySemantic(engine, "session-p4-5");

  // Before fallback: a speech final is suppressed, exactly like the first
  // test above.
  const firstRecognizer = FakeSpeechRecognition.instances.at(-1);
  firstRecognizer.emitFinal("First attempt, before fallback");
  await flushMicrotasks();
  assert.equal(studentTextPublishes(room).length, 0);

  sendSemanticFallback(room, "stt_stream_died");
  await flushMicrotasks();

  // A suppressed final never starts a real turn, so there is no
  // speaking_ended to synchronously create the next recognizer - simulate
  // the browser naturally ending this (still-listening) recognition
  // session, which arms the SAME debounced restart onEnd always uses, then
  // fire it (fake timers, no real 150ms wait) to get a fresh instance.
  firstRecognizer.abort();
  fireTimerById(latestTimerId());

  // After fallback: browser SpeechRecognition is authoritative again - a
  // fresh final on the NEW recognizer now publishes normally, exactly like
  // the pre-Phase-4/non-semantic path.
  FakeSpeechRecognition.instances.at(-1).emitFinal("Second attempt, after fallback");
  await flushMicrotasks();
  const published = decodedStudentTextPublishes(room);
  assert.equal(published.length, 1, "fallback must restore normal browser-authoritative publishing");
  assert.equal(published[0].source, "speech_browser");
  assert.equal(engine.getState(), "thinking");

  await engine.end();
});

test("PHASE 4: no duplicate student/patient signaling - exactly one publish and one onTurnCompleted per semantic turn", async () => {
  resetFixtures();

  const rec = makeCallbackRecorder();
  const engine = new LiveKitPocEngine(rec.callbacks);
  const room = await startReadySemantic(engine, "session-p4-6");

  // A speech final is suppressed (no publish at all)...
  FakeSpeechRecognition.instances.at(-1).emitFinal("Suppressed final");
  await flushMicrotasks();
  assert.equal(studentTextPublishes(room).length, 0);

  // ...and the SAME utterance's server-side semantic turn completes exactly
  // once end-to-end.
  sendSemanticTurnStarted(room, "semantic-session-p4-6-1");
  await flushMicrotasks();
  sendTurnStatus(room, "semantic-session-p4-6-1", "speaking_started");
  await flushMicrotasks();
  sendTurnStatus(room, "semantic-session-p4-6-1", "speaking_ended");
  await flushMicrotasks();

  assert.equal(studentTextPublishes(room).length, 0, "still zero browser publishes for the whole turn");
  assert.deepEqual(rec.completedTurns, [1], "onTurnCompleted must fire exactly once, never zero or twice");

  await engine.end();
});
