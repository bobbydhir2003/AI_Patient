import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../state/AuthContext";
import { ApiError } from "../services/api";
import { homeCta, postLoginPath } from "../services/authRouting";
import styles from "./WelcomePage.module.css";

const FEATURES = [
  {
    title: "Realistic Patient Encounters",
    text: "Engage in lifelike interviews across a variety of conditions.",
    icon: (
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 5h11a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H9l-4 3v-3H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z" />
      </svg>
    ),
  },
  {
    title: "AI-Supported Assessment",
    text: "Get objective, actionable feedback aligned with PT competencies.",
    icon: (
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M5 20V10M12 20V4M19 20v-7" />
      </svg>
    ),
  },
  {
    title: "Learn. Reflect. Improve.",
    text: "Build skills and confidence with every conversation.",
    icon: (
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 3l8 4-8 4-8-4 8-4Z" /><path d="M6 10v4c0 1.7 2.7 3 6 3s6-1.3 6-3v-4" />
      </svg>
    ),
  },
];

export function WelcomePage() {
  const navigate = useNavigate();
  const { user, login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      // Real student authentication happens HERE on "/" (not via /login).
      const signedIn = await login(email.trim(), password);
      // ACTIVE student -> student home; a promoted admin/professor also lands on
      // the Patient Simulator; only the system/super admin goes straight to /admin.
      navigate(postLoginPath(signedIn), { replace: true });
    } catch (err) {
      // Account-status (403) and invalid-credential (401) messages come straight
      // from the backend; a true connection failure is a distinct message.
      setError(err instanceof ApiError ? err.message : "Sign in failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.homePage}>
      <main className={styles.hero}>
        <div className={styles.overlay} aria-hidden="true" />

        <div className={styles.heroInner}>
          <section className={styles.content}>
            <span className={styles.eyebrow}>UNMC · iEXCEL</span>
            <h1 className={styles.heading}>
              PT AI Patient
              <br />
              Simulator
            </h1>
            <div className={styles.accentLine} aria-hidden="true" />
            <p className={styles.subtitle}>
              Practice realistic patient interviews and build clinical confidence with
              AI-supported feedback aligned with PT competencies.
            </p>

            <div className={styles.features}>
              {FEATURES.map((f) => (
                <div key={f.title} className={styles.feature}>
                  <span className={styles.featureIcon}>{f.icon}</span>
                  <div>
                    <p className={styles.featureTitle}>{f.title}</p>
                    <p className={styles.featureText}>{f.text}</p>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <aside className={styles.loginPanel}>
            <div className={styles.loginCard}>
              <span className={`${styles.corner} ${styles.cornerTopLeft}`} aria-hidden="true" />
              <span className={`${styles.corner} ${styles.cornerTopRight}`} aria-hidden="true" />
              <span className={`${styles.corner} ${styles.cornerBottomLeft}`} aria-hidden="true" />
              <span className={`${styles.corner} ${styles.cornerBottomRight}`} aria-hidden="true" />
              <span className={`${styles.edgeGlow} ${styles.edgeGlowTop}`} aria-hidden="true" />
              <span className={`${styles.edgeGlow} ${styles.edgeGlowBottom}`} aria-hidden="true" />
              {user ? (
                // Already authenticated: continue to the right home (no student
                // interview flow for admins).
                <>
                  <div className={styles.loginCap} aria-hidden="true">
                    <svg viewBox="0 0 24 24" width="30" height="30" fill="currentColor">
                      <path d="M12 3 1 8l11 5 9-4.1V15h2V8L12 3Z" />
                      <path d="M5 12.5V16c0 1.9 3.1 3.5 7 3.5s7-1.6 7-3.5v-3.5l-7 3.2-7-3.2Z" />
                    </svg>
                  </div>
                  <h2 className={styles.loginTitle}>Welcome back!</h2>
                  <p className={styles.loginSub}>You are already signed in.</p>
                  <button
                    type="button"
                    className={styles.signInButton}
                    onClick={() => navigate(homeCta(user).to)}
                  >
                    {homeCta(user).label}
                    <span className={styles.buttonArrow} aria-hidden="true">›</span>
                  </button>
                </>
              ) : (
                <form className={styles.loginForm} onSubmit={onSubmit}>
                  <div className={styles.loginCap} aria-hidden="true">
                    <svg viewBox="0 0 24 24" width="30" height="30" fill="currentColor">
                      <path d="M12 3 1 8l11 5 9-4.1V15h2V8L12 3Z" />
                      <path d="M5 12.5V16c0 1.9 3.1 3.5 7 3.5s7-1.6 7-3.5v-3.5l-7 3.2-7-3.2Z" />
                    </svg>
                  </div>
                  <h2 className={styles.loginTitle}>Welcome back!</h2>
                  <p className={styles.loginSub}>Sign in to continue your PT AI experience.</p>

                  <div className={styles.formField}>
                    <label className={styles.fieldLabel} htmlFor="student-email">Email</label>
                    <div className={styles.inputShell}>
                      <span className={styles.inputIcon} aria-hidden="true">
                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
                          <rect x="3" y="5" width="18" height="14" rx="2" />
                          <path d="m4 7 8 6 8-6" />
                        </svg>
                      </span>
                      <input
                        id="student-email"
                        type="email"
                        className={styles.textInput}
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        placeholder="you@unmc.edu"
                        autoComplete="email"
                        required
                      />
                    </div>
                  </div>

                  <div className={styles.formField}>
                    <label className={styles.fieldLabel} htmlFor="student-password">Password</label>
                    <div className={styles.inputShell}>
                      <span className={styles.inputIcon} aria-hidden="true">
                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
                          <rect x="4" y="11" width="16" height="9" rx="2" />
                          <path d="M8 11V8a4 4 0 1 1 8 0v3" />
                        </svg>
                      </span>
                      <input
                        id="student-password"
                        type={showPassword ? "text" : "password"}
                        className={styles.textInput}
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        placeholder="Enter your password"
                        autoComplete="current-password"
                        required
                      />
                      <button
                        type="button"
                        className={styles.visibilityToggle}
                        aria-label={showPassword ? "Hide password" : "Show password"}
                        aria-pressed={showPassword}
                        onClick={() => setShowPassword((value) => !value)}
                      >
                        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6Z" />
                          <circle cx="12" cy="12" r="3" />
                        </svg>
                      </button>
                    </div>
                  </div>

                  {error && <div className={styles.errorText} role="alert">{error}</div>}

                  <button className={styles.signInButton} type="submit" disabled={busy}>
                    {busy ? "Signing in…" : "Sign In"}
                    <span className={styles.buttonArrow} aria-hidden="true">›</span>
                  </button>

                  <p className={styles.loginFoot}>
                    New student? <Link className={styles.inlineLink} to="/register">Create an account</Link>
                  </p>
                </form>
              )}
            </div>
          </aside>
        </div>
      </main>
    </div>
  );
}
