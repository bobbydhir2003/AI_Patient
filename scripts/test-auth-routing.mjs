/**
 * Tests for role-based post-auth routing (Priority E).
 * Run with: npm run test:routing
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { postLoginPath, homeCta, isAdminRole, caseHubPath } from "../.test-build/services/authRouting.js";

test("student signs in -> student dashboard", () => {
  assert.equal(postLoginPath("student"), "/student/dashboard");
});

test("admin signs in -> admin dashboard", () => {
  assert.equal(postLoginPath("admin"), "/admin");
});

test("super_admin signs in -> admin dashboard (not student)", () => {
  assert.equal(postLoginPath("super_admin"), "/admin");
  assert.equal(isAdminRole("super_admin"), true);
});

test("home CTA points admins to admin dashboard and students to student home", () => {
  assert.equal(homeCta("admin").to, "/admin");
  assert.equal(homeCta("super_admin").to, "/admin");
  assert.equal(homeCta("student").to, "/student/dashboard");
});

test("student case hub is now the dashboard while legacy visitors keep /cases", () => {
  assert.equal(caseHubPath("student"), "/student/dashboard");
  assert.equal(caseHubPath("admin"), "/cases");
  assert.equal(caseHubPath(null), "/cases");
});
