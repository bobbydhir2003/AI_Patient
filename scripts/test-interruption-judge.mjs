/**
 * Executable tests for the echo-safe interruption judge.
 * Run with: npm run test:voice
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createInterruptionJudge,
  getJudgeProfile,
} from "../.test-build/services/interruptionJudge.js";

const FRAME = 50;

/** Feed a sequence of RMS levels (one per 50ms frame) starting at arm time 0. */
function run(judge, levels, startMs = 0) {
  let verdicts = [];
  for (let i = 0; i < levels.length; i += 1) {
    verdicts.push(judge.feed(startMs + i * FRAME, levels[i]));
  }
  return verdicts;
}

function frames(ms) {
  return Math.ceil(ms / FRAME);
}

const speakersMed = getJudgeProfile("speakers", "medium");
const headphonesMed = getJudgeProfile("headphones", "medium");
const ECHO = 0.05; // typical speaker-echo RMS in these tests
const AMBIENT = 0.008;

function armedJudge(profile = speakersMed) {
  return createInterruptionJudge(profile, 0, AMBIENT);
}

test("patient speaker echo does not interrupt (echo near baseline)", () => {
  const judge = armedJudge();
  // whole utterance at steady echo level: protection + sampling + 5s active
  const total = frames(speakersMed.protectionMs + speakersMed.echoSampleMs + 5000);
  const verdicts = run(judge, Array(total).fill(ECHO));
  assert.ok(!verdicts.includes("interrupt"), "echo must never interrupt");
  // baseline learned the echo, so threshold sits far above it
  const snap = judge.snapshot();
  assert.ok(snap.startThreshold > ECHO * 2, "start threshold must clear the echo level");
});

test("initial protection window: even loud audio is ignored", () => {
  const judge = armedJudge();
  const loud = 0.5;
  const verdicts = run(judge, Array(frames(speakersMed.protectionMs) - 1).fill(loud));
  assert.ok(verdicts.every((v) => v === "protected"));
  assert.ok(!verdicts.includes("interrupt"));
});

test("one short spike (keyboard click) does not interrupt", () => {
  const judge = armedJudge();
  const warmup = Array(frames(speakersMed.protectionMs + speakersMed.echoSampleMs)).fill(ECHO);
  run(judge, warmup);
  const t0 = warmup.length * FRAME;
  // 100 ms spike far above threshold, then back to echo
  const spike = [0.6, 0.6, ...Array(20).fill(ECHO)];
  const verdicts = run(judge, spike, t0);
  assert.ok(!verdicts.includes("interrupt"), "single spike must not interrupt");
});

test("sustained student voice interrupts exactly once", () => {
  const judge = armedJudge();
  const warmup = Array(frames(speakersMed.protectionMs + speakersMed.echoSampleMs)).fill(ECHO);
  run(judge, warmup);
  const t0 = warmup.length * FRAME;
  const sustained = Array(frames(speakersMed.sustainMs) + 3).fill(0.5);
  const verdicts = run(judge, sustained, t0);
  assert.ok(verdicts.includes("interrupt"), "sustained loud voice must interrupt");
  // duplicate protection: further frames keep reporting interrupt (already fired),
  // and the caller's lock prevents any second action.
  const after = judge.feed(t0 + sustained.length * FRAME, 0.5);
  assert.equal(after, "interrupt");
  assert.equal(judge.snapshot().phase, "fired");
});

test("hysteresis: dropping below continue threshold resets the sustain timer", () => {
  const judge = armedJudge();
  const warmup = Array(frames(speakersMed.protectionMs + speakersMed.echoSampleMs)).fill(ECHO);
  run(judge, warmup);
  const t0 = warmup.length * FRAME;
  const halfSustain = Math.floor(frames(speakersMed.sustainMs) / 2);
  // above start threshold for half the required time, then silence, repeatedly
  const pattern = [
    ...Array(halfSustain).fill(0.5),
    ...Array(4).fill(0.005),
    ...Array(halfSustain).fill(0.5),
    ...Array(4).fill(0.005),
  ];
  const verdicts = run(judge, pattern, t0);
  assert.ok(!verdicts.includes("interrupt"), "interrupted bursts must not accumulate");
});

test("minimum absolute threshold: near-silent baseline is not hair-trigger", () => {
  const judge = createInterruptionJudge(speakersMed, 0, 0.0001);
  const warmup = Array(frames(speakersMed.protectionMs + speakersMed.echoSampleMs)).fill(0.0001);
  run(judge, warmup);
  const snap = judge.snapshot();
  assert.ok(snap.startThreshold >= speakersMed.minThreshold);
  // quiet breathing-level sound stays below the floor
  const t0 = warmup.length * FRAME;
  const verdicts = run(judge, Array(30).fill(speakersMed.minThreshold * 0.8), t0);
  assert.ok(!verdicts.includes("interrupt"));
});

test("headphones profile reacts faster than speakers profile", () => {
  assert.ok(headphonesMed.protectionMs < speakersMed.protectionMs);
  assert.ok(headphonesMed.sustainMs < speakersMed.sustainMs);
  assert.ok(headphonesMed.multiplier < speakersMed.multiplier);
  const judge = createInterruptionJudge(headphonesMed, 0, AMBIENT);
  const quietEcho = 0.005; // headphones: almost no speaker echo reaches the mic
  const warmup = Array(frames(headphonesMed.protectionMs + headphonesMed.echoSampleMs)).fill(quietEcho);
  run(judge, warmup);
  const t0 = warmup.length * FRAME;
  const verdicts = run(judge, Array(frames(headphonesMed.sustainMs) + 2).fill(0.2), t0);
  assert.ok(verdicts.includes("interrupt"));
});

test("slow baseline drift: gradually louder echo does not interrupt", () => {
  const judge = armedJudge();
  const warm = Array(frames(speakersMed.protectionMs + speakersMed.echoSampleMs)).fill(0.04);
  run(judge, warm);
  const t0 = warm.length * FRAME;
  // echo creeps from 0.04 to 0.055 over 6 seconds (stays below threshold as
  // the rolling baseline follows it)
  const drift = Array.from({ length: frames(6000) }, (_, i) => 0.04 + (0.015 * i) / frames(6000));
  const verdicts = run(judge, drift, t0);
  assert.ok(!verdicts.includes("interrupt"), "slow drift must be absorbed by the rolling baseline");
});
