import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { ApiError } from "../../services/api";

export function RegisterPage() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [studentNumber, setStudentNumber] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setBusy(true);
    try {
      await register({
        fullName: fullName.trim(),
        email: email.trim(),
        password,
        studentNumber: studentNumber.trim(),
      });
      navigate("/student/dashboard", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Registration failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pt-auth-shell">
      <form className="pt-card pt-auth-card" onSubmit={onSubmit}>
        <h1 className="pt-h1">Create your student account</h1>
        <p className="pt-sub">
          If you already have interview sessions under your student number, they will be linked
          automatically.
        </p>

        <div className="pt-field">
          <label htmlFor="fullName">Full name</label>
          <input id="fullName" className="pt-input" value={fullName}
            onChange={(e) => setFullName(e.target.value)} required />
        </div>
        <div className="pt-field">
          <label htmlFor="email">Email</label>
          <input id="email" type="email" className="pt-input" value={email}
            onChange={(e) => setEmail(e.target.value)} autoComplete="email" required />
        </div>
        <div className="pt-field">
          <label htmlFor="studentNumber">Student number</label>
          <input id="studentNumber" className="pt-input" value={studentNumber}
            onChange={(e) => setStudentNumber(e.target.value)} />
        </div>
        <div className="pt-field">
          <label htmlFor="password">Password</label>
          <input id="password" type="password" className="pt-input" value={password}
            onChange={(e) => setPassword(e.target.value)} autoComplete="new-password"
            minLength={8} required />
        </div>

        {error && <div className="pt-error-text" role="alert">{error}</div>}

        <button className="pt-btn pt-btn-block" type="submit" disabled={busy}>
          {busy ? "Creating account…" : "Create account"}
        </button>

        <p className="pt-muted" style={{ marginTop: 16, fontSize: "0.88rem" }}>
          Already have an account? <Link className="pt-link" to="/login">Sign in</Link>
        </p>
      </form>
    </div>
  );
}
