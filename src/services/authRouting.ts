/**
 * Role-based post-authentication routing (pure + dependency-free so it can be
 * unit-tested with `node --test`). Admins and super admins go to the admin
 * dashboard; students go to the student home. This keeps admins out of the
 * student interview-creation flow (require_student is also enforced backend).
 */
export type Role = "student" | "admin" | "super_admin";

export function isAdminRole(role: Role | null | undefined): boolean {
  return role === "admin" || role === "super_admin";
}

/** Where to send a user immediately after a successful sign in. */
export function postLoginPath(role: Role | null | undefined): string {
  return isAdminRole(role) ? "/admin" : "/student/dashboard";
}

/** The "continue" call-to-action shown on the public landing page when a user
 * is already authenticated. */
export function homeCta(role: Role | null | undefined): { label: string; to: string } {
  return isAdminRole(role)
    ? { label: "Continue to Admin Dashboard", to: "/admin" }
    : { label: "Continue to Student Dashboard", to: "/student/dashboard" };
}

/** Student-facing "case hub" destination. Authenticated students now start new
 * cases from the dashboard; other visitors can still use the legacy catalog. */
export function caseHubPath(role: Role | null | undefined): string {
  return role === "student" ? "/student/dashboard" : "/cases";
}
