/**
 * Tests for role-based post-auth routing after the two-role consolidation.
 * Run with: npm run test:routing
 *
 * Final model: exactly two roles (student, admin) and BOTH land on the Patient
 * Cases dashboard. Admins are never force-redirected into an admin-only page;
 * they open Admin Management / System Dashboard from controls on Patient Cases.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  postLoginPath,
  homeCta,
  isAdminRole,
  caseHubPath,
  canAccessAdmin,
  isSuperAdminRole,
  PATIENT_CASES_PATH,
  SUPERADMIN_LOGIN_ROUTE,
  SUPERADMIN_HOME,
} from "../.test-build/services/authRouting.js";

test("TEST 1 - student lands on Patient Cases", () => {
  assert.equal(postLoginPath({ role: "student" }), PATIENT_CASES_PATH);
  assert.equal(postLoginPath({ role: "student" }), "/student/dashboard");
});

test("TEST 2 - admin ALSO lands on Patient Cases (not forced into /admin)", () => {
  assert.equal(postLoginPath({ role: "admin" }), "/student/dashboard");
});

test("TEST 3 - permissions: only admin can reach the admin area", () => {
  assert.equal(canAccessAdmin("admin"), true);
  assert.equal(canAccessAdmin("student"), false);
  assert.equal(isAdminRole("admin"), true);
  assert.equal(isAdminRole("student"), false);
});

test("TEST 4 - home CTA points both roles to Patient Cases", () => {
  assert.equal(homeCta({ role: "admin" }).to, "/student/dashboard");
  assert.equal(homeCta({ role: "student" }).to, "/student/dashboard");
});

test("TEST 5 - case hub is the dashboard for any authenticated role", () => {
  assert.equal(caseHubPath("student"), "/student/dashboard");
  assert.equal(caseHubPath("admin"), "/student/dashboard");
  assert.equal(caseHubPath(null), "/cases");
});

test("TEST 6 - super_admin is an admin AND the only super admin", () => {
  assert.equal(isAdminRole("super_admin"), true);
  assert.equal(canAccessAdmin("super_admin"), true);
  assert.equal(isSuperAdminRole("super_admin"), true);
  assert.equal(isSuperAdminRole("admin"), false);
  assert.equal(isSuperAdminRole("student"), false);
  assert.equal(isSuperAdminRole(null), false);
  assert.equal(caseHubPath("super_admin"), "/student/dashboard");
  assert.equal(postLoginPath({ role: "super_admin" }), "/student/dashboard");
  assert.equal(SUPERADMIN_LOGIN_ROUTE, "/superadmin/login");
  assert.equal(SUPERADMIN_HOME, "/superadmin/dashboard");
});
