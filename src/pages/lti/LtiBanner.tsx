import { Link } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import styles from "./lti.module.css";

/** Shown on every page while this tab runs an AI iEXCEL course session. */
export function LtiBanner() {
  const { lti, logout } = useAuth();
  if (!lti) return null;
  const home = lti.role === "instructor" ? "/course/instructor" : "/course";
  return (
    <div className={styles.banner} role="region" aria-label="Course session">
      <span className={styles.bannerText}>
        <strong>AI iEXCEL course</strong> · {lti.context.title || lti.context.label}
        <span className={styles.rolePill}>{lti.role === "instructor" ? "Instructor" : "Student"}</span>
      </span>
      <span className={styles.bannerActions}>
        <Link to={home}>Course home</Link>
        <button type="button" onClick={logout}>
          Leave course session
        </button>
      </span>
    </div>
  );
}
