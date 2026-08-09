import { Navigate } from "react-router-dom";

/**
 * DEPRECATED / removed screen. The separate "Administrator Sign In" page has
 * been removed — there is ONE login entry point for the whole app (the main
 * portal at "/"). This component is retained only as a safe redirect for any
 * stale import/bookmark and is no longer routed (see App.tsx: /login → "/").
 */
export function LoginPage() {
  return <Navigate to="/" replace />;
}
