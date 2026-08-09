import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import styles from "./StudentDashboardPage.module.css";

function initials(name: string | undefined, email: string | undefined): string {
  const src = (name || email || "?").trim();
  const parts = src.split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || src[0]?.toUpperCase() || "?";
}

/** Student profile (bottom-nav "Profile"). Shows the REAL authenticated account
 * from useAuth and offers logout; admins also get a link to Admin Management. */
export function StudentProfilePage() {
  const { user, isAdmin, logout } = useAuth();
  const navigate = useNavigate();

  return (
    <div className={styles.wrap}>
      <div className={styles.head}>
        <div>
          <h1 className={styles.welcome}>Profile</h1>
          <p className={styles.welcomeSub}>Your account details.</p>
        </div>
      </div>

      <div className={styles.sideCard} style={{ maxWidth: 560 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 18 }}>
          <span className={styles.avatar} style={{ width: 56, height: 56, fontSize: "1.2rem" }}>
            {initials(user?.fullName, user?.email)}
          </span>
          <div>
            <div style={{ color: "#fff", fontWeight: 700, fontSize: "1.1rem" }}>{user?.fullName || "Student"}</div>
            <div style={{ color: "var(--color-text-muted)", fontSize: "0.85rem" }}>{isAdmin ? "PT Student • Admin" : "PT Student"}</div>
          </div>
        </div>

        <dl className="pt-uac-card-grid" style={{ gridTemplateColumns: "1fr" }}>
          <div><dt>Email</dt><dd>{user?.email || "—"}</dd></div>
          {user?.studentNumber && <div><dt>Student ID</dt><dd>{user.studentNumber}</dd></div>}
          <div><dt>Role</dt><dd>{isAdmin ? "Admin" : "Student"}</dd></div>
        </dl>

        {isAdmin && (
          <button type="button" className="pt-btn pt-btn-secondary pt-btn-block" style={{ marginTop: 16 }} onClick={() => navigate("/admin")}>
            Open Admin Management
          </button>
        )}
        <button type="button" className="pt-btn pt-btn-block" style={{ marginTop: 10 }} onClick={logout}>
          Log out
        </button>
      </div>
    </div>
  );
}
