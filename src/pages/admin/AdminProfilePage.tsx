import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { ActiveBadge, LoadingState } from "../../portal/ui";
import { fmtDate, fmtDateTime } from "../../portal/format";

export function AdminProfilePage() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  if (!user) return <LoadingState label="Loading profile…" />;

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">Profile</h1>
          <p className="pt-page-sub">Your administrator account details.</p>
        </div>
        <ActiveBadge active={user.isActive} />
      </div>

      <div className="pt-card pt-section" style={{ maxWidth: 640 }}>
        <dl className="pt-kv">
          <dt>Name</dt>
          <dd>{user.fullName}</dd>
          <dt>Email</dt>
          <dd>{user.email}</dd>
          <dt>Role</dt>
          <dd style={{ textTransform: "capitalize" }}>{user.role}</dd>
          <dt>Account number</dt>
          <dd>{user.studentNumber || "—"}</dd>
          <dt>Member since</dt>
          <dd>{fmtDate(user.createdAt)}</dd>
          <dt>Last login</dt>
          <dd>{fmtDateTime(user.lastLoginAt)}</dd>
        </dl>

        <div className="pt-row" style={{ marginTop: 20 }}>
          <button
            className="pt-btn pt-btn-danger pt-btn-sm"
            onClick={() => {
              logout();
              navigate("/login");
            }}
          >
            Log out
          </button>
          <span className="pt-muted" style={{ fontSize: "0.8rem" }}>
            Profile editing is managed by your institution.
          </span>
        </div>
      </div>
    </div>
  );
}
