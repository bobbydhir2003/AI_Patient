/**
 * Role-based post-authentication routing (pure + dependency-free so it can be
 * unit-tested with `node --test`).
 *
 * Final role model — exactly TWO roles, and BOTH land on Patient Cases:
 *  - Student -> Patient Cases dashboard (/student/dashboard).
 *  - Admin   -> Patient Cases dashboard (/student/dashboard). Admins are NOT
 *              force-redirected into the admin area; they reach Admin Management
 *              and the System Dashboard from the controls shown on the Patient
 *              Cases dashboard (see StudentDashboardPage + AdminSidebar).
 *
 * Permissions are still enforced by the backend (require_admin); this module
 * only decides where to send the browser after sign in.
 */
export type Role = "student" | "admin";

/** Minimal shape needed to make routing decisions (a subset of AuthUser). */
export interface RoutingUser {
  role?: Role | null;
}

/** Case-hub / Patient Cases destination shared by both roles. */
export const PATIENT_CASES_PATH = "/student/dashboard";

/** THE single login entry point + logout / unauthenticated / session-expired
 * destination for the whole app. There is no separate admin login screen. */
export const LOGIN_ROUTE = "/";

export function isAdminRole(role: Role | null | undefined): boolean {
  return role === "admin";
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
