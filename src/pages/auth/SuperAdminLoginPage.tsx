import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { ApiError } from "../../services/api";
import { SUPERADMIN_HOME } from "../../services/authRouting";
import styles from "./SuperAdminLoginPage.module.css";

// Same wording as the backend's generic credential error: the portal never
// reveals whether the credentials were valid for another role or account state.
const GENERIC_FAILURE = "Incorrect email or password.";

/**
 * Super Admin portal sign-in (/superadmin/login).
 *
 * Uses the existing auth backend via POST /api/auth/superadmin/login, which runs
 * the normal credential check and issues a token ONLY to an active super_admin.
 * Every other outcome (unknown email, wrong password, student, normal admin,
 * disabled account) gets the SAME generic 401 and no token, so this page can only
 * ever show one failure message (or the generic rate-limit message). No
 * credentials or role data live in this file.
 */
export function SuperAdminLoginPage() {
  const { loginSuperAdmin, isSuperAdmin, loading } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!loading && isSuperAdmin) return <Navigate to={SUPERADMIN_HOME} replace />;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await loginSuperAdmin(email.trim(), password);
      navigate(SUPERADMIN_HOME, { replace: true });
    } catch (err) {
      // 429 (throttled) keeps its own generic message; everything else is uniform.
      setError(err instanceof ApiError && err.status === 429 ? err.message : GENERIC_FAILURE);
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`pt-auth-shell ${styles.shell}`}>
      <div className={`pt-card pt-auth-card ${styles.card}`}>
        <div className={styles.brand}>
          <img className={styles.logo} src="/branding/unmc-logo.png" alt="UNMC" />
          <span className={styles.brandText}>PT AI Patient Simulator</span>
        </div>
        <div className={styles.badge}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
          Restricted
        </div>
        <h1 className="pt-h1">Super Admin Portal</h1>
        <p className="pt-sub">Restricted system administration access.</p>

        <form onSubmit={onSubmit} noValidate>
          <div className="pt-field">
            <label htmlFor="sa-email">Email</label>
            <input
              id="sa-email"
              className="pt-input"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              disabled={busy}
            />
          </div>
          <div className="pt-field">
            <label htmlFor="sa-password">Password</label>
            <input
              id="sa-password"
              className="pt-input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              disabled={busy}
            />
          </div>
          {error && <p className="pt-error-text" role="alert">{error}</p>}
          <button className="pt-btn pt-btn-block" type="submit" disabled={busy || !email.trim() || !password}>
            {busy ? "Signing in…" : "Sign In"}
          </button>
        </form>
        <p className={styles.foot}>
          Not a Super Admin? <Link to="/">Go to the main sign in</Link>
        </p>
      </div>
    </div>
  );
}
