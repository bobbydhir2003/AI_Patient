import { useSearchParams } from "react-router-dom";
import { launchErrorMessage } from "../../services/ltiApi";

/** Launch failures (/lti/error) and ended or expired course sessions
 * (/lti/ended). Shows a fixed, human-readable message for the reason code. */
export function LtiMessagePage({ kind }: { kind: "error" | "ended" }) {
  const [params] = useSearchParams();
  const reason = params.get("reason") ?? (kind === "ended" ? "ended" : null);
  const title = kind === "ended" ? "Course session ended" : "We couldn't open your course";
  return (
    <div className="pt-auth-shell">
      <div className="pt-card pt-auth-card" role={kind === "error" ? "alert" : "status"}>
        <h1 className="pt-h1">{title}</h1>
        <p className="pt-muted">{launchErrorMessage(reason)}</p>
        <p className="pt-muted" style={{ marginTop: 16 }}>
          Students and staff with an AI Standardized Patient account can still{" "}
          <a href="/">sign in directly</a>.
        </p>
        {reason && kind === "error" && (
          <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: 16 }}>
            Reference: {reason}
          </p>
        )}
      </div>
    </div>
  );
}
