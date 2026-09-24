/**
 * Source-level guarantees for the admin Student Data page.
 * Run with: npm run test:studentdata
 *
 * No jsdom in this project, so (like test-student-dashboard.mjs and
 * test-assessment-view-tracker.mjs) we assert the behaviour-critical wiring from
 * source.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, relative } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

const sidebar = read("src/components/admin/AdminSidebar.tsx");
const app = read("src/App.tsx");
const page = read("src/pages/admin/AdminStudentDataPage.tsx");
const detail = read("src/pages/admin/StudentDataDetail.tsx");
const api = read("src/services/authApi.ts");

test("sidebar: Student Data sits right after Dashboard under Academic Management", () => {
  assert.match(
    sidebar,
    /\{ to: "\/admin", label: "Dashboard"[^\n]*\n\s*\{ to: "\/admin\/student-data", label: "Student Data", icon: IconStudentData \},\n\s*\{ to: "\/admin\/students", label: "Students"/,
  );
});

test("routes: list and per-student deep link live under the admin-protected layout", () => {
  const adminBlock = app.slice(app.indexOf('path="/admin"'), app.indexOf("</Route>", app.indexOf('path="/admin"')));
  assert.match(adminBlock, /<ProtectedRoute role="admin">/);
  assert.match(adminBlock, /<Route path="student-data" element=\{<AdminStudentDataPage \/>\} \/>/);
  assert.match(adminBlock, /<Route path="student-data\/:studentId" element=\{<AdminStudentDataPage \/>\} \/>/);
});

test("list: first view is the student list; detail renders only once a student is selected", () => {
  assert.match(page, /const selected = studentId \?\? null;/);
  assert.match(page, /\{selected && \(\s*<section className=\{styles\.detailPane\}/);
  assert.match(page, /onSelect=\{\(id\) => navigate\(`\/admin\/student-data\/\$\{id\}`\)\}/);
});

test("list: paged + searchable + loading/empty/error states; no bulk history fetch", () => {
  assert.match(page, /fetchStudentDataList\(token, \{ search: query, status, sort, page, pageSize: PAGE_SIZE \}\)/);
  assert.match(page, /Search name, student ID or email/);
  assert.match(page, /<LoadingState label="Loading students…" \/>/);
  assert.match(page, /<EmptyState\s+title="No students found"/);
  assert.match(page, /<ErrorState message=\{error\}/);
  assert.doesNotMatch(page, /fetchStudentDataDetail/); // detail is fetched per selected student only
});

test("detail: fetches one student and renders history, visits, survey and timeline", () => {
  assert.match(detail, /fetchStudentDataDetail\(token, studentId\)/);
  for (const heading of [
    "Session &amp; Activity History",
    "Student Activity Summary",
    "Survey Controls",
    "Recent Activity Timeline",
  ]) {
    assert.ok(detail.includes(heading), heading);
  }
  for (const col of ["Date", "Case", "Duration", "Questions", "AI assessment", "View time", "Visits", "Survey", "Actions"]) {
    assert.ok(detail.includes(`<th scope="col">${col}</th>`), col);
  }
});

test("session actions reuse the existing admin session/transcript/assessment routes", () => {
  assert.match(detail, /navigate\(`\/admin\/sessions\/\$\{s\.sessionId\}\?tab=transcript`\)/);
  assert.match(detail, /navigate\(`\/admin\/sessions\/\$\{s\.sessionId\}\?tab=assessment`\)/);
  assert.match(detail, /navigate\(`\/admin\/sessions\/\$\{s\.sessionId\}`\)/);
});

test("visit history: compact Visit/Source/Started/Active time table with source labels", () => {
  assert.match(detail, /initial_assessment: "After Interview Report"/);
  assert.match(detail, /student_dashboard: "Student Dashboard"/);
  assert.match(detail, /legacy: "Historical \(Before Visit Tracking\)"/);
  for (const col of ["Visit", "Source", "Started", "Active time"]) {
    assert.ok(detail.includes(`<th scope="col">${col}</th>`), col);
  }
  assert.match(detail, /aria-expanded=\{open\}/);
});

test("AI Assessment Activity: dedicated section, per-case numbering, reuses VisitHistory", () => {
  // A dedicated section rendered from the SAME session data (no new API).
  assert.match(detail, /function AiAssessmentActivity\(\{ sessions \}/);
  assert.match(detail, /<AiAssessmentActivity sessions=\{data\.sessions\} \/>/);
  assert.match(detail, /AI Assessment Activity<\/h3>/);
  // Only sessions that generated an assessment, numbered per case in gen order.
  assert.match(detail, /sessions\.filter\(\(s\) => s\.hasAssessment\)/);
  assert.match(detail, /Assessment #\$\{num\}/);
  // Columns for the summary row and expansion into the existing per-visit table.
  for (const col of ["Assessment", "Generated", "Result", "Views", "Active time", "Last viewed"]) {
    assert.ok(detail.includes(`<th scope="col">${col}</th>`), col);
  }
  assert.match(detail, /<VisitHistory session=\{s\} \/>/);
  // Never-viewed assessments still appear, truthfully.
  assert.match(detail, /Never viewed/);
});

test("survey reset: every scope goes through a confirmation dialog before the API call", () => {
  assert.match(detail, /onReset=\{\(\) => setConfirm\("pre"\)\}/);
  assert.match(detail, /onReset=\{\(\) => setConfirm\("post"\)\}/);
  assert.match(detail, /onClick=\{\(\) => setConfirm\("both"\)\}/);
  assert.match(detail, /<ConfirmModal[\s\S]*onConfirm=\{\(\) => doReset\(confirm\)\}/);
  assert.match(detail, /Reset this student's Post Survey\?/);
  // The only caller of the reset API is doReset (reached from the modal).
  assert.equal(detail.match(/resetStudentSurvey\(/g).length, 1);
  assert.match(detail, /This allows the student to complete the survey again\./);
});

test("reset API sends only the scope; the student comes from the route", () => {
  assert.match(api, /`\/admin\/student-data\/\$\{encodeURIComponent\(studentId\)\}\/survey-reset`/);
  assert.match(api, /body: JSON\.stringify\(\{ scope \}\)/);
});

test("no Student Data / survey-reset UI leaks into student-facing code", () => {
  const offenders = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (/\.(tsx?|css)$/.test(name)) {
        const src = readFileSync(full, "utf8");
        if (/resetStudentSurvey|StudentDataDetail|student-data/.test(src)) offenders.push(relative(root, full));
      }
    }
  };
  walk(join(root, "src"));
  const allowed = new Set([
    "src/App.tsx",
    "src/components/admin/AdminSidebar.tsx",
    "src/services/authApi.ts",
    "src/pages/admin/AdminStudentDataPage.tsx",
    "src/pages/admin/StudentDataDetail.tsx",
  ]);
  assert.deepEqual(offenders.filter((f) => !allowed.has(f)), []);
});
