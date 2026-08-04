import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { ApiError } from "../../services/api";
import { isAdminRole, postLoginPath } from "../../services/authRouting";
import styles from "./LoginPage.module.css";

const STUDENT_ON_ADMIN = "This login is for administrator accounts only.";

/**
 * ADMIN-ONLY sign in (route "/login"). Students sign in on "/". This page uses
 * the same backend auth API; the difference is intent + role validation. Backend
 * RBAC (require_admin/require_super_admin) is the real protection - this only
 * routes correctly and refuses to send a student into the admin area.
 */
export function LoginPage() {
  const { login, user } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Already-authenticated visits: admins are routed to their landing page (the
  // system/super admin to /admin, a promoted admin to the Patient Simulator);
  // a signed-in student sees the admin-only notice.
  useEffect(() => {
    if (user && isAdminRole(user.role)) {
      navigate(postLoginPath(user), { replace: true });
    } else if (user) {
      setError("This portal is for administrators only.");
    }
  }, [user, navigate]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const signedIn = await login(email.trim(), password);
      if (isAdminRole(signedIn.role)) {
        // System/super admin -> /admin; promoted admin -> Patient Simulator
        // (they open the Admin Dashboard from the Admin Management control).
        navigate(postLoginPath(signedIn), { replace: true });
      } else {
        // Valid student credentials, but this is the admin portal.
        setError(STUDENT_ON_ADMIN);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign in failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.adminShell}>
      <div className={styles.adminOverlay} aria-hidden="true" />
      <form className={styles.adminCard} onSubmit={onSubmit}>
        <div className={styles.adminBadge} aria-hidden="true">
          <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6l7-3Z" />
            <path d="M9.5 12.5l1.8 1.8 3.2-3.6" />
          </svg>
        </div>
        <h1 className={styles.adminTitle}>Administrator Sign In</h1>
        <p className={styles.adminSub}>Access the PT AI Patient Simulator administration system.</p>

        <div className="pt-field">
          <label htmlFor="admin-email">Email</label>
          <input id="admin-email" type="email" className="pt-input" value={email}
            onChange={(e) => setEmail(e.target.value)} placeholder="you@unmc.edu"
            autoComplete="email" required />
        </div>
        <div className="pt-field">
          <label htmlFor="admin-password">Password</label>
          <input id="admin-password" type="password" className="pt-input" value={password}
            onChange={(e) => setPassword(e.target.value)} placeholder="Enter your password"
            autoComplete="current-password" required />
        </div>

        {error && <div className="pt-error-text" role="alert">{error}</div>}

        <button className="pt-btn pt-btn-block" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <div className={styles.adminBack}>
          <Link className="pt-link" to="/">← Back to Student Portal</Link>
        </div>
      </form>
      <p className={styles.secureNote}>Secure access · Restricted to authorized administrators</p>
    </div>
  );
}
