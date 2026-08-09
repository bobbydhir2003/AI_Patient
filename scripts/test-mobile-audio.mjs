/**
 * Tests for the mobile interview audio logic (pure modules).
 * Run with: npm run test:mobileaudio
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { describeMicError, checkVoicePreconditions } from "../.test-build/services/mediaErrors.js";
import { audioSetupOptions, autoInterruptNote } from "../.test-build/services/mobileAudio.js";
import { reduce, initialMachine } from "../.test-build/hooks/voiceStateMachine.js";

// ---------------------------------------------------------------- mic errors
test("describeMicError maps permission denial to 'blocked'", () => {
  assert.equal(describeMicError({ name: "NotAllowedError" }).code, "blocked");
  assert.equal(describeMicError({ name: "SecurityError" }).code, "blocked");
  assert.match(describeMicError({ name: "NotAllowedError" }).message, /blocked/i);
});

test("describeMicError maps missing device to 'no_microphone'", () => {
  assert.equal(describeMicError({ name: "NotFoundError" }).code, "no_microphone");
});

test("describeMicError maps busy device to 'in_use'", () => {
  assert.equal(describeMicError({ name: "NotReadableError" }).code, "in_use");
  assert.equal(describeMicError({ name: "AbortError" }).code, "in_use");
});

test("describeMicError falls back to 'unknown' and still offers typing", () => {
  const info = describeMicError(new Error("weird"));
  assert.equal(info.code, "unknown");
  assert.match(info.message, /typing/i);
});

// -------------------------------------------------------------- preconditions
test("insecure context is reported clearly", () => {
  const r = checkVoicePreconditions({ isSecureContext: false, hasMediaDevices: true, hasGetUserMedia: true });
  assert.equal(r.ok, false);
  assert.equal(r.info.code, "insecure_context");
});

test("missing getUserMedia => unsupported (typing fallback)", () => {
  const r = checkVoicePreconditions({ isSecureContext: true, hasMediaDevices: false, hasGetUserMedia: false });
  assert.equal(r.ok, false);
  assert.equal(r.info.code, "unsupported");
});

test("secure + capable => ok", () => {
  const r = checkVoicePreconditions({ isSecureContext: true, hasMediaDevices: true, hasGetUserMedia: true });
  assert.equal(r.ok, true);
});

// --------------------------------------------------------------- mobile labels
test("mobile audio labels never say 'laptop', values preserved", () => {
  const mobile = audioSetupOptions(true);
  assert.deepEqual(mobile.map((o) => o.value), ["speakers", "headphones"]);
  assert.doesNotMatch(mobile[0].label, /laptop/i);
  assert.match(mobile[0].label, /device|phone/i);
  assert.match(mobile[1].label, /earbuds|headphones/i);

  const desktop = audioSetupOptions(false);
  assert.match(desktop[0].label, /laptop/i);
});

test("auto-interrupt note is stronger on mobile", () => {
  assert.match(autoInterruptNote(true), /headphones|earbuds/i);
  assert.notEqual(autoInterruptNote(true), autoInterruptNote(false));
});

// ------------------------------------------------- background pause / resume
test("backgrounding an active conversation pauses (STOP => PAUSED), resumable", () => {
  // Simulate: START -> permission granted -> LISTENING, then app hidden (STOP).
  let m = reduce(initialMachine, { type: "START" });
  m = reduce(m, { type: "PERMISSION_GRANTED" });
  assert.equal(m.state, "LISTENING");
  m = reduce(m, { type: "STOP" }); // visibilitychange -> hidden
  assert.equal(m.state, "PAUSED");
  // Returning to foreground must NOT auto-listen; only an explicit RESUME does.
  m = reduce(m, { type: "RESUME" });
  assert.equal(m.state, "REQUESTING_PERMISSION");
});

test("mobile default: a denied permission lands in ERROR and is retryable", () => {
  let m = reduce(initialMachine, { type: "START" });
  m = reduce(m, { type: "PERMISSION_DENIED", message: "blocked" });
  assert.equal(m.state, "ERROR");
  assert.equal(m.errorMessage, "blocked");
  m = reduce(m, { type: "RETRY" });
  assert.equal(m.state, "REQUESTING_PERMISSION");
});
