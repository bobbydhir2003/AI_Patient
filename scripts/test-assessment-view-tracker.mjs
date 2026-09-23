/**
 * Source-level guarantees for the silent assessment view-time tracker.
 * Run with: npm run test:assessmentview
 *
 * The hook is effect/timer/DOM driven (no jsdom in this project), so we assert
 * the safety-critical behaviors from source, matching the repo's existing
 * source-guard test style (see test-student-dashboard.mjs).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

const hook = read("src/hooks/useAssessmentViewTracker.ts");
const api = read("src/services/assessmentViewApi.ts");
const page = read("src/pages/AssessmentReviewPage.tsx");

test("hook pings ONLY while the page is visible", () => {
  // ping() is guarded by a visibility check, and visibility uses the standard API.
  assert.match(hook, /const ping = \(\) => \{\s*if \(isVisible\(\)\) void pingAssessmentView\(assessmentId\)/);
  assert.match(hook, /document\.visibilityState === "visible"/);
});

test("hidden tab pauses; visible resumes (visibilitychange wired to start/stop)", () => {
  assert.match(hook, /addEventListener\("visibilitychange", onVisibility\)/);
  assert.match(hook, /const onVisibility = \(\) => \{\s*if \(isVisible\(\)\) start\(\);\s*else stop\(\);/);
});

test("heartbeat interval is 20s and only one interval runs at a time", () => {
  assert.match(hook, /HEARTBEAT_MS = 20_000/);
  assert.match(hook, /setInterval\(ping, HEARTBEAT_MS\)/);
  // start() is idempotent (won't stack intervals on repeated visible events).
  assert.match(hook, /if \(timerRef\.current !== null\) return;/);
});

test("cleanup removes the listener AND clears the interval", () => {
  assert.match(hook, /return \(\) => \{\s*document\.removeEventListener\("visibilitychange", onVisibility\);\s*stop\(\);/);
  assert.match(hook, /window\.clearInterval\(timerRef\.current\)/);
});

test("hook is inert with no assessmentId and renders nothing (returns void)", () => {
  assert.match(hook, /if \(!assessmentId\) return;/);
  assert.match(hook, /export function useAssessmentViewTracker\([^)]*\): void/);
});

test("ping API is best-effort and silent (never throws to the student)", () => {
  assert.match(api, /method: "POST"/);
  assert.match(api, /\/api\/assessments\/\$\{encodeURIComponent\(assessmentId\)\}\/view\/ping/);
  assert.match(api, /keepalive: true/);
  // Errors are swallowed; no body/duration is ever sent.
  assert.match(api, /catch \{[\s\S]*?\}/);
  assert.doesNotMatch(api, /body:/);
});

test("AssessmentReviewPage mounts the tracker with the assessment id, no UI", () => {
  assert.match(page, /useAssessmentViewTracker\(assessment\?\.assessmentId \?\? null\)/);
  // No visible timer text is introduced.
  assert.doesNotMatch(page, /viewing time/i);
});

test("StudentSessionPage (dashboard reopen) also mounts the tracker, silently", () => {
  const sessionPage = read("src/pages/student/StudentSessionPage.tsx");
  assert.match(sessionPage, /import \{ useAssessmentViewTracker \} from/);
  assert.match(sessionPage, /useAssessmentViewTracker\(assessment\?\.assessmentId \?\? null\)/);
  // Silent: no student-facing timer/tracking text on this page either.
  assert.doesNotMatch(sessionPage, /viewing time/i);
});

test("Admin Assessments table shows viewing time / views / first viewed and opts in", () => {
  const admin = read("src/pages/admin/AdminAssessmentsPage.tsx");
  // Requests the admin-only timing columns via the batched backend flag.
  assert.match(admin, /withViewTime: true/);
  // New compact columns.
  assert.match(admin, /Active viewing time/);
  assert.match(admin, /<th scope="col">Views<\/th>/);
  assert.match(admin, /First viewed/);
  // Formats duration and handles never-viewed with a consistent dash.
  assert.match(admin, /s\.activeViewingSeconds != null \? fmtDuration\(s\.activeViewingSeconds\) : "—"/);
  assert.match(admin, /s\.viewCount \?\? "—"/);
  assert.match(admin, /s\.firstViewedAt \? fmtDateTime\(s\.firstViewedAt\) : "—"/);
});
