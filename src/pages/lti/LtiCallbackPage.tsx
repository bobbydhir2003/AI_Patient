import { useEffect, useRef, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { exchangeLaunchCode } from "../../services/ltiApi";
import { ApiError } from "../../services/api";
import { useAuth } from "../../state/AuthContext";
import { Spinner } from "../../portal/ui";

/** Landing point of an AI iEXCEL course launch. The one-time code arrives in
 * the URL fragment; it is removed from the address bar immediately and traded
 * once for a course session. */
export function LtiCallbackPage() {
  const navigate = useNavigate();
  const { beginLtiSession } = useAuth();
  const started = useRef(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (started.current) return; // StrictMode double effect: the code is single use
    started.current = true;
    const code = new URLSearchParams(window.location.hash.slice(1)).get("code");
    window.history.replaceState(null, "", window.location.pathname);
    if (!code) {
      setFailed(true);
      return;
    }
    exchangeLaunchCode(code)
      .then((session) => {
        beginLtiSession(session);
        navigate(session.role === "instructor" ? "/course/instructor" : "/course", { replace: true });
      })
      .catch((err: unknown) => {
        const reason = err instanceof ApiError && err.code === "network_error" ? "network" : "invalid_code";
        navigate(`/lti/error?reason=${reason}`, { replace: true });
      });
  }, [beginLtiSession, navigate]);

  if (failed) return <Navigate to="/lti/error?reason=invalid_code" replace />;
  return (
    <div className="pt-portal">
      <Spinner label="Opening your course…" />
    </div>
  );
}
