/**
 * Source-level guards for the Priority G merged student dashboard.
 * Run with: npm run test:dashboard
 * Verifies the dashboard is DATA-DRIVEN (real catalog + real sessions), reuses
 * CaseCard for patient images, keeps the real case-start route, and shows no
 * fabricated progress %/profile photo/sample values.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(root, p), "utf8");

const dash = read("src/pages/student/StudentDashboardPage.tsx");
const caseCard = read("src/components/cases/CaseCard.tsx");
const app = read("src/App.tsx");

test("dashboard is data-driven: real catalog + real per-student sessions", () => {
  assert.match(dash, /useCaseCatalog/);
  assert.match(dash, /fetchMySessions/);
});

test("standard cases render via CaseCard (existing patient images)", () => {
  assert.match(dash, /standard\.cases\.map/);
  assert.match(dash, /<CaseCard/);
});

test("CaseCard uses the case's own image and the real case-start route", () => {
  assert.match(caseCard, /src=\{patientCase\.image\}/);
  assert.match(caseCard, /navigate\(`\/cases\/\$\{patientCase\.id\}`\)/);
});

test("counts come from real session data (no hardcoded 12/7/4)", () => {
  assert.match(dash, /sessions\?\.length/);
  assert.match(dash, /s\.status === "completed"/);
  assert.match(dash, /s\.hasAssessment/);
  assert.match(dash, /\{total \?\?/);
  assert.match(dash, /\{completed \?\?/);
  assert.match(dash, /\{assessments \?\?/);
});

test("no fake progress percentage anywhere", () => {
  assert.ok(!/\d+\s*%/.test(dash), "dashboard must not render a numeric percentage");
  assert.ok(!/%\s*complete/i.test(dash), "no '% Complete'");
  assert.ok(!/progress-?(ring|bar|pct|percent)/i.test(dash), "no progress ring/bar");
});

test("continue-session appears only for a real resumable session, with empty state", () => {
  assert.match(dash, /s\.status === "active" && !s\.locked/);
  assert.match(dash, /No active interview to resume/);
});

test("recent activity is real with an empty state", () => {
  assert.match(dash, /No recent activity yet/);
});

test("no fabricated profile photo (initials avatar only)", () => {
  assert.match(dash, /initials\(/);
  assert.ok(!/avatarUrl|profilePhoto|photoUrl|profileImage/i.test(dash));
});

test("referral cases retained and use their existing image", () => {
  assert.match(dash, /Referral &amp; Interprofessional/);
  assert.match(dash, /referral\.cases\.map/);
  assert.match(dash, /src=\{c\.image\}/);
  // no hardcoded patient image paths in the dashboard
  assert.ok(!/\/patients?\//i.test(dash));
});

test("dashboard no longer shows a View All Cases button", () => {
  assert.ok(!/View All Cases/.test(dash));
});

test("existing /cases routes are retained for backward compatibility", () => {
  assert.match(app, /path="\/cases"/);
  assert.match(app, /path="\/cases\/:caseId"/);
});

test("legacy /cases page redirects authenticated students back to the dashboard", () => {
  const catalog = read("src/pages/CaseCatalogPage.tsx");
  assert.match(catalog, /Navigate/);
  assert.match(catalog, /user\?\.role === "student"/);
  assert.match(catalog, /caseHubPath\(user\.role\)/);
});
