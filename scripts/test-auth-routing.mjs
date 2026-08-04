/**
 * Tests for role-based post-auth routing (Priority E).
 * Run with: npm run test:routing
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  postLoginPath,
  homeCta,
  isAdminRole,
  caseHubPath,
  landsOnAdminDashboard,
  canAccessAdmin,
} from "../.test-build/services/authRouting.js";

// Default landing page depends ONLY on isSystemAdmin, never on role alone.
test("TEST 1 - student -> student dashboard", () => {
  assert.equal(postLoginPath({ role: "student", isSystemAdmin: false }), "/student/dashboard");
  assert.equal(landsOnAdminDashboard({ role: "student", isSystemAdmin: false }), false);
});

test("TEST 2 - promoted admin (isSystemAdmin=false) -> student dashboard", () => {
  assert.equal(postLoginPath({ role: "admin", isSystemAdmin: false }), "/student/dashboard");
  assert.equal(landsOnAdminDashboard({ role: "admin", isSystemAdmin: false }), false);
});

test("TEST 3 - promoted SUPER admin (isSystemAdmin=false) -> student dashboard", () => {
  // The core bug: a promoted super_admin must NOT be sent straight to /admin.
  assert.equal(postLoginPath({ role: "super_admin", isSystemAdmin: false }), "/student/dashboard");
  assert.equal(landsOnAdminDashboard({ role: "super_admin", isSystemAdmin: false }), false);
});

test("TEST 4 - system admin (isSystemAdmin=true) -> admin dashboard", () => {
  assert.equal(postLoginPath({ role: "admin", isSystemAdmin: true }), "/admin");
  assert.equal(landsOnAdminDashboard({ role: "admin", isSystemAdmin: true }), true);
});

test("TEST 5 - system SUPER admin (isSystemAdmin=true) -> admin dashboard", () => {
  assert.equal(postLoginPath({ role: "super_admin", isSystemAdmin: true }), "/admin");
  assert.equal(landsOnAdminDashboard({ role: "super_admin", isSystemAdmin: true }), true);
});

test("landing != permission: all admin roles can still access the admin area", () => {
  assert.equal(canAccessAdmin("admin"), true);
  assert.equal(canAccessAdmin("super_admin"), true);
  assert.equal(canAccessAdmin("student"), false);
  assert.equal(isAdminRole("super_admin"), true);
});

test("home CTA follows the same isSystemAdmin landing rule", () => {
  assert.equal(homeCta({ role: "admin", isSystemAdmin: true }).to, "/admin");
  assert.equal(homeCta({ role: "super_admin", isSystemAdmin: true }).to, "/admin");
  assert.equal(homeCta({ role: "super_admin", isSystemAdmin: false }).to, "/student/dashboard");
  assert.equal(homeCta({ role: "admin", isSystemAdmin: false }).to, "/student/dashboard");
  assert.equal(homeCta({ role: "student", isSystemAdmin: false }).to, "/student/dashboard");
});

test("case hub is the dashboard for any authenticated role; legacy visitors keep /cases", () => {
  assert.equal(caseHubPath("student"), "/student/dashboard");
  assert.equal(caseHubPath("admin"), "/student/dashboard");
  assert.equal(caseHubPath("super_admin"), "/student/dashboard");
  assert.equal(caseHubPath(null), "/cases");
});
