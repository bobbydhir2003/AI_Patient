/**
 * Role-based post-authentication routing (pure + dependency-free so it can be
 * unit-tested with `node --test`).
 *
 * Final role flow:
 *  - Student                -> Patient Simulator dashboard.
 *  - Promoted admin/prof.   -> Patient Simulator dashboard (reaches the Admin
 *                              Dashboard via the "Admin Management" control).
 *  - Default/system admin   -> Admin Dashboard directly.
 *  - Super admin            -> Admin Dashboard directly (existing behavior).
 *
 * Permissions are still enforced by the backend (require_admin /
 * require_super_admin / require_simulator_access); this module only decides
 * where to send the browser.
 */
export type Role = "student" | "admin" | "super_admin";

/** Minimal shape needed to make routing decisions (a subset of AuthUser). */
export interface RoutingUser {
  role?: Role | null;
  isSystemAdmin?: boolean;
}

export function isAdminRole(role: Role | null | undefined): boolean {
  return role === "admin" || role === "super_admin";
}

/** Whether the account may reach the Admin Management area / Admin Dashboard. */
export function canAccessAdmin(role: Role | null | undefined): boolean {
  return isAdminRole(role);
}

/**
 * Does this account land DIRECTLY on the Admin Dashboard after signing in?
 * ONLY the seeded/default system admin does (isSystemAdmin === true) - regardless
 * of whether its role is admin or super_admin. Everyone else, including a user
 * PROMOTED to admin or super_admin, lands on the Patient Simulator and opts into
 * administration via the "Admin Management" control. This is a LANDING-PAGE
 * decision only; route/API permissions are unchanged (see canAccessAdmin and the
 * backend require_admin / require_super_admin guards).
 */
export function landsOnAdminDashboard(user: RoutingUser | null | undefined): boolean {
  if (!user) return false;
  return user.isSystemAdmin === true;
}

/** Where to send a user immediately after a successful sign in. */
export function postLoginPath(user: RoutingUser | null | undefined): string {
  return landsOnAdminDashboard(user) ? "/admin" : "/student/dashboard";
}

/** The "continue" call-to-action shown on the public landing page when a user
 * is already authenticated. */
export function homeCta(user: RoutingUser | null | undefined): { label: string; to: string } {
  return landsOnAdminDashboard(user)
    ? { label: "Continue to Admin Dashboard", to: "/admin" }
    : { label: "Continue to Student Dashboard", to: "/student/dashboard" };
}

/** Student-facing "case hub" destination. Any authenticated role (students plus
 * admins/professors running practice cases) uses the simulator dashboard as its
 * case hub; other visitors can still use the legacy catalog. */
export function caseHubPath(role: Role | null | undefined): string {
  return isAdminRole(role) || role === "student" ? "/student/dashboard" : "/cases";
}
