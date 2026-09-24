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

test("hook pings ONLY while the page is visible, reusing the visitId", () => {
  // ping() is guarded by a visibility check and forwards the stable visitId.
  assert.match(hook, /const ping = \(\) => \{\s*if \(isVisible\(\)\) void pingAssessmentView\(assessmentId, visitId, source\)/);
  assert.match(hook, /document\.visibilityState === "visible"/);
});

test("hook accepts a stable visitId + source and re-runs when they change", () => {
  assert.match(hook, /assessmentId: string \| null \| undefined,\s*visitId: string,\s*source: AssessmentVisitSource,\s*\): void/);
  assert.match(hook, /\}, \[assessmentId, visitId, source\]\);/);
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

test("ping API sends ONLY the opaque visitId + source enum, best-effort and silent", () => {
  assert.match(api, /method: "POST"/);
  assert.match(api, /\/api\/assessments\/\$\{encodeURIComponent\(assessmentId\)\}\/view\/ping/);
  assert.match(api, /keepalive: true/);
  // Body carries ONLY visitId + source (no duration/seconds/identity keys).
  assert.match(api, /body: JSON\.stringify\(\{ visitId, source \}\)/);
  assert.match(api, /export type AssessmentVisitSource = "initial_assessment" \| "student_dashboard";/);
  assert.doesNotMatch(api, /JSON\.stringify\(\{[^}]*(activeSeconds|seconds|studentId|sessionId)[^}]*\}\)/);
  // Errors are swallowed.
  assert.match(api, /catch \{[\s\S]*?\}/);
});

test("AssessmentReviewPage: one visitId per mount (useRef), passed to the hook", () => {
  assert.match(page, /useRef<string>\(crypto\.randomUUID\(\)\)/);
  // The post-interview review page always records source=initial_assessment.
  assert.match(
    page,
    /useAssessmentViewTracker\(\s*assessment\?\.assessmentId \?\? null,\s*visitIdRef\.current,\s*"initial_assessment",\s*\)/,
  );
  assert.doesNotMatch(page, /viewing time/i);
});

test("StudentSessionPage: visitId per mount AND tracking gated to the Assessment tab", () => {
  const sessionPage = read("src/pages/student/StudentSessionPage.tsx");
  assert.match(sessionPage, /import \{ useAssessmentViewTracker \} from/);
  assert.match(sessionPage, /useRef<string>\(crypto\.randomUUID\(\)\)/);
  // Gated: only the Assessment tab tracks; transcript tab passes null (inert).
  assert.match(
    sessionPage,
    /useAssessmentViewTracker\(\s*tab === "assessment" \? \(assessment\?\.assessmentId \?\? null\) : null,\s*visitIdRef\.current,\s*"student_dashboard",\s*\)/,
  );
  assert.doesNotMatch(sessionPage, /viewing time/i);
});

test("Admin Assessments table shows time / views / first / last viewed and opts in", () => {
  const admin = read("src/pages/admin/AdminAssessmentsPage.tsx");
  assert.match(admin, /withViewTime: true/);
  assert.match(admin, /Active viewing time/);
  assert.match(admin, /<th scope="col">Views<\/th>/);
  assert.match(admin, /First viewed/);
  assert.match(admin, /Last viewed/);
  assert.match(admin, /s\.activeViewingSeconds != null \? fmtDuration\(s\.activeViewingSeconds\) : "—"/);
  assert.match(admin, /s\.viewCount \?\? "—"/);
  assert.match(admin, /s\.firstViewedAt \? fmtDateTime\(s\.firstViewedAt\) : "—"/);
  assert.match(admin, /s\.lastViewedAt \? fmtDateTime\(s\.lastViewedAt\) : "—"/);
});
