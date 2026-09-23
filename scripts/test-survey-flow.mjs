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
  globalPreGate,
  globalPostGate,
  isResumableSession,
  resumeEntryDestination,
  resolveInterviewDestination,
  destinationToPath,
  isSessionNotFound,
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
// GLOBAL survey gating (one package per student). isOwnerCase + globalStatus.
// ---------------------------------------------------------------------------
test("globalPreGate: not_started → collect (this case may claim ownership)", () => {
  assert.equal(globalPreGate("not_started", false, false), "collect");
  assert.equal(globalPreGate("not_started", true, false), "collect");
});

test("globalPreGate: owner case → collect if Pre pending, continue if Pre done", () => {
  assert.equal(globalPreGate("in_progress", true, false), "collect");
  assert.equal(globalPreGate("in_progress", true, true), "continue");
  assert.equal(globalPreGate("completed", true, true), "continue");
});

test("globalPreGate: non-owner (in progress or completed) → skip", () => {
  assert.equal(globalPreGate("in_progress", false, false), "skip");
  assert.equal(globalPreGate("completed", false, false), "skip");
});

test("globalPostGate: only the owning, not-yet-completed case collects Post", () => {
  assert.equal(globalPostGate("in_progress", true), "collect");
  assert.equal(globalPostGate("completed", true), "skip");
  assert.equal(globalPostGate("in_progress", false), "skip");
  assert.equal(globalPostGate("completed", false), "skip");
  assert.equal(globalPostGate("not_started", false), "skip");
});

// ---------------------------------------------------------------------------
// Centralized interview flow resolution (new / resume / stale / refresh).
// ---------------------------------------------------------------------------
const ACTIVE = { status: "active", locked: false, hasAssessment: false };
const LOCKED = { status: "active", locked: true, hasAssessment: false };
const DONE_NO_ASSESS = { status: "completed", locked: true, hasAssessment: false };
const DONE_ASSESS = { status: "completed", locked: true, hasAssessment: true };

test("isResumableSession: only ACTIVE + unlocked is resumable", () => {
  assert.equal(isResumableSession(ACTIVE), true);
  assert.equal(isResumableSession(LOCKED), false);
  assert.equal(isResumableSession(DONE_NO_ASSESS), false);
  assert.equal(isResumableSession(null), false);
});

test("resumeEntryDestination: active resume ENTERS at Case Info, not the interview", () => {
  assert.deepEqual(resumeEntryDestination(ACTIVE), { kind: "caseInfo" });
  assert.deepEqual(resumeEntryDestination(DONE_ASSESS), { kind: "assessment" });
  assert.deepEqual(resumeEntryDestination(DONE_NO_ASSESS), { kind: "postSurvey" });
});

test("resolveInterviewDestination: active → interview; stale → post/assessment", () => {
  assert.deepEqual(resolveInterviewDestination(ACTIVE), { kind: "interview" });
  assert.deepEqual(resolveInterviewDestination(LOCKED), { kind: "postSurvey" });
  assert.deepEqual(resolveInterviewDestination(DONE_ASSESS), { kind: "assessment" });
  assert.deepEqual(resolveInterviewDestination(null), { kind: "caseInfo" });
});

