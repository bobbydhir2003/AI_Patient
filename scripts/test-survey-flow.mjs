/**
 * Tests for the Post-Survey → assessment routing decision.
 * Run with: npm run test:surveyflow
 *
 * Covers spec items 14/15/16:
 *   - Post submit + assessment completed → Assessment Review
 *   - Post submit + still running → existing Assessment Loading page
 *   - failed → routes into the existing Assessment Loading page (which owns the
 *     retry/failure UI — we do not invent a second failure system)
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  postSurveyDestination,
  preSurveyGate,
  isSurveyCompleted,
  postSurveyGate,
} from "../.test-build/services/surveyFlow.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

const SID = "sess123";

test("completed → Assessment Review", () => {
  assert.equal(postSurveyDestination(SID, "completed"), `/assessment/${SID}`);
});

for (const status of ["not_started", "pending", "processing", "verifying"]) {
  test(`${status} → Assessment Loading`, () => {
    assert.equal(postSurveyDestination(SID, status), `/assessment/${SID}/loading`);
  });
}

test("failed → Assessment Loading (existing retry/failure UI)", () => {
  assert.equal(postSurveyDestination(SID, "failed"), `/assessment/${SID}/loading`);
});

test("unknown / null status → Assessment Loading (safe default)", () => {
  assert.equal(postSurveyDestination(SID, "weird"), `/assessment/${SID}/loading`);
  assert.equal(postSurveyDestination(SID, null), `/assessment/${SID}/loading`);
  assert.equal(postSurveyDestination(SID, undefined), `/assessment/${SID}/loading`);
});

// ---------------------------------------------------------------------------
// Case-level survey gating (one package per student+case; Pre and Post are its
// two stages).
// ---------------------------------------------------------------------------
test("preSurveyGate: completed package → skip Pre with completed message", () => {
  assert.equal(preSurveyGate("completed", true), "completed");
  assert.equal(preSurveyGate("completed", false), "completed");
});

test("preSurveyGate: Pre done but package IN_PROGRESS → resume (do not re-ask Pre)", () => {
  assert.equal(preSurveyGate("in_progress", true), "resume");
});

test("preSurveyGate: no receipt / Pre not done → collect (normal Pre-Survey)", () => {
  assert.equal(preSurveyGate("in_progress", false), "collect");
  assert.equal(preSurveyGate("not_started", false), "collect");
  assert.equal(preSurveyGate(null, false), "collect");
  assert.equal(preSurveyGate(undefined, false), "collect");
});

test("isSurveyCompleted: only 'completed' is completed", () => {
  assert.equal(isSurveyCompleted("completed"), true);
  assert.equal(isSurveyCompleted("in_progress"), false);
  assert.equal(isSurveyCompleted("not_started"), false);
  assert.equal(isSurveyCompleted(null), false);
});

test("postSurveyGate: only a completed package skips Post", () => {
  assert.equal(postSurveyGate("completed"), "completed");
  assert.equal(postSurveyGate("in_progress"), "collect");
  assert.equal(postSurveyGate("not_started"), "collect");
  assert.equal(postSurveyGate(null), "collect");
});

// ---------------------------------------------------------------------------
// Static-source guarantees the pages are wired to the gate and preserve the
// backgrounded-assessment behaviour (read source, same style as
// test-interview-ui.mjs).
// ---------------------------------------------------------------------------
const preSurveyPage = read("src/pages/PreSurveyPage.tsx");
const postSurveyPage = read("src/pages/PostSurveyPage.tsx");
const interviewPage = read("src/pages/InterviewPage.tsx");
const surveyLikert = read("src/components/survey/SurveyLikert.tsx");

test("PreSurveyPage: completed state keeps the normal survey structure and banner", () => {
  assert.match(preSurveyPage, /preSurveyGate\(/);
  assert.match(preSurveyPage, /Pre-Interview Survey/);
  assert.match(preSurveyPage, /PRE_LIKERT\.map/);
  assert.match(preSurveyPage, /styles\.completedBanner/);
  assert.match(preSurveyPage, /already completed the survey for/i);
  assert.match(preSurveyPage, /You do\s+not need to complete it again/);
  assert.match(preSurveyPage, /Skip Survey &amp; Continue to Interview/);
  assert.match(preSurveyPage, /checkingStatus/);
  // Handles the completed race on submit without resubmitting.
  assert.match(preSurveyPage, /survey_already_completed/);
});

test("PreSurveyPage: completed questions are disabled and only Skip advances", () => {
  assert.match(preSurveyPage, /disabled=\{alreadyCompleted\}/);
  assert.match(preSurveyPage, /if \(alreadyCompleted \|\| submitting/);
  assert.match(preSurveyPage, /onClick=\{\(\) => caseId && void proceedToInterview\(caseId\)\}/);
  assert.match(preSurveyPage, /\{!alreadyCompleted && \([\s\S]*?Submit & Start Interview/);
});

test("PostSurveyPage: completed state keeps the normal survey structure and banner", () => {
  assert.match(postSurveyPage, /postSurveyGate\(/);
  assert.match(postSurveyPage, /survey_already_completed/);
  assert.match(postSurveyPage, /Post-Interview Survey/);
  assert.match(postSurveyPage, /POST_LIKERT\.map/);
  assert.match(postSurveyPage, /POST_OPEN_ENDED\.map/);
  assert.match(postSurveyPage, /styles\.completedBanner/);
  assert.match(postSurveyPage, /Survey already completed/);
  assert.match(postSurveyPage, /No additional survey response\s+is required/);
  assert.match(postSurveyPage, /Skip Survey &amp; Continue to Assessment/);
});

test("PostSurveyPage: completed controls are disabled and only Skip advances", () => {
  assert.match(postSurveyPage, /disabled=\{alreadyCompleted\}/);
  assert.match(postSurveyPage, /if \(alreadyCompleted \|\| submitting/);
  assert.match(postSurveyPage, /onClick=\{\(\) => sessionId && void routeToAssessment\(sessionId\)\}/);
  assert.match(postSurveyPage, /\{!alreadyCompleted && \([\s\S]*?Submit & Continue/);
});

test("SurveyLikert: disabled state uses native fieldset semantics", () => {
  assert.match(surveyLikert, /disabled\?: boolean/);
  assert.match(surveyLikert, /<fieldset[\s\S]*?disabled=\{disabled\}/);
  assert.match(surveyLikert, /if \(!disabled\) onChange/);
});

test("CaseIntroductionPage: survey completion does not disable starting a case", () => {
  const caseIntroductionPage = read("src/pages/CaseIntroductionPage.tsx");
  assert.match(caseIntroductionPage, /Start Interview/);
  assert.match(caseIntroductionPage, /navigate\(`\/survey\/\$\{id\}\/pre`\)/);
});

test("InterviewPage: assessment still kicks off at End Interview (backgrounded) before Post-Survey", () => {
  // createAssessment is fired, and navigation goes to the Post-Survey (not the
  // loading screen), so grading runs in the background while Post is shown.
  assert.match(interviewPage, /createAssessment\(/);
  assert.match(interviewPage, /\/survey\/\$\{[^}]+\}\/post/);
});

test("PostSurveyPage: keeps the 'assessment is being prepared in the background' note", () => {
  assert.match(postSurveyPage, /being prepared in the background/i);
});
