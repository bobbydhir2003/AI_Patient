/**
 * Source-level guards for the Priority H AI Assessment Review layout refactor.
 * Run with: npm run test:assessment
 * Verifies the redesign is PRESENTATION-ONLY: it reuses the existing components,
 * renders real backend assessment fields, keeps the tabs/buttons, grids the
 * rubric + improvement sections, and introduces no numeric scores.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(root, p), "utf8");

const page = read("src/pages/AssessmentReviewPage.tsx");
const pageCss = read("src/pages/AssessmentReviewPage.module.css");
const overall = read("src/components/assessment/OverallImpression.tsx");
const method = read("src/components/assessment/AssessmentMethodPanel.tsx");
const header = read("src/components/assessment/AssessmentHeader.tsx");
const focus = read("src/components/assessment/FocusAreas.tsx");
const rubric = read("src/components/assessment/RubricCard.tsx");
const sharedCss = read("src/components/assessment/assessment.module.css");
const all = [page, overall, method, header, focus, rubric].join("\n");

test("reuses existing assessment components (no duplicated logic)", () => {
  for (const c of ["OverallImpression", "RubricCard", "FocusAreas", "AssessmentMethodPanel", "TranscriptReview", "AssessmentHeader"]) {
    assert.match(page, new RegExp(c));
  }
});

test("renders the real case details + existing patient image", () => {
  assert.match(page, /patientCase\.name/);
  assert.match(page, /src=\{patientCase\.image\}/);
});

test("Overall AI Impression renders the actual backend text", () => {
  assert.match(overall, /\{assessment\.overallSummary\}/);
});

test("four rubric results render from real domains via RubricCard", () => {
  assert.match(page, /assessment\.domains\.map/);
  assert.match(page, /<RubricCard/);
  assert.match(rubric, /level=\{domain\.performanceLevel\}/);
});

test("transcript moment counts use the real evidence length", () => {
  assert.match(rubric, /domain\.evidence\.length/);
  assert.match(rubric, /transcript moment/);
});

test("improvement opportunities use real focusAreas data", () => {
  assert.match(page, /assessment\.focusAreas/);
  assert.match(focus, /areas\.map/);
  assert.match(focus, /area\.whyItMatters/);
  assert.match(focus, /area\.suggestedPractice/);
});

test("metadata uses real backend fields (not hardcoded sample values)", () => {
  for (const f of ["modelName", "caseVersion", "rubricVersion", "promptVersion", "verificationStatus"]) {
    assert.match(method, new RegExp(`assessment\\.${f}`));
  }
  assert.ok(!/gpt-4o-mini/.test(method), "model must not be hardcoded");
  assert.ok(!/NEEDS_REVIEW/.test(method), "verification must not be hardcoded");
  // missing metadata falls back to a clean dash
  assert.match(method, /\?\? "—"/);
});

test("no numeric scores / percentages / progress introduced", () => {
  assert.ok(!/\d+\s*%/.test(all), "no percentage");
  assert.ok(!/\b\d+\s*\/\s*10\b/.test(all), "no x/10 score");
  assert.ok(!/progress-?(ring|bar|pct|percent)/i.test(all), "no progress ring/bar");
});

test("all three primary tabs are present", () => {
  assert.match(header, /Overview/);
  assert.match(header, /Rubric Review/);
  assert.match(header, /Transcript Review/);
});

test("View Full Transcript and Try Another Case remain", () => {
  assert.match(page, /View Full Transcript/);
  assert.match(page, /Try Another Case/);
});

test("layout refactor: rubric 2x2 grid + focus multi-column grid exist", () => {
  assert.match(page, /styles\.rubricGrid/);
  assert.match(pageCss, /\.rubricGrid\s*\{/);
  assert.match(pageCss, /grid-template-columns:\s*1fr 1fr/);
  assert.match(focus, /styles\.focusGrid/);
  assert.match(sharedCss, /\.focusGrid\s*\{/);
  // responsive: grids collapse (no horizontal overflow)
  assert.match(pageCss, /max-width: 780px/);
  assert.match(sharedCss, /max-width: 620px/);
});
