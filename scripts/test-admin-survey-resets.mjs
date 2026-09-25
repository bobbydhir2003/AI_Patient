/**
 * Source-level guarantees for the admin Survey Resets page.
 * Run with: npm run test:surveyresets
 *
 * No jsdom in this project, so (like test-admin-student-data.mjs) we assert the
 * behaviour-critical wiring from source.
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
const page = read("src/pages/admin/AdminSurveyResetsPage.tsx");
const api = read("src/services/authApi.ts");

test("sidebar: Survey Resets is the first Super Administration item (super admins only)", () => {
  assert.match(
    sidebar,
    /title: "Super Administration",\n\s*superOnly: true,\n\s*items: \[\n\s*\{ to: "\/superadmin\/survey-resets", label: "Survey Resets", icon: IconRefresh \},/,
  );
  // No longer an Academic Management (normal admin) item.
  assert.doesNotMatch(sidebar, /to: "\/admin\/survey-resets"/);
});

test("route: /superadmin/survey-resets lives under the super_admin-protected layout", () => {
  const start = app.indexOf('path="/superadmin"');
  const block = app.slice(start, app.indexOf("</Route>", start));
  assert.match(block, /<ProtectedRoute role="super_admin">/);
  assert.match(block, /<Route path="survey-resets" element=\{<AdminSurveyResetsPage \/>\} \/>/);
  // The old admin URL only redirects into the guarded Super Admin route.
  assert.match(app, /<Route path="survey-resets" element=\{<Navigate to="\/superadmin\/survey-resets" replace \/>\} \/>/);
});

test("header, banner wording reflects preserved history (never claims deletion)", () => {
  assert.match(page, /<h1 className="pt-h1">Survey Resets<\/h1>/);
  assert.match(page, /Reset pre-survey, post-survey, or both for students when they need to retake survey responses\./);
  assert.match(page, /Existing survey history and prior\s+REDCap responses are preserved/);
  assert.doesNotMatch(page, /cannot be undone|will clear|clear their existing responses/i);
});

test("three bulk actions with the right scopes", () => {
  for (const [scope, title] of [
    ["both", "Reset All Surveys"],
    ["pre", "Reset All Pre Surveys"],
    ["post", "Reset All Post Surveys"],
  ]) {
    assert.match(page, new RegExp(`scope: "${scope}",\\s*title: "${title}"`), title);
  }
  // Clicking a bulk card only opens a confirmation; it never calls the API.
  assert.match(page, /onClick=\{\(\) => setPending\(\{ kind: "bulk", scope: b\.scope \}\)\}/);
});

test("bulk confirmation requires typing RESET before the confirm button enables", () => {
  assert.match(page, /const ready = text\.trim\(\)\.toUpperCase\(\) === "RESET" && affected > 0;/);
  assert.match(page, /disabled=\{!ready \|\| busy\}/);
  assert.match(page, /This will allow all eligible real students to complete/);
  assert.match(page, /<BulkConfirmModal[\s\S]*onConfirm=\{\(\) => doReset\(pending\)\}/);
});

test("per-student Reset Pre / Reset Post / Reset Full each open a confirmation", () => {
  assert.match(page, /setPending\(\{ kind: "row", scope: "pre", row: s \}\)[\s\S]*Reset Pre\n/);
  assert.match(page, /setPending\(\{ kind: "row", scope: "post", row: s \}\)[\s\S]*Reset Post\n/);
  assert.match(page, /setPending\(\{ kind: "row", scope: "both", row: s \}\)[\s\S]*Reset Full\n/);
  // Buttons follow backend eligibility.
  assert.match(page, /disabled=\{!s\.canResetPre \|\| busy\}/);
  assert.match(page, /disabled=\{!s\.canResetPost \|\| busy\}/);
  assert.match(page, /disabled=\{!s\.canResetBoth \|\| busy\}/);
  assert.match(page, /<ConfirmModal[\s\S]*danger[\s\S]*onConfirm=\{\(\) => doReset\(pending\)\}/);
  assert.match(page, /pre: \{ title: \(n\) => `Reset pre-survey for \$\{n\}\?`, label: "Reset Pre Survey" \}/);
});

test("API calls happen only in doReset (reached from a modal) with the chosen scope", () => {
  assert.equal(page.match(/resetStudentSurvey\(/g).length, 1);
  assert.equal(page.match(/bulkResetSurveys\(/g).length, 1);
  assert.match(page, /resetStudentSurvey\(token, p\.row\.studentId, p\.scope\)/);
  assert.match(page, /bulkResetSurveys\(token, p\.scope\)/);
  // doReset is only ever invoked from the two confirm dialogs.
  assert.equal(page.match(/doReset\(/g).length, 3); // definition + 2 onConfirm
});

test("success refreshes from the server; failure shows an error and keeps state", () => {
  const body = page.slice(page.indexOf("async function doReset"), page.indexOf("const summary = data?.summary"));
  const tryPart = body.slice(body.indexOf("try {"), body.indexOf("} catch"));
  const catchPart = body.slice(body.indexOf("} catch"), body.indexOf("} finally"));
  assert.match(tryPart, /refresh\(\);/);
  assert.match(tryPart, /setPending\(null\)/);
  assert.match(catchPart, /toast\.error\(e instanceof ApiError \? e\.message : "Survey reset failed\."\)/);
  assert.doesNotMatch(catchPart, /refresh\(|setPending\(null\)|setData\(/); // no optimistic success
  // Bulk partial failures are surfaced as errors.
  assert.match(tryPart, /if \(res\.failed > 0\) toast\.error\(res\.message\);/);
});

test("list is server-paged/filtered and the summary cards use backend values", () => {
  assert.match(page, /fetchSurveyResets\(token, \{ search: query, status, caseId, page, pageSize \}\)/);
  assert.match(page, /const PAGE_SIZES = \[10, 25, 50\] as const;/);
  assert.match(page, /Rows per page:/);
  for (const label of ["Total Students", "Pre Surveys Completed", "Post Surveys Completed", "Available for Reset"]) {
    assert.ok(page.includes(`label: "${label}"`), label);
  }
  assert.match(page, /value: summary\?\.preCompleted/);
  assert.match(page, /value: summary\?\.availableForReset/);
  for (const col of ["Student", "Email", "Student ID", "Survey Case", "Pre Survey", "Post Survey", "Last Activity"]) {
    assert.ok(page.includes(`<th scope="col">${col}</th>`), col);
  }
  assert.match(page, /onClick=\{refresh\} aria-label="Refresh"/);
});

test("API client: list + bulk endpoints send only the scope", () => {
  assert.match(api, /`\/admin\/survey-resets\?\$\{q\.toString\(\)\}`/);
  assert.match(api, /"\/admin\/survey-resets\/bulk", token, \{\s*method: "POST",\s*body: JSON\.stringify\(\{ scope \}\),/);
});

test("Survey Resets UI never leaks into student-facing code", () => {
  const offenders = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (/\.(tsx?|css)$/.test(name)) {
        const src = readFileSync(full, "utf8");
        if (/bulkResetSurveys|fetchSurveyResets|AdminSurveyResets|survey-resets/.test(src)) offenders.push(relative(root, full));
      }
    }
  };
  walk(join(root, "src"));
  const allowed = new Set([
    "src/App.tsx",
    "src/components/admin/AdminSidebar.tsx",
    "src/services/authApi.ts",
    "src/pages/admin/AdminSurveyResetsPage.tsx",
  ]);
  assert.deepEqual(offenders.filter((f) => !allowed.has(f)), []);
});
