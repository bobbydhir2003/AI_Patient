/**
 * Source-level guarantees for the Super Admin tier (routes, guard, sidebar,
 * login portal, and the admin UI never offering the super_admin role).
 * Run with: npm run test:superadmin
 *
 * No jsdom in this project, so (like the other admin source tests) we assert
 * the behaviour-critical wiring from source. Authorization itself is enforced
 * and tested on the backend (tests/test_super_admin.py).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, relative } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

const app = read("src/App.tsx");
const guard = read("src/portal/ProtectedRoute.tsx");
const sidebar = read("src/components/admin/AdminSidebar.tsx");
const topbar = read("src/components/admin/AdminTopbar.tsx");
const login = read("src/pages/auth/SuperAdminLoginPage.tsx");
const auth = read("src/state/AuthContext.tsx");
const routing = read("src/services/authRouting.ts");
const authApi = read("src/services/authApi.ts");
const users = read("src/pages/admin/AdminUsersPage.tsx");
const usersApi = read("src/services/usersApi.ts");
const studentData = read("src/pages/admin/StudentDataDetail.tsx");

const block = (path) => {
  const start = app.indexOf(`path="${path}"`);
  return app.slice(start, app.indexOf("</Route>", start));
};

test("/superadmin/login exists and is outside the guarded subtree", () => {
  assert.match(app, /<Route path="\/superadmin\/login" element=\{<SuperAdminLoginPage \/>\} \/>/);
  assert.doesNotMatch(block("/superadmin"), /SuperAdminLoginPage/);
});

test("/superadmin/* is guarded by role super_admin and serves every privileged page", () => {
  const sa = block("/superadmin");
  assert.match(sa, /<ProtectedRoute role="super_admin">\s*<AdminLayout \/>/);
  for (const [path, page] of [
    ["dashboard", "SystemDashboardPage"],
    ["survey-resets", "AdminSurveyResetsPage"],
    ["traffic", "TrafficDashboardPage"],
    ["load-capacity", "LoadCapacityTestingPage"],
    ["ai-usage", "AiUsageCostPage"],
    ["ai-config", "AiConfigurationPage"],
    ["credentials", "ApiCredentialsPage"],
    ["health", "SystemHealthPage"],
  ]) {
    assert.match(sa, new RegExp(`<Route path="${path}" element=\\{<${page} \\/>\\} \\/>`), path);
  }
});

test("old /admin/system/* URLs only redirect into the guarded /superadmin routes", () => {
  const admin = block("/admin");
  assert.match(admin, /<ProtectedRoute role="admin">/);
  for (const page of ["SystemDashboardPage", "TrafficDashboardPage", "LoadCapacityTestingPage", "AiUsageCostPage", "AdminSurveyResetsPage", "ApiCredentialsPage", "AiConfigurationPage"]) {
    assert.doesNotMatch(admin, new RegExp(`<${page} `), `${page} must not render under /admin`);
  }
  assert.match(admin, /<Route path="system" element=\{<Navigate to="\/superadmin\/dashboard" replace \/>\} \/>/);
  assert.match(admin, /<Route path="system\/load-testing" element=\{<Navigate to="\/superadmin\/load-capacity" replace \/>\} \/>/);
});

test("guard: admin allows admin+super_admin; super_admin is exact; denial never grants access", () => {
  assert.match(guard, /if \(required === "admin"\) return isAdminRole\(actual\);/);
  assert.match(guard, /if \(required === "super_admin"\) return isSuperAdminRole\(actual\);/);
  assert.match(guard, /role === "super_admin" \? SUPERADMIN_LOGIN_ROUTE : LOGIN_ROUTE/);
  assert.match(guard, /Super Admin access required\./);
  assert.match(routing, /return role === "admin" \|\| role === "super_admin";/);
  assert.match(routing, /export function isSuperAdminRole[^{]*\{\s*return role === "super_admin";/);
  // Role comes from the backend user object only; never from storage/URL.
  assert.match(auth, /isSuperAdmin: isSuperAdminRole\(user\?\.role\)/);
  assert.doesNotMatch(auth, /localStorage\.(get|set)Item\([^)]*role/i);
});

test("sidebar: normal admins see Academic Management only; super admins also see Super Administration", () => {
  const academic = sidebar.slice(sidebar.indexOf('title: "Academic Management"'), sidebar.indexOf('title: "Super Administration"'));
  for (const label of ["Dashboard", "Student Data", "Students", "Sessions", "Transcripts", "Assessments", "User Accounts"]) {
    assert.ok(academic.includes(`label: "${label}"`), label);
  }
  for (const label of ["Survey Resets", "System Dashboard", "Traffic Dashboard", "Load & Capacity Testing", "AI Usage & Cost"]) {
    assert.ok(!academic.includes(`label: "${label}"`), `${label} must not be an academic item`);
  }
  const superSection = sidebar.slice(sidebar.indexOf('title: "Super Administration"'));
  assert.match(superSection, /^title: "Super Administration",\n\s*superOnly: true,/);
  for (const [to, label] of [
    ["/superadmin/survey-resets", "Survey Resets"],
    ["/superadmin/dashboard", "System Dashboard"],
    ["/superadmin/traffic", "Traffic Dashboard"],
    ["/superadmin/load-capacity", "Load & Capacity Testing"],
    ["/superadmin/ai-usage", "AI Usage & Cost"],
  ]) {
    assert.ok(superSection.includes(`{ to: "${to}", label: "${label}"`), label);
  }
  assert.match(sidebar, /const sections = SECTIONS\.filter\(\(s\) => !s\.superOnly \|\| isSuperAdmin\);/);
  assert.doesNotMatch(sidebar, /System Administration/);
});

test("topbar: system links are super-admin only; super admins are labelled", () => {
  assert.match(topbar, /const systemItems = SYSTEM_ITEMS\.filter\(\(i\) => !i\.superOnly \|\| isSuperAdmin\);/);
  for (const to of ["/superadmin/dashboard", "/superadmin/ai-config", "/superadmin/credentials", "/superadmin/health"]) {
    assert.match(topbar, new RegExp(`\\{ to: "${to.replace(/\//g, "\\/")}",[^}]*superOnly: true \\}`), to);
  }
  assert.doesNotMatch(topbar, /to: "\/admin\/system/);
  assert.match(topbar, /isSuperAdmin \? "Super Administrator" : user\?\.email/);
});

test("login portal: dedicated endpoint, required copy, no embedded credentials", () => {
  assert.match(login, /Super Admin Portal/);
  assert.match(login, /Restricted system administration access\./);
  assert.match(login, /htmlFor="sa-email">Email</);
  assert.match(login, /htmlFor="sa-password">Password</);
  assert.match(login, /"Sign In"/);
  assert.match(login, /await loginSuperAdmin\(email\.trim\(\), password\)/);
  // Every failure shows ONE generic message (only 429 keeps its own); the page
  // never tells the browser the credentials were valid for another role.
  assert.match(login, /const GENERIC_FAILURE = "Incorrect email or password\.";/);
  assert.match(login, /setError\(err instanceof ApiError && err\.status === 429 \? err\.message : GENERIC_FAILURE\);/);
  assert.doesNotMatch(login, /Super Admin access required/);
  assert.doesNotMatch(auth, /throw new Error\("Super Admin access required\."\)/);
  assert.match(authApi, /"\/auth\/superadmin\/login"/);
  assert.doesNotMatch(login, /useState\("[^"]+@[^"]+"\)|password\s*[:=]\s*"[^"]+"/i);
});

test("admin UI never offers the super_admin role and protects super_admin rows", () => {
  assert.match(usersApi, /export type AssignableRole = "student" \| "admin";/);
  assert.match(usersApi, /changeUserRole = \(t: string \| null, id: string, role: AssignableRole\)/);
  assert.doesNotMatch(users, /role: "super_admin" \}\)/); // no confirm that assigns it
  assert.doesNotMatch(users, /Make Super Admin/i);
  assert.match(users, /const isProtected = \(u: AdminUser\) => u\.role === "super_admin" && !isSuperAdmin;/);
  assert.match(users, /if \(isProtected\(u\)\) return <span className="pt-muted"[^>]*>Protected<\/span>;/);
  assert.equal(users.match(/disabled=\{isProtected\(u\)\}/g).length, 2); // table + card checkboxes
});

test("Student Data: reset controls render only for super admins", () => {
  assert.match(studentData, /const \{ token, isSuperAdmin \} = useAuth\(\);/);
  assert.match(studentData, /showReset=\{isSuperAdmin\}/);
  assert.match(studentData, /\{isSuperAdmin \? \(/);
  assert.match(studentData, /\{isSuperAdmin && confirm && \(/);
  assert.match(studentData, /Survey resets are managed by a Super Administrator\./);
});

test("no hard-coded super admin credentials anywhere in the frontend", () => {
  const offenders = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (/\.(tsx?|jsx?)$/.test(name)) {
        const src = readFileSync(full, "utf8");
        if (/SUPER_ADMIN_PASSWORD|superpass|super_admin_password/i.test(src)) offenders.push(relative(root, full));
      }
    }
  };
  walk(join(root, "src"));
  assert.deepEqual(offenders, []);
});
