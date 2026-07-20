import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { ApiError } from "../../services/api";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const user = await login(email.trim(), password);
      navigate(user.role === "admin" ? "/admin" : "/student/dashboard", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign in failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pt-auth-shell">
      <form className="pt-card pt-auth-card" onSubmit={onSubmit}>
        <h1 className="pt-h1">Sign in</h1>
        <p className="pt-sub">Access your PT AI Patient interviews and assessments.</p>

        <div className="pt-field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            className="pt-input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
          />
        </div>
        <div className="pt-field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            className="pt-input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </div>

        {error && <div className="pt-error-text" role="alert">{error}</div>}

        <button className="pt-btn pt-btn-block" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="pt-muted" style={{ marginTop: 16, fontSize: "0.88rem" }}>
          New student? <Link className="pt-link" to="/register">Create an account</Link>
        </p>
      </form>
    </div>
  );
}
