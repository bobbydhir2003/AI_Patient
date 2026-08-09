import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../state/AuthContext";
import { Spinner } from "./ui";

/** Guards a subtree by authentication and (optionally) role. Unauthenticated
 * users are sent to /login; authenticated users lacking the required role are
 * routed to their own home so they never see another role's pages. */
export function ProtectedRoute({
  role,
  children,
}: {
  role?: "student" | "admin";
  children: ReactNode;
}) {
  const { loading, isAuthenticated, user } = useAuth();
  const location = useLocation();

  // Two roles only. A gate is satisfied when no role is required or the user's
  // role matches exactly.
  const roleSatisfied = !role || user?.role === role;

  if (loading) {
    return (
      <div className="pt-portal">
        <Spinner label="Checking your session…" />
      </div>
    );
  }
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  if (!roleSatisfied) {
    // Both roles' home is the Patient Cases dashboard; a student who tries to
    // reach an admin route is sent there (never to an admin-only page).
    return <Navigate to="/student/dashboard" replace />;
  }
  return <>{children}</>;
}