test("destinationToPath: builds the same routes for every caller", () => {
  const ids = { caseId: "carly", sessionId: "sess123" };
  assert.equal(destinationToPath({ kind: "caseInfo" }, ids), "/cases/carly");
  assert.equal(destinationToPath({ kind: "interview" }, ids), "/interview/carly");
  assert.equal(destinationToPath({ kind: "postSurvey" }, ids), "/survey/sess123/post");
  assert.equal(destinationToPath({ kind: "assessmentLoading" }, ids), "/assessment/sess123/loading");
  assert.equal(destinationToPath({ kind: "assessment" }, ids), "/assessment/sess123");
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

test("PreSurveyPage: owner-aware skip/continue messaging (A-D)", () => {
  assert.match(preSurveyPage, /globalPreGate\(/);
  assert.match(preSurveyPage, /Pre-Interview Survey/);
  assert.match(preSurveyPage, /PRE_LIKERT\.map/);
  assert.match(preSurveyPage, /styles\.completedBanner/);
  // Owner name comes from the backend field, with a safe fallback (no crash).
  assert.match(preSurveyPage, /status\.surveyOwnerCaseName/);
  assert.match(preSurveyPage, /ownerName \|\| "your original survey case"/);
  assert.match(preSurveyPage, /identity\?\.caseName \|\| patientCase\?\.name \|\| "this case"/);
  // Case A — owner, Pre done, Post pending (NOT "completed").
  assert.match(preSurveyPage, /Pre-Survey already submitted/);
  assert.match(preSurveyPage, /Complete this interview to finish the Post-Survey afterward\./);
  // Case B — non-owner, in progress.
  assert.match(preSurveyPage, /Survey already assigned to \$\{ownerLabel\}/);
  assert.match(preSurveyPage, /Only the \$\{ownerLabel\} case will collect your survey responses/);
  // Case C / D — completed.
  assert.match(preSurveyPage, /Survey already completed/);
  assert.match(preSurveyPage, /You already completed your survey with \$\{ownerLabel\}/);
  // Buttons + branching on completed vs in-progress.
  assert.match(preSurveyPage, /Skip Survey & Continue to Interview/);
  assert.match(preSurveyPage, /Continue to Interview/);
  assert.match(preSurveyPage, /globalStatus === "completed"/);
  assert.match(preSurveyPage, /checkingStatus/);
  // Handles both race codes on submit without resubmitting.
  assert.match(preSurveyPage, /survey_already_completed/);
  assert.match(preSurveyPage, /survey_owned_by_other_case/);
});

test("PreSurveyPage: Pre questions render ONLY when not skip/continue", () => {
  // The Likert card is gated behind !readOnly (hidden, not read-only, in the
  // skip/continue state).
  assert.match(preSurveyPage, /\{!readOnly && \([\s\S]*?PRE_LIKERT\.map/);
  // The identity (NUID + case) block is NOT gated by readOnly and stays visible.
  assert.match(preSurveyPage, /aria-label="Your survey identity"/);
  assert.match(preSurveyPage, /Student NUID/);
  // No disabled read-only questions remain in the skip state.
  assert.doesNotMatch(preSurveyPage, /disabled=\{readOnly\}/);
});

test("PreSurveyPage: resume mode reuses session + skips the queue", () => {
  // Resume mode is driven by the persisted resume flag, never a bare active
  // session, and only for the matching case.
  assert.match(preSurveyPage, /activeInterview\.resume === true && activeInterview\.caseId === caseId/);
  // NEW flow creates a session; RESUME reuses it (no createSession under resume).
  assert.match(preSurveyPage, /if \(resumeMode && activeInterview\)/);
  assert.match(preSurveyPage, /createSession\(/);
  // Resume proceeds WITHOUT joinQueue; new flow uses joinQueue.
  assert.match(preSurveyPage, /if \(resumeMode\) \{[\s\S]*?resolveInterviewDestination/);
  assert.match(preSurveyPage, /joinQueue\(token, id\)/);
  assert.match(preSurveyPage, /readOnly/);
});

test("PostSurveyPage: owner-aware skip messaging (B/C)", () => {
  assert.match(postSurveyPage, /globalPostGate\(/);
  assert.match(postSurveyPage, /survey_already_completed/);
  assert.match(postSurveyPage, /survey_owned_by_other_case/);
  assert.match(postSurveyPage, /Post-Interview Survey/);
  assert.match(postSurveyPage, /POST_LIKERT\.map/);
  assert.match(postSurveyPage, /POST_OPEN_ENDED\.map/);
  assert.match(postSurveyPage, /styles\.completedBanner/);
  // Owner name from backend + safe fallback + current case fallback.
  assert.match(postSurveyPage, /status\.surveyOwnerCaseName/);
  assert.match(postSurveyPage, /ownerName \|\| "your original survey case"/);
  // Case B — non-owner, owner survey in progress.
  assert.match(postSurveyPage, /No additional survey required/);
  assert.match(postSurveyPage, /Your survey is being completed with \$\{ownerLabel\}/);
  // Case C / D — completed.
  assert.match(postSurveyPage, /Survey already completed/);
  assert.match(postSurveyPage, /No additional survey is required for \$\{currentCaseName\}/);
  assert.match(postSurveyPage, /globalStatus === "completed"/);
  assert.match(postSurveyPage, /Skip Survey &amp; Continue to Assessment/);
});

test("PostSurveyPage: Likert + open-ended render ONLY when not skip", () => {
  // Both survey cards are gated behind !alreadyCompleted (hidden in skip state).
  assert.match(postSurveyPage, /\{!alreadyCompleted && \([\s\S]*?POST_LIKERT\.map/);
  assert.match(postSurveyPage, /\{!alreadyCompleted && \([\s\S]*?POST_OPEN_ENDED\.map/);
  // No disabled read-only questions remain in the skip state.
  assert.doesNotMatch(postSurveyPage, /disabled=\{alreadyCompleted\}/);
  // Only Skip advances from the completed state.
  assert.match(postSurveyPage, /onClick=\{\(\) => sessionId && void routeToAssessment\(sessionId\)\}/);
  assert.match(postSurveyPage, /\{!alreadyCompleted && \([\s\S]*?Submit & Continue/);
});

test("Dashboard resume uses the centralized resolver, not a hard /interview jump", () => {
  const dashboard = read("src/pages/student/StudentDashboardPage.tsx");
  assert.match(dashboard, /resume: true/);
  assert.match(dashboard, /resumeEntryDestination\(/);
  assert.match(dashboard, /destinationToPath\(/);
  // The old hard-coded jump is gone.
  assert.doesNotMatch(dashboard, /navigate\(`\/interview\/\$\{session\.caseId\}`\)/);
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

// ---------------------------------------------------------------------------
// Stale/dead interview session handling ("session_not_found") + login cleanup.
// ---------------------------------------------------------------------------
test("isSessionNotFound: only the backend 'session_not_found' code is treated as dead", () => {
  assert.equal(isSessionNotFound({ code: "session_not_found" }), true);
  // A real ApiError-shaped object (has status + code) is matched by code only.
  assert.equal(isSessionNotFound({ status: 404, code: "session_not_found" }), true);
  // Generic / transient / other errors are NOT treated as a dead session.
  assert.equal(isSessionNotFound({ code: "network_error" }), false);
  assert.equal(isSessionNotFound({ code: "unknown_error" }), false);
  assert.equal(isSessionNotFound(new Error("boom")), false);
  assert.equal(isSessionNotFound(null), false);
  assert.equal(isSessionNotFound(undefined), false);
  assert.equal(isSessionNotFound("session_not_found"), false); // a bare string is not an error object
});

test("AuthContext: stale app-state is cleared on login, logout AND token expiry", () => {
  const auth = read("src/state/AuthContext.tsx");
  // A single shared helper removes the persisted interview/app state...
  assert.match(auth, /function clearPersistedAppState\(\)/);
  assert.match(auth, /removeItem\(APP_STATE_KEY\)/);
  assert.match(auth, /APP_STATE_KEY = "ptai-app-state"/);
  // ...and is invoked on login (before the token/user take effect)...
  assert.match(auth, /const res = await apiLogin\(email, password\);[\s\S]*?clearPersistedAppState\(\);/);
  // ...on the expired/invalid persisted-token path...
  assert.match(auth, /persistToken\(null\);[\s\S]*?setUser\(null\);[\s\S]*?clearPersistedAppState\(\);/);
  // ...and on explicit logout.
  assert.match(auth, /const logout = useCallback\([\s\S]*?clearPersistedAppState\(\);/);
  // Token persistence itself is untouched (still keyed separately).
  assert.match(auth, /TOKEN_KEY = "ptai-auth-token"/);
});

test("PreSurveyPage: a dead persisted session id is discarded + recovered, never surveyed", () => {
  // Distinguishes the specific 404 from generic errors.
  assert.match(preSurveyPage, /isSessionNotFound\(err\)/);
  // On session_not_found: discard the stale pointer and create ONE fresh session.
  assert.match(preSurveyPage, /recoveredRef\.current/);
  assert.match(preSurveyPage, /setActiveInterview\(null\)/);
  assert.match(preSurveyPage, /isSessionNotFound\(err\) && !recoveredRef\.current[\s\S]*?createSession\(/);
  // Re-reads status with the fresh, valid id (validation-before-trust).
  assert.match(preSurveyPage, /await loadGate\(session\.sessionId, cid\)/);
  // Non-404 keeps the previous best-effort collect behavior.
  assert.match(preSurveyPage, /setGateMode\("collect"\)/);
});

test("PostSurveyPage: session_not_found shows recovery panel, never a submittable survey", () => {
  // The dead-session state is explicit and separate from alreadyCompleted.
  assert.match(postSurveyPage, /sessionMissing/);
  assert.match(postSurveyPage, /isSessionNotFound\(err\)/);
  // On the dead session: clear stale pointer + flip to the recovery panel + early return.
  assert.match(postSurveyPage, /isSessionNotFound\(err\)\)\s*\{[\s\S]*?setActiveInterview\(null\)[\s\S]*?setSessionMissing\(true\)[\s\S]*?return;/);
  // The recovery panel routes back to the dashboard/case hub (no survey, no submit).
  assert.match(postSurveyPage, /if \(sessionMissing\) \{/);
  assert.match(postSurveyPage, /no longer available/i);
  assert.match(postSurveyPage, /caseHubPath\(user\?\.role\)/);
});

// ---------------------------------------------------------------------------
// Follow-up: submit-time stale session + friendly required-question messaging.
// ---------------------------------------------------------------------------
const FRIENDLY = /Please answer all required questions before continuing\./;

test("PreSurveyPage: submit-time session_not_found recovers (fresh session, re-check, retry once)", () => {
  // The submit handler now handles the specific 404 (not just initial load) and
  // NEVER shows the raw backend message for it.
  assert.match(preSurveyPage, /err\.code === "session_not_found" &&\s*!submitRecoveredRef\.current/);
  assert.match(preSurveyPage, /submitRecoveredRef\.current = true;/);
  // Discard the dead id, create ONE fresh session, re-check status, and only then
  // (if still collecting) submit once against the fresh id.
  assert.match(preSurveyPage, /setActiveInterview\(null\);[\s\S]*?createSession\(/);
  assert.match(preSurveyPage, /await getSurveyStatus\(session\.sessionId\)/);
  assert.match(preSurveyPage, /gate === "collect"[\s\S]*?submitPreSurvey\(session\.sessionId, payload\)/);
  // payload is built BEFORE the try so it is reusable by the recovery retry.
  assert.match(preSurveyPage, /const payload: Record<string, number \| string> = \{ \.\.\.answers \};[\s\S]*?try \{/);
});

test("PreSurveyPage: required validation is friendly, blocks the API, and maps 422", () => {
  // Exact friendly copy; old generic copy is gone.
  assert.match(preSurveyPage, FRIENDLY);
  assert.doesNotMatch(preSurveyPage, /Please answer all questions before continuing\./);
  // Missing answers short-circuit BEFORE any API call (focus + return, no submit).
  assert.match(preSurveyPage, /if \(likertMissing \|\| openMissing\) \{[\s\S]*?focusFirstMissing\(\);\s*return;/);
  // A backend 422 backstop is mapped to the SAME friendly message (never raw text).
  assert.match(preSurveyPage, /err\.status === 422[\s\S]*?Please answer all required questions/);
  assert.match(preSurveyPage, /function focusFirstMissing\(\)/);
});

test("PostSurveyPage: submit-time session_not_found → recovery panel + friendly validation/422", () => {
  // Submit-time dead session flips to the recovery panel (no raw error, no 404 submit).
  assert.match(postSurveyPage, /err\.code === "session_not_found"\)\s*\{[\s\S]*?setSessionMissing\(true\)/);
  // Exact friendly copy; old generic copy is gone; API blocked before call.
  assert.match(postSurveyPage, FRIENDLY);
  assert.doesNotMatch(postSurveyPage, /Please answer all questions before continuing\./);
  assert.match(postSurveyPage, /if \(likertMissing \|\| openMissing\) \{[\s\S]*?focusFirstMissing\(\);\s*return;/);
  assert.match(postSurveyPage, /err\.status === 422[\s\S]*?Please answer all required questions/);
  assert.match(postSurveyPage, /function focusFirstMissing\(\)/);
});

test("Both survey pages surface the error near the TOP of the survey (role=alert)", () => {
  // A top-of-survey error region exists directly inside the main survey column.
  assert.match(preSurveyPage, /<div className=\{styles\.main\}>\s*\{\/\*[\s\S]*?\*\/\}\s*\{error && \(/);
  assert.match(postSurveyPage, /<div className=\{styles\.main\}>\s*\{\/\*[\s\S]*?\*\/\}\s*\{error && \(/);
});
