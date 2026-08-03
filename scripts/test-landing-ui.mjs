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
const login = read("src/pages/auth/LoginPage.tsx");
const loginCss = read("src/pages/auth/LoginPage.module.css");
const app = read("src/App.tsx");

// ------------------------------ STUDENT "/" ------------------------------
test("student home uses the local Home.webp background", () => {
  assert.match(welcomeCss, /url\(["']\/home\/Home\.webp["']\)/);
});

test('student home has a real login form that calls the auth API (not /login nav)', () => {
  assert.match(welcome, /await login\(/, "must call the real login() auth API");
  assert.match(welcome, /Welcome back/);
  assert.match(welcome, /type="password"/);
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

// ------------------------------ ADMIN "/login" ------------------------------
test("admin login uses the local admin.webp background", () => {
  assert.match(loginCss, /url\(["']\/home\/admin\.webp["']\)/);
});

test("admin login shows Administrator Sign In + Back to Student Portal", () => {
  assert.match(login, /Administrator Sign In/);
  assert.match(login, /Back to Student Portal/);
  assert.match(login, /to="\/"/);
});

test("admin login routes admins to /admin and rejects students", () => {
  assert.match(login, /navigate\(\s*["']\/admin["']/);
  assert.match(login, /isAdminRole/);
  assert.match(login, /administrator accounts only/i);
});

test("admin login has NO student account creation and NO request access", () => {
  assert.ok(!/Create an account/i.test(login), "admin login must not offer registration");
  assert.ok(!/\/register/.test(login), "admin login must not link to /register");
  assert.ok(!/request-access/i.test(login) && !/Request access/i.test(login));
});

// ------------------------------ GLOBAL ------------------------------
test("no Request Access anywhere in the primary entry UI", () => {
  for (const [name, src] of [["welcome", welcome], ["login", login], ["app", app]]) {
    assert.ok(!/request-access/i.test(src), `${name} must not reference request-access`);
    assert.ok(!/Request access/i.test(src), `${name} must not show a Request access link`);
  }
});
