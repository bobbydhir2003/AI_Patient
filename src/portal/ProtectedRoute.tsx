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
  if (role && user?.role !== role) {
    const home = user?.role === "admin" ? "/admin" : "/student/dashboard";
    return <Navigate to={home} replace />;
  }
  return <>{children}</>;
}
