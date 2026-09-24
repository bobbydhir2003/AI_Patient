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

test("active period: resume on enter, heartbeat while visible, pause on leave", () => {
  // Entering an active period sends resume (server credits 0); the interval only
  // heartbeats while visible.
  assert.match(hook, /activeRef\.current = true;\s*send\("resume"\);/);
  assert.match(hook, /setInterval\(\(\) => \{\s*if \(isVisible\(\)\) send\("heartbeat"\);\s*\}, HEARTBEAT_MS\)/);
  // Leaving banks the final partial interval with pause (NOT visibility-guarded).
  assert.match(hook, /activeRef\.current = false;[\s\S]*?send\("pause"\);/);
  assert.match(hook, /document\.visibilityState === "visible"/);
});

test("hook accepts a stable visitId + source and re-runs when they change", () => {
  assert.match(hook, /assessmentId: string \| null \| undefined,\s*visitId: string,\s*source: AssessmentVisitSource,\s*\): void/);
  assert.match(hook, /\}, \[assessmentId, visitId, source\]\);/);
});

test("hidden tab pauses; visible resumes; pagehide also pauses (best-effort tail)", () => {
  assert.match(hook, /addEventListener\("visibilitychange", onVisibility\)/);
  assert.match(hook, /addEventListener\("pagehide", onPageHide\)/);
  assert.match(hook, /const onVisibility = \(\) => \{\s*if \(isVisible\(\)\) startActive\(\);\s*else pauseActive\(\);/);
  assert.match(hook, /const onPageHide = \(\) => pauseActive\(\);/);
});

test("heartbeat interval is 20s and only one active period runs at a time", () => {
  assert.match(hook, /HEARTBEAT_MS = 20_000/);
  // startActive is idempotent (won't stack intervals on repeated visible events).
  assert.match(hook, /if \(activeRef\.current\) return;/);
  // pauseActive is idempotent (won't send a second pause on the leave burst).
  assert.match(hook, /if \(!activeRef\.current\) return;/);
});

test("cleanup removes BOTH listeners AND banks the final pause", () => {
  assert.match(
    hook,
    /return \(\) => \{\s*document\.removeEventListener\("visibilitychange", onVisibility\);\s*window\.removeEventListener\("pagehide", onPageHide\);\s*pauseActive\(\);/,
  );
  assert.match(hook, /window\.clearInterval\(timerRef\.current\)/);
});

test("hook is inert with no assessmentId and renders nothing (returns void)", () => {
  assert.match(hook, /if \(!assessmentId\) return;/);
  assert.match(hook, /export function useAssessmentViewTracker\([^)]*\): void/);
});

test("ping API sends ONLY the opaque visitId + source + event, best-effort and silent", () => {
  assert.match(api, /method: "POST"/);
  assert.match(api, /\/api\/assessments\/\$\{encodeURIComponent\(assessmentId\)\}\/view\/ping/);
  assert.match(api, /keepalive: true/);
  // Body carries ONLY visitId + source + event (no duration/seconds/identity keys).
  assert.match(api, /body: JSON\.stringify\(\{ visitId, source, event \}\)/);
  assert.match(api, /export type AssessmentVisitSource = "initial_assessment" \| "student_dashboard";/);
  assert.match(api, /export type AssessmentViewEvent = "heartbeat" \| "pause" \| "resume";/);
  assert.doesNotMatch(api, /JSON\.stringify\(\{[^}]*(activeSeconds|duration|seconds|studentId|sessionId)[^}]*\}\)/);
  // Errors are swallowed.
  assert.match(api, /catch \{[\s\S]*?\}/);
});

test("AssessmentReviewPage: one visitId per mount (useRef) AND tracking paused on the Transcript tab", () => {
  assert.match(page, /useRef<string>\(crypto\.randomUUID\(\)\)/);
  // The post-interview review page records source=initial_assessment, and — like
  // StudentSessionPage — gates tracking so the Transcript tab passes null (inert),
  // so reading the transcript never counts as assessment-view time.
  assert.match(
    page,
    /useAssessmentViewTracker\(\s*tab === "transcript" \? null : \(assessment\?\.assessmentId \?\? null\),\s*visitIdRef\.current,\s*"initial_assessment",\s*\)/,
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
