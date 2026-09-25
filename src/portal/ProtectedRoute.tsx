import type { ReactNode } from "react";
import { Link, Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../state/AuthContext";
import {
  isAdminRole,
  isSuperAdminRole,
  LOGIN_ROUTE,
  SUPERADMIN_LOGIN_ROUTE,
  type Role,
} from "../services/authRouting";
import { Spinner } from "./ui";

function satisfies(required: Role | undefined, actual: Role | null | undefined): boolean {
  if (!required) return true;
  if (required === "admin") return isAdminRole(actual); // admin OR super_admin
  if (required === "super_admin") return isSuperAdminRole(actual);
  return actual === required;
}

/** Guards a subtree by authentication and (optionally) role. The role comes
 * from the user object the backend returned (/auth/me), never from the URL or
 * browser storage; and every protected API re-checks it server-side, so this
 * guard is UX only, not the security boundary.
 *
 * - Unauthenticated -> the main login ("/"), or the Super Admin portal login for
 *   a super_admin-only subtree.
 * - Authenticated but lacking an admin role -> the Patient Cases dashboard.
 * - An admin/student on a super_admin-only subtree -> an access-denied panel. */
export function ProtectedRoute({
  role,
  children,
}: {
  role?: Role;
  children: ReactNode;
}) {
  const { loading, isAuthenticated, user } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="pt-portal">
        <Spinner label="Checking your session…" />
      </div>
    );
  }
  if (!isAuthenticated) {
    const to = role === "super_admin" ? SUPERADMIN_LOGIN_ROUTE : LOGIN_ROUTE;
    return <Navigate to={to} replace state={{ from: location.pathname }} />;
  }
  if (!satisfies(role, user?.role)) {
    if (role === "super_admin") return <SuperAdminRequired isAdmin={isAdminRole(user?.role)} />;
    return <Navigate to="/student/dashboard" replace />;
  }
  return <>{children}</>;
}

function SuperAdminRequired({ isAdmin }: { isAdmin: boolean }) {
  return (
    <div className="pt-portal">
      <div className="pt-card" role="alert" style={{ maxWidth: 480, margin: "15vh auto", textAlign: "center" }}>
        <h1 className="pt-h1" style={{ fontSize: "1.4rem" }}>Super Admin access required.</h1>
        <p className="pt-muted">This area is restricted to Super Administrators.</p>
        <Link className="pt-btn pt-btn-secondary" to={isAdmin ? "/admin" : "/student/dashboard"}>
          {isAdmin ? "Back to Admin Dashboard" : "Back to Patient Cases"}
        </Link>
      </div>
    </div>
  );
}
