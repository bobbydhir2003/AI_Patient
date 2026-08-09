import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../../services/api";
import { requestAccess, type AccessRequestResult } from "../../services/accessApi";

export function RequestAccessPage() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AccessRequestResult | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      setResult(await requestAccess(email.trim()));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not submit your request. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pt-auth-shell">
      <form className="pt-card pt-auth-card" onSubmit={onSubmit}>
        <h1 className="pt-h1">Request access</h1>
        <p className="pt-sub">
          Enter your email to request access. An administrator will review your request.
        </p>

        {result ? (
          <div className="pt-access-result" role="status">
            <p style={{ marginTop: 0 }}>{result.message}</p>
            {result.result === "ALREADY_APPROVED" ? (
              <Link className="pt-btn pt-btn-block" to="/">Continue to sign in</Link>
            ) : (
              <Link className="pt-link" to="/">Back to sign in</Link>
            )}
          </div>
        ) : (
          <>
            <div className="pt-field">
              <label htmlFor="email">Email address</label>
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

            {error && <div className="pt-error-text" role="alert">{error}</div>}

            <button className="pt-btn pt-btn-block" type="submit" disabled={busy}>
              {busy ? "Submitting…" : "Request Access"}
            </button>

            <p className="pt-muted" style={{ marginTop: 16, fontSize: "0.88rem" }}>
              Already have an account? <Link className="pt-link" to="/">Sign in</Link>
            </p>
          </>
        )}
      </form>
    </div>
  );
}
