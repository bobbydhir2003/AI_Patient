import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../state/AuthContext";
import { ApiError } from "../../services/api";
import { ErrorState, LoadingState } from "../../portal/ui";
import {
  approveUser,
  changeUserRole,
  disableUser,
  enableUser,
  fetchUsers,
  rejectUser,
  type AdminUser,
} from "../../services/usersApi";

const FILTERS = ["ALL", "PENDING", "ACTIVE", "REJECTED", "DISABLED", "ADMINS"] as const;
type Filter = (typeof FILTERS)[number];

function statusBadge(s: string) {
  const cls = { PENDING: "pt-badge-amber", ACTIVE: "pt-badge-green", REJECTED: "pt-badge-red", DISABLED: "pt-badge-gray" }[s] ?? "pt-badge-gray";
  return <span className={`pt-badge ${cls}`}>{s}</span>;
}

function roleBadge(r: string) {
  const label = r === "super_admin" ? "Super Admin" : r === "admin" ? "Admin" : "Student";
  const cls = r === "super_admin" ? "pt-badge-red" : r === "admin" ? "pt-badge-amber" : "pt-badge-gray";
  return <span className={`pt-badge ${cls}`}>{label}</span>;
}

export function AdminUsersPage() {
  const { token, user, isSuperAdmin } = useAuth();
  const [rows, setRows] = useState<AdminUser[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("PENDING");
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!token) return;
    setLoading(true);
    fetchUsers(token, filter)
      .then((r) => { setRows(r); setError(null); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load users."))
      .finally(() => setLoading(false));
  }, [token, filter]);

  useEffect(() => { load(); }, [load]);

  async function run(id: string, fn: () => Promise<unknown>, confirmMsg?: string) {
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    setBusyId(id);
    try {
      await fn();
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Action failed.");
    } finally {
      setBusyId(null);
    }
  }

  const fmt = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");

  function actions(u: AdminUser) {
    const isSelf = u.id === user?.id;
    const btns: React.ReactNode[] = [];
    if (u.accountStatus === "PENDING") {
      btns.push(<button key="ap" className="pt-btn pt-btn-sm" disabled={busyId === u.id} onClick={() => run(u.id, () => approveUser(token, u.id))}>Approve</button>);
      btns.push(<button key="rj" className="pt-btn pt-btn-sm pt-btn-danger" disabled={busyId === u.id} onClick={() => run(u.id, () => rejectUser(token, u.id))}>Reject</button>);
    }
    if (u.accountStatus === "ACTIVE" && !isSelf) {
      btns.push(<button key="di" className="pt-btn pt-btn-sm pt-btn-danger" disabled={busyId === u.id} onClick={() => run(u.id, () => disableUser(token, u.id), `Disable ${u.email}?`)}>Disable</button>);
    }
    if (u.accountStatus === "DISABLED" || u.accountStatus === "REJECTED") {
      btns.push(<button key="en" className="pt-btn pt-btn-sm" disabled={busyId === u.id} onClick={() => run(u.id, () => enableUser(token, u.id))}>Enable</button>);
    }
    // Role changes (never for self; super-admin transitions gated by role)
    if (u.accountStatus === "ACTIVE" && !isSelf) {
      if (u.role === "student") {
        btns.push(<button key="ma" className="pt-btn pt-btn-sm" disabled={busyId === u.id} onClick={() => run(u.id, () => changeUserRole(token, u.id, "admin"), `Make ${u.email} an admin?`)}>Make Admin</button>);
      }
      if (u.role === "admin") {
        btns.push(<button key="ms" className="pt-btn pt-btn-sm" disabled={busyId === u.id} onClick={() => run(u.id, () => changeUserRole(token, u.id, "student"), `Make ${u.email} a student?`)}>Make Student</button>);
        if (isSuperAdmin) btns.push(<button key="msa" className="pt-btn pt-btn-sm" disabled={busyId === u.id} onClick={() => run(u.id, () => changeUserRole(token, u.id, "super_admin"), `Grant Super Admin to ${u.email}?`)}>Make Super Admin</button>);
      }
      if (u.role === "super_admin" && isSuperAdmin) {
        btns.push(<button key="rsa" className="pt-btn pt-btn-sm pt-btn-danger" disabled={busyId === u.id} onClick={() => run(u.id, () => changeUserRole(token, u.id, "admin"), `Remove Super Admin from ${u.email}?`)}>Remove Super Admin</button>);
      }
    }
    return btns.length ? (
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>{btns}</div>
    ) : (
      <span className="pt-muted">—</span>
    );
  }

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>User Accounts</h1>
          <p className="pt-page-sub">Approve new accounts, enable/disable access, and manage roles.</p>
        </div>
        <div className="pt-header-actions">
          {FILTERS.map((f) => (
            <button key={f} type="button" className={`pt-btn pt-btn-sm ${filter === f ? "" : "pt-btn-secondary"}`} onClick={() => setFilter(f)}>{f}</button>
          ))}
        </div>
      </div>

      {error && <p className="pt-error-text" role="alert" style={{ marginBottom: "var(--space-4)" }}>{error}</p>}

      {loading && !rows ? (
        <LoadingState label="Loading users…" />
      ) : error && !rows ? (
        <ErrorState message={error} onRetry={load} />
      ) : (
        <div className="pt-card">
          <div className="pt-table-wrap">
            <table className="pt-table">
              <thead>
                <tr><th>Name</th><th>Email</th><th>Student ID</th><th>Status</th><th>Role</th><th>Created</th><th>Last Login</th><th>Actions</th></tr>
              </thead>
              <tbody>
                {rows && rows.length === 0 && <tr><td colSpan={8} className="pt-muted">No matching accounts.</td></tr>}
                {rows?.map((u) => (
                  <tr key={u.id}>
                    <td>{u.fullName || "—"}</td>
                    <td>{u.email}</td>
                    <td>{u.studentNumber || "—"}</td>
                    <td>{statusBadge(u.accountStatus)}</td>
                    <td>{roleBadge(u.role)}</td>
                    <td>{fmt(u.createdAt)}</td>
                    <td>{fmt(u.lastLoginAt)}</td>
                    <td>{actions(u)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
