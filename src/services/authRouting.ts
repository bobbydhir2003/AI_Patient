/**
 * Role-based post-authentication routing (pure + dependency-free so it can be
 * unit-tested with `node --test`).
 *
 * Role model — THREE roles; the main login lands every role on Patient Cases:
 *  - Student     -> Patient Cases dashboard (/student/dashboard).
 *  - Admin       -> Patient Cases dashboard; reaches Admin Management (academic
 *                   administration) from there.
 *  - Super Admin -> same as Admin, plus the Super Admin area (/superadmin/*),
 *                   which also has its own portal login (/superadmin/login).
 *
 * Permissions are enforced by the backend (require_admin / require_super_admin);
 * this module only decides where to send the browser.
 */
export type Role = "student" | "admin" | "super_admin";

/** Minimal shape needed to make routing decisions (a subset of AuthUser). */
export interface RoutingUser {
  role?: Role | null;
}

/** Case-hub / Patient Cases destination shared by both roles. */
export const PATIENT_CASES_PATH = "/student/dashboard";

/** THE main login entry point + logout / unauthenticated / session-expired
 * destination for the whole app. */
export const LOGIN_ROUTE = "/";

/** Super Admin portal sign-in and landing page. The URL grants nothing: every
 * /superadmin API call is re-checked server-side (require_super_admin). */
export const SUPERADMIN_LOGIN_ROUTE = "/superadmin/login";
export const SUPERADMIN_HOME = "/superadmin/dashboard";

/** Admin OR super admin: may reach academic Admin Management. */
export function isAdminRole(role: Role | null | undefined): boolean {
  return role === "admin" || role === "super_admin";
}

/** Super admin only: may reach system administration (/superadmin/*). */
export function isSuperAdminRole(role: Role | null | undefined): boolean {
  return role === "super_admin";
}

/** Whether the account may reach the Admin Management area / System Dashboard. */
export function canAccessAdmin(role: Role | null | undefined): boolean {
  return isAdminRole(role);
}

/**
 * Where to send a user immediately after a successful sign in. Every
 * authenticated account — student OR admin — lands on the Patient Cases
 * dashboard. Admins opt into administration from there; they are never bounced
 * straight into an admin-only page.
 */
export function postLoginPath(_user: RoutingUser | null | undefined): string {
  return PATIENT_CASES_PATH;
}

/** The "continue" call-to-action shown on the public landing page when a user
 * is already authenticated. Both roles continue to Patient Cases. */
export function homeCta(_user: RoutingUser | null | undefined): { label: string; to: string } {
  return { label: "Continue to Patient Cases", to: PATIENT_CASES_PATH };
}

/** Student-facing "case hub" destination. Any authenticated role (students plus
 * admins running practice cases) uses the Patient Cases dashboard as its case
 * hub; other visitors can still use the legacy catalog. */
export function caseHubPath(role: Role | null | undefined): string {
  return isAdminRole(role) || role === "student" ? PATIENT_CASES_PATH : "/cases";
}
