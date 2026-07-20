/**
 * Executable tests for the pure voice state machine.
 * Run with: npm run test:voice
 * (compiles src/hooks/voiceStateMachine.ts to .test-build/, then runs node --test)
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  initialMachine,
  reduce,
  isUsableTranscript,
  isConversationActive,
} from "../.test-build/hooks/voiceStateMachine.js";

function drive(events, from = initialMachine) {
  return events.reduce((m, e) => reduce(m, e), from);
}

test("normal flow: IDLE → LISTENING → PROCESSING → SPEAKING → COOLDOWN → LISTENING", () => {
  let m = drive([{ type: "START" }]);
  assert.equal(m.state, "REQUESTING_PERMISSION");
  m = reduce(m, { type: "PERMISSION_GRANTED" });
  assert.equal(m.state, "LISTENING");
  m = reduce(m, { type: "FINAL_TRANSCRIPT", text: "Hi Carly, how are you today?" });
  assert.equal(m.state, "PROCESSING");
  assert.equal(m.acceptedTranscript, "Hi Carly, how are you today?");
  m = reduce(m, { type: "RESPONSE_RECEIVED", speak: true });
  assert.equal(m.state, "SPEAKING");
  m = reduce(m, { type: "TTS_ENDED" });
  assert.equal(m.state, "COOLDOWN");
  m = reduce(m, { type: "COOLDOWN_ELAPSED" });
  assert.equal(m.state, "LISTENING");
});

test("manual interruption: SPEAKING → INTERRUPTING → LISTENING → PROCESSING", () => {
  let m = drive([
    { type: "START" },
    { type: "PERMISSION_GRANTED" },
    { type: "FINAL_TRANSCRIPT", text: "Tell me about your wrists" },
    { type: "RESPONSE_RECEIVED", speak: true },
  ]);
  assert.equal(m.state, "SPEAKING");
  m = reduce(m, { type: "INTERRUPT" });
  assert.equal(m.state, "INTERRUPTING");
  m = reduce(m, { type: "INTERRUPT_READY" });
  assert.equal(m.state, "LISTENING");
  m = reduce(m, { type: "FINAL_TRANSCRIPT", text: "When did your wrist problem start?" });
  assert.equal(m.state, "PROCESSING");
});

test("automatic interruption: sustained voice interrupts, spike does not", () => {
  const speaking = drive([
    { type: "START" },
    { type: "PERMISSION_GRANTED" },
    { type: "FINAL_TRANSCRIPT", text: "How are you feeling?" },
    { type: "RESPONSE_RECEIVED", speak: true },
  ]);
  assert.equal(speaking.state, "SPEAKING");
  // false-noise test: one short spike → patient continues speaking
  assert.equal(reduce(speaking, { type: "VAD_SPIKE" }).state, "SPEAKING");
  // sustained voice → interruption
  assert.equal(reduce(speaking, { type: "VAD_SUSTAINED_VOICE" }).state, "INTERRUPTING");
});

test("LISTENING and SPEAKING can never coexist (single state)", () => {
  const m = drive([
    { type: "START" },
    { type: "PERMISSION_GRANTED" },
    { type: "FINAL_TRANSCRIPT", text: "Hello there" },
    { type: "RESPONSE_RECEIVED", speak: true },
  ]);
  assert.equal(m.state, "SPEAKING"); // one state at a time by construction
  // While SPEAKING, a final transcript must be ignored (recognition is off):
  assert.equal(reduce(m, { type: "FINAL_TRANSCRIPT", text: "echo text" }).state, "SPEAKING");
});

test("duplicate submission: second final transcript is rejected while PROCESSING", () => {
  let m = drive([
    { type: "START" },
    { type: "PERMISSION_GRANTED" },
    { type: "FINAL_TRANSCRIPT", text: "What brought you in today?" },
  ]);
  assert.equal(m.state, "PROCESSING");
  const again = reduce(m, { type: "FINAL_TRANSCRIPT", text: "What brought you in today?" });
  assert.equal(again.state, "PROCESSING");
  // The event must be a no-op: the machine object is returned unchanged,
  // meaning no second transcript was accepted for submission.
  assert.equal(again, m, "duplicate transcript must not be re-accepted");
});

test("noise / unusable transcripts keep listening", () => {
  const listening = drive([{ type: "START" }, { type: "PERMISSION_GRANTED" }]);
  for (const noise of ["", "   ", "a", "9", "!!"]) {
    assert.equal(reduce(listening, { type: "FINAL_TRANSCRIPT", text: noise }).state, "LISTENING");
  }
  assert.equal(isUsableTranscript("hi"), true);
  assert.equal(isUsableTranscript("mm"), true);
  assert.equal(isUsableTranscript("h"), false);
});

test("backend failure: PROCESSING → ERROR, retry → LISTENING, no fake reply state", () => {
  let m = drive([
    { type: "START" },
    { type: "PERMISSION_GRANTED" },
    { type: "FINAL_TRANSCRIPT", text: "How are you?" },
    { type: "RESPONSE_FAILED", message: "The patient response could not be generated. Please retry." },
  ]);
  assert.equal(m.state, "ERROR");
  assert.match(m.errorMessage, /could not be generated/);
  // no automatic restart: COOLDOWN_ELAPSED/TTS events do nothing in ERROR
  assert.equal(reduce(m, { type: "COOLDOWN_ELAPSED" }).state, "ERROR");
  m = reduce(m, { type: "RETRY" });
  assert.equal(m.state, "REQUESTING_PERMISSION");
});

test("permission denied → ERROR with message", () => {
  const m = drive([{ type: "START" }, { type: "PERMISSION_DENIED", message: "Microphone access was denied." }]);
  assert.equal(m.state, "ERROR");
  assert.match(m.errorMessage, /denied/);
});

test("stop and resume", () => {
  let m = drive([{ type: "START" }, { type: "PERMISSION_GRANTED" }]);
  m = reduce(m, { type: "STOP" });
  assert.equal(m.state, "PAUSED");
  m = reduce(m, { type: "RESUME" });
  assert.equal(m.state, "REQUESTING_PERMISSION");
});

test("case-switch cleanup: RESET returns to a clean IDLE state", () => {
  const mid = drive([
    { type: "START" },
    { type: "PERMISSION_GRANTED" },
    { type: "FINAL_TRANSCRIPT", text: "Tell me about dance" },
    { type: "RESPONSE_RECEIVED", speak: true },
  ]);
  const fresh = reduce(mid, { type: "RESET" });
  assert.deepEqual(fresh, initialMachine);
  assert.equal(isConversationActive(fresh.state), false);
});

test("speak-replies disabled: PROCESSING → COOLDOWN (skips SPEAKING)", () => {
  const m = drive([
    { type: "START" },
    { type: "PERMISSION_GRANTED" },
    { type: "FINAL_TRANSCRIPT", text: "How do you sleep?" },
    { type: "RESPONSE_RECEIVED", speak: false },
  ]);
  assert.equal(m.state, "COOLDOWN");
});
