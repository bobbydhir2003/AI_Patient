/**
 * Source-level guards for the Priority F entry screens.
 * Run with: npm run test:landing
 * (Reads the source files directly - no build/render harness needed.)
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(root, p), "utf8");

const welcome = read("src/pages/WelcomePage.tsx");
const welcomeCss = read("src/pages/WelcomePage.module.css");
const app = read("src/App.tsx");
const protectedRoute = read("src/portal/ProtectedRoute.tsx");
const authRouting = read("src/services/authRouting.ts");

// ------------------------------ STUDENT "/" ------------------------------
test("student home uses the local Home.webp background", () => {
  assert.match(welcomeCss, /url\(["']\/home\/Home\.webp["']\)/);
});

test('student home has a real login form that calls the auth API (not /login nav)', () => {
  assert.match(welcome, /await login\(/, "must call the real login() auth API");
  assert.match(welcome, /Welcome back/);
  assert.match(welcome, /student-password/);
  assert.match(welcome, /Sign In/);
  // Student Sign In must NOT navigate to the admin /login route.
  assert.ok(!/navigate\(\s*["']\/login["']/.test(welcome), "student Sign In must not go to /login");
});

test("student home no longer uses free-text Student Name / Student ID identity", () => {
  assert.ok(!/Student Name/i.test(welcome));
  assert.ok(!/setStudentName|setStudentId|useAppContext/.test(welcome));
});

test('student home "Create an account" routes to /register', () => {
  assert.match(welcome, /to="\/register"/);
});

// -------------------- SINGLE LOGIN ENTRY POINT (no admin /login) --------------------
test("there is ONE canonical login route and it is the main portal '/'", () => {
  assert.match(authRouting, /LOGIN_ROUTE\s*=\s*["']\/["']/);
});

test("the old /login admin screen is removed from the flow (redirects to '/')", () => {
  // /login must not render the admin sign-in page; it redirects to the main login.
  assert.match(app, /path="\/login"\s+element=\{<Navigate to="\/" replace \/>\}/);
  assert.ok(!/<LoginPage/.test(app), "App must not render the admin LoginPage");
});

test("unauthenticated guard sends users to the main login (LOGIN_ROUTE), not /login", () => {
  assert.match(protectedRoute, /LOGIN_ROUTE/);
  assert.ok(!/to="\/login"/.test(protectedRoute), "guard must not redirect to /login");
});

// ------------------------------ GLOBAL ------------------------------
test("no Request Access anywhere in the primary entry UI", () => {
  for (const [name, src] of [["welcome", welcome], ["app", app]]) {
    assert.ok(!/request-access/i.test(src), `${name} must not reference request-access`);
    assert.ok(!/Request access/i.test(src), `${name} must not show a Request access link`);
  }
});
