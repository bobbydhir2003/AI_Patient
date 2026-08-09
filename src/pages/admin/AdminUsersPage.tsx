import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useAuth } from "../../state/AuthContext";
import { ApiError } from "../../services/api";
import { ConfirmModal, ErrorState, LoadingState, useToast } from "../../portal/ui";
import {
  approveAllPending,
  approveUser,
  bulkApproveUsers,
  bulkRejectUsers,
  changeUserRole,
  disableUser,
  enableUser,
  fetchUserSummary,
  fetchUsers,
  rejectUser,
  type AdminUser,
  type UserSummary,
} from "../../services/usersApi";

/* -------------------------------------------------------------------------- */
/*  Tabs, roles, sorting                                                       */
/* -------------------------------------------------------------------------- */
// Each tab maps to a real backend status query and a real summary count key.
const TABS: { key: string; label: string; status: string; count: keyof UserSummary | "total" }[] = [
  { key: "ALL", label: "All", status: "ALL", count: "total" },
  { key: "PENDING", label: "Pending", status: "PENDING", count: "pending" },
  { key: "APPROVED", label: "Approved", status: "ACTIVE", count: "active" },
  { key: "REJECTED", label: "Rejected", status: "REJECTED", count: "rejected" },
  { key: "DISABLED", label: "Disabled", status: "DISABLED", count: "disabled" },
  { key: "ADMINS", label: "Admins", status: "ADMINS", count: "admins" },
];
type TabKey = (typeof TABS)[number]["key"];
const ROLES = ["ALL", "student", "admin"] as const;
type RoleFilter = (typeof ROLES)[number];
type SortKey = "newest" | "oldest" | "name";
const PAGE_SIZES = [10, 25, 50] as const;

/* -------------------------------------------------------------------------- */
/*  Small presentational helpers                                               */
/* -------------------------------------------------------------------------- */
function initials(name: string, email: string): string {
  const src = (name || email || "?").trim();
  const parts = src.split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || src[0]?.toUpperCase() || "?";
}

function relTime(iso: string | null): string {
  if (!iso) return "";
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} hour${h > 1 ? "s" : ""} ago`;
  const d = Math.floor(h / 24);
  return `${d} day${d > 1 ? "s" : ""} ago`;
}
function absDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// Status label does not depend on colour alone: an icon + text carry meaning.
function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { cls: string; label: string; dot: string }> = {
    PENDING: { cls: "pt-badge-amber", label: "Pending", dot: "◔" },
    ACTIVE: { cls: "pt-badge-green", label: "Approved", dot: "●" },
    REJECTED: { cls: "pt-badge-red", label: "Rejected", dot: "✕" },
    DISABLED: { cls: "pt-badge-gray", label: "Disabled", dot: "⊘" },
  };
  const s = map[status] ?? { cls: "pt-badge-gray", label: status, dot: "•" };
  return (
    <span className={`pt-badge ${s.cls}`}>
      <span aria-hidden="true" style={{ marginRight: 4 }}>{s.dot}</span>
      {s.label}
    </span>
  );
}
function RoleBadge({ role }: { role: string }) {
  const admin = role === "admin";
  return <span className={`pt-badge ${admin ? "pt-badge-amber" : "pt-badge-blue"}`}>{admin ? "Admin" : "Student"}</span>;
}

// Icons (inline, no new deps)
const ICheck = () => <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12.5l4 4 10-10" /></svg>;
const IX = () => <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 6l12 12M18 6L6 18" /></svg>;
const IDots = () => <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="12" cy="19" r="1.8" /></svg>;
const ISearch = () => <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.2-3.2" /></svg>;
const IFilter = () => <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 5h18M6 12h12M10 19h4" /></svg>;
const IRefresh = () => <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 1 0-.5 4M20 5v6h-6" /></svg>;
const IExport = () => <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 3v12M8 11l4 4 4-4M5 21h14" /></svg>;
const IUsers = () => <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="9" cy="8" r="3.2" /><path d="M3 20c0-3 2.7-5 6-5s6 2 6 5" /><path d="M16 4.5a3 3 0 0 1 0 6M18 20c0-2.4-1-4-3-4.6" /></svg>;
const IClock = () => <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>;
const ICheckC = () => <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M8.5 12.5l2.5 2.5 4.5-5" /></svg>;
const IXC = () => <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M9 9l6 6M15 9l-6 6" /></svg>;
const IBan = () => <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9" /><path d="M5.6 5.6l12.8 12.8" /></svg>;
const IShield = () => <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6l7-3Z" /></svg>;

const STAT_CARDS: {
  key: keyof UserSummary; label: string; hint: string; accent: string; icon: () => ReactNode;
}[] = [
  { key: "total", label: "Total Users", hint: "All accounts", accent: "red", icon: IUsers },
  { key: "pending", label: "Pending Approvals", hint: "Requires your review", accent: "amber", icon: IClock },
  { key: "active", label: "Approved Users", hint: "Active and ready", accent: "green", icon: ICheckC },
  { key: "rejected", label: "Rejected Users", hint: "Declined requests", accent: "red2", icon: IXC },
  { key: "disabled", label: "Disabled Users", hint: "Temporarily disabled", accent: "gray", icon: IBan },
  { key: "admins", label: "Admins", hint: "Full access", accent: "blue", icon: IShield },
];

/* -------------------------------------------------------------------------- */
/*  Per-row action menu (three-dot)                                            */
/* -------------------------------------------------------------------------- */
function RowMenu({ items, label }: { items: { label: string; danger?: boolean; onSelect: () => void }[]; label: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  if (items.length === 0) return null;
  return (
    <div className="pt-rowmenu" ref={ref}>
      <button
        type="button"
        className="pt-iconbtn pt-iconbtn-ghost"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        onClick={() => setOpen((v) => !v)}
      >
        <IDots />
      </button>
      {open && (
        <div className="pt-rowmenu-pop" role="menu">
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              role="menuitem"
              className={`pt-rowmenu-item ${it.danger ? "danger" : ""}`}
              onClick={() => { setOpen(false); it.onSelect(); }}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                       */
/* -------------------------------------------------------------------------- */
type RowConfirm =
  | { kind: "reject"; user: AdminUser }
  | { kind: "disable"; user: AdminUser }
  | { kind: "role"; user: AdminUser; role: "admin" | "student" }
  | { kind: "approve-all"; count: number }
  | { kind: "approve-selected"; ids: string[] }
  | { kind: "reject-selected"; ids: string[] }
  | null;

export function AdminUsersPage() {
  const { token, user } = useAuth();
  const toast = useToast();
  const [rows, setRows] = useState<AdminUser[] | null>(null);
  const [summary, setSummary] = useState<UserSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<TabKey>("PENDING");
  const [roleFilter, setRoleFilter] = useState<RoleFilter>("ALL");
  const [sort, setSort] = useState<SortKey>("newest");
  const [showFilters, setShowFilters] = useState(false);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(10);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<RowConfirm>(null);
  const [bulkBusy, setBulkBusy] = useState(false);

  const activeTab = TABS.find((t) => t.key === tab)!;

  const loadSummary = useCallback(() => {
    if (!token) return;
    fetchUserSummary(token).then(setSummary).catch(() => setSummary(null));
  }, [token]);

  const load = useCallback(() => {
    if (!token) return;
    setLoading(true);
    fetchUsers(token, activeTab.status)
      .then((r) => { setRows(r); setError(null); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load users."))
      .finally(() => setLoading(false));
  }, [token, activeTab.status]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadSummary(); }, [loadSummary]);
  useEffect(() => { setSelected(new Set()); setPage(1); }, [tab, roleFilter, search, sort, pageSize]);

  const refreshAll = useCallback(() => { load(); loadSummary(); }, [load, loadSummary]);

  /* ---- client-side search + role filter + sort + pagination over the tab --- */
  const filteredRows = useMemo(() => {
    let out = rows ?? [];
    if (roleFilter !== "ALL") out = out.filter((u) => u.role === roleFilter);
    const q = search.trim().toLowerCase();
    if (q) {
      out = out.filter((u) =>
        (u.fullName || "").toLowerCase().includes(q) ||
        u.email.toLowerCase().includes(q) ||
        (u.studentNumber || "").toLowerCase().includes(q),
      );
    }
    out = [...out].sort((a, b) => {
      if (sort === "name") return (a.fullName || a.email).localeCompare(b.fullName || b.email);
      const da = new Date(a.createdAt).getTime();
      const db = new Date(b.createdAt).getTime();
      return sort === "oldest" ? da - db : db - da;
    });
    return out;
  }, [rows, roleFilter, search, sort]);

  const totalPages = Math.max(1, Math.ceil(filteredRows.length / pageSize));
  const clampedPage = Math.min(page, totalPages);
  const pageRows = useMemo(
    () => filteredRows.slice((clampedPage - 1) * pageSize, clampedPage * pageSize),
    [filteredRows, clampedPage, pageSize],
  );

  const allSelected = pageRows.length > 0 && pageRows.every((u) => selected.has(u.id));
  const someSelected = pageRows.some((u) => selected.has(u.id));
  const headerCbRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (headerCbRef.current) headerCbRef.current.indeterminate = someSelected && !allSelected;
  }, [someSelected, allSelected]);

  const selectedPending = useMemo(
    () => (rows ?? []).filter((u) => selected.has(u.id) && u.accountStatus === "PENDING").map((u) => u.id),
    [rows, selected],
  );
  const selectedRejectable = useMemo(
    () => (rows ?? [])
      .filter((u) => selected.has(u.id) && u.id !== user?.id && u.accountStatus !== "REJECTED")
      .map((u) => u.id),
    [rows, selected, user?.id],
  );

  function toggleRow(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }
  function toggleAll() {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allSelected) pageRows.forEach((u) => next.delete(u.id));
      else pageRows.forEach((u) => next.add(u.id));
      return next;
    });
  }

  /* ---- single-row action (modal-gated for destructive/role changes) ------- */
  async function run(id: string, fn: () => Promise<unknown>, okMsg: string) {
    if (busyId) return;
    setBusyId(id);
    try {
      await fn();
      toast.success(okMsg);
      refreshAll();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Action failed.";
      setError(msg);
      toast.error(msg);
    } finally {
      setBusyId(null);
    }
  }

  /* ---- bulk actions ------------------------------------------------------- */
  async function runBulk(
    fn: () => Promise<{ succeeded: string[]; skipped: { userId: string; reason: string }[]; summary: UserSummary }>,
    verb: string,
  ) {
    if (bulkBusy) return;
    setBulkBusy(true);
    try {
      const res = await fn();
      setSummary(res.summary);
      const okN = res.succeeded.length;
      const skipN = res.skipped.length;
      if (okN) toast.success(`${verb} ${okN} account${okN === 1 ? "" : "s"}.${skipN ? ` ${skipN} skipped.` : ""}`);
      else toast.error(skipN ? `No accounts ${verb.toLowerCase()}; ${skipN} skipped.` : "No eligible accounts.");
      setSelected(new Set());
      load();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Bulk action failed.";
      setError(msg);
      toast.error(msg);
    } finally {
      setBulkBusy(false);
      setConfirm(null);
    }
  }

  async function doRowConfirm() {
    if (!confirm) return;
    if (confirm.kind === "approve-all") return runBulk(() => approveAllPending(token), "Approved");
    if (confirm.kind === "approve-selected") return runBulk(() => bulkApproveUsers(token, confirm.ids), "Approved");
    if (confirm.kind === "reject-selected") return runBulk(() => bulkRejectUsers(token, confirm.ids), "Rejected");
    const u = confirm.kind === "role" ? confirm.user : confirm.user;
    setConfirm(null);
    if (confirm.kind === "reject") return run(u.id, () => rejectUser(token, u.id), "Account rejected.");
    if (confirm.kind === "disable") return run(u.id, () => disableUser(token, u.id), "Account disabled.");
    if (confirm.kind === "role") return run(u.id, () => changeUserRole(token, u.id, confirm.role), "Role updated.");
  }

  /* ---- CSV export of the currently filtered rows (real data) --------------- */
  function exportCsv() {
    const cols = ["Name", "Email", "Student ID", "Role", "Status", "Requested", "Last Login"];
    const esc = (v: string) => `"${(v ?? "").replace(/"/g, '""')}"`;
    const lines = [cols.join(",")].concat(
      filteredRows.map((u) => [
        u.fullName || "", u.email, u.studentNumber || "", u.role, u.accountStatus,
        u.createdAt || "", u.lastLoginAt || "",
      ].map(esc).join(",")),
    );
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `user-accounts-${activeTab.key.toLowerCase()}-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    toast.success(`Exported ${filteredRows.length} row${filteredRows.length === 1 ? "" : "s"}.`);
  }

  /* ---- per-row action controls -------------------------------------------- */
  function quickActions(u: AdminUser) {
    const isSelf = u.id === user?.id;
    const disabled = busyId === u.id;
    const menuItems: { label: string; danger?: boolean; onSelect: () => void }[] = [];
    if (u.accountStatus === "ACTIVE" && !isSelf) {
      if (u.role === "student") menuItems.push({ label: "Make Admin", onSelect: () => setConfirm({ kind: "role", user: u, role: "admin" }) });
      if (u.role === "admin") menuItems.push({ label: "Make Student", onSelect: () => setConfirm({ kind: "role", user: u, role: "student" }) });
      menuItems.push({ label: "Disable account", danger: true, onSelect: () => setConfirm({ kind: "disable", user: u }) });
    }
    if (u.accountStatus === "DISABLED" || u.accountStatus === "REJECTED") {
      menuItems.push({ label: "Enable account", onSelect: () => run(u.id, () => enableUser(token, u.id), "Account enabled.") });
    }

    return (
      <div className="pt-uac-actions">
        {u.accountStatus === "PENDING" && (
          <>
            <button
              type="button"
              className="pt-iconbtn pt-iconbtn-green"
              aria-label={`Approve ${u.email}`}
              disabled={disabled}
              onClick={() => run(u.id, () => approveUser(token, u.id), "Account approved.")}
            >
              <ICheck />
            </button>
            <button
              type="button"
              className="pt-iconbtn pt-iconbtn-red"
              aria-label={`Reject ${u.email}`}
              disabled={disabled}
              onClick={() => setConfirm({ kind: "reject", user: u })}
            >
              <IX />
            </button>
          </>
        )}
        {(u.accountStatus === "DISABLED" || u.accountStatus === "REJECTED") && (
          <button
            type="button"
            className="pt-iconbtn pt-iconbtn-green"
            aria-label={`Enable ${u.email}`}
            disabled={disabled}
            onClick={() => run(u.id, () => enableUser(token, u.id), "Account enabled.")}
          >
            <ICheck />
          </button>
        )}
        <RowMenu items={menuItems} label={`More actions for ${u.email}`} />
      </div>
    );
  }

  const confirmContent = (() => {
    if (!confirm) return null;
    switch (confirm.kind) {
      case "approve-all":
        return { title: "Approve all pending users?", body: `You are about to approve ${confirm.count} pending account${confirm.count === 1 ? "" : "s"}. These users will be able to access the Patient Simulator.`, label: `Approve ${confirm.count} User${confirm.count === 1 ? "" : "s"}`, danger: false };
      case "approve-selected":
        return { title: "Approve selected users?", body: `You are about to approve ${confirm.ids.length} pending account${confirm.ids.length === 1 ? "" : "s"}.`, label: `Approve ${confirm.ids.length}`, danger: false };
      case "reject-selected":
        return { title: "Reject selected users?", body: `You are about to reject ${confirm.ids.length} account request${confirm.ids.length === 1 ? "" : "s"}.`, label: `Reject ${confirm.ids.length}`, danger: true };
      case "reject":
        return { title: "Reject this account?", body: `Reject ${confirm.user.email}? They will not be able to sign in.`, label: "Reject account", danger: true };
      case "disable":
        return { title: "Disable this account?", body: `Disable ${confirm.user.email}? They will be signed out and blocked until re-enabled.`, label: "Disable account", danger: true };
      case "role":
        return { title: confirm.role === "admin" ? "Grant admin access?" : "Change to student?", body: confirm.role === "admin" ? `Make ${confirm.user.email} an administrator? Admins have full access to the System Dashboard.` : `Change ${confirm.user.email} to a student account?`, label: confirm.role === "admin" ? "Make Admin" : "Make Student", danger: false };
    }
  })();

  const showingFrom = filteredRows.length === 0 ? 0 : (clampedPage - 1) * pageSize + 1;
  const showingTo = Math.min(clampedPage * pageSize, filteredRows.length);
  const selCount = selected.size;

  return (
    <div className="pt-uac">
      {/* Header */}
      <div className="pt-uac-head">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>User Accounts</h1>
          <p className="pt-page-sub">Approve new accounts, manage roles, and control user access.</p>
        </div>
        {tab === "PENDING" && (
          <button
            type="button"
            className="pt-btn pt-btn-success pt-uac-approveall"
            disabled={bulkBusy || (summary?.pending ?? 0) === 0}
            onClick={() => setConfirm({ kind: "approve-all", count: summary?.pending ?? 0 })}
          >
            <ICheck /> Approve All Pending
          </button>
        )}
      </div>

      {/* Tabs with real counts */}
      <div className="pt-uac-tabs" role="tablist" aria-label="Filter accounts by status">
        {TABS.map((t) => {
          const n = summary ? (summary[t.count as keyof UserSummary] ?? 0) : null;
          return (
            <button
              key={t.key}
              role="tab"
              aria-selected={tab === t.key}
              className={`pt-uac-tab ${tab === t.key ? "active" : ""}`}
              onClick={() => setTab(t.key)}
            >
              {t.label}
              <span className="pt-uac-tabcount">{n ?? "—"}</span>
            </button>
          );
        })}
      </div>

      {/* Stat cards — real DB counts */}
      <div className="pt-uac-stats">
        {STAT_CARDS.map((c) => (
          <div key={c.key} className={`pt-statcard accent-${c.accent}`}>
            <div className="pt-statcard-top">
              <span className="pt-statcard-lbl">{c.label}</span>
              <span className="pt-statcard-ic" aria-hidden="true">{c.icon()}</span>
            </div>
            <div className="pt-statcard-val">{summary ? summary[c.key] : "—"}</div>
            <div className="pt-statcard-hint">{c.hint}</div>
          </div>
        ))}
      </div>

      {error && <p className="pt-error-text" role="alert" style={{ marginBottom: "var(--space-4)" }}>{error}</p>}

      {/* Controls: search + role + filters | refresh + export */}
      <div className="pt-uac-controls">
        <div className="pt-uac-search">
          <span className="pt-uac-search-ic" aria-hidden="true"><ISearch /></span>
          <input
            type="search"
            className="pt-input"
            placeholder="Search by name, email, or student ID…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search users by name, email, or student ID"
          />
        </div>
        <select className="pt-input pt-uac-role" value={roleFilter} onChange={(e) => setRoleFilter(e.target.value as RoleFilter)} aria-label="Filter by role">
          {ROLES.map((r) => <option key={r} value={r}>{r === "ALL" ? "All Roles" : r === "admin" ? "Admin" : "Student"}</option>)}
        </select>
        <button type="button" className={`pt-btn pt-btn-secondary pt-btn-icon ${showFilters ? "active" : ""}`} aria-expanded={showFilters} onClick={() => setShowFilters((v) => !v)}>
          <IFilter /> Filters
        </button>
        <div className="pt-uac-controls-right">
          <button type="button" className="pt-btn pt-btn-secondary pt-btn-icon" onClick={refreshAll} aria-label="Refresh"><IRefresh /> Refresh</button>
          <button type="button" className="pt-btn pt-btn-secondary pt-btn-icon" onClick={exportCsv} aria-label="Export current view as CSV" disabled={filteredRows.length === 0}><IExport /> Export</button>
        </div>
      </div>

      {showFilters && (
        <div className="pt-uac-filterpanel">
          <label className="pt-uac-fp-field">
            <span>Sort by</span>
            <select className="pt-input" value={sort} onChange={(e) => setSort(e.target.value as SortKey)}>
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
              <option value="name">Name (A–Z)</option>
            </select>
          </label>
          <button type="button" className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => { setSearch(""); setRoleFilter("ALL"); setSort("newest"); }}>Clear filters</button>
        </div>
      )}

      {/* Table / cards */}
      {loading && !rows ? (
        <LoadingState label="Loading users…" />
      ) : error && !rows ? (
        <ErrorState message={error} onRetry={load} />
      ) : (
        <div className="pt-uac-tablecard">
          {/* Desktop table */}
          <div className="pt-uac-tablewrap">
            <table className="pt-uac-table">
              <thead>
                <tr>
                  <th style={{ width: 40 }}>
                    <input ref={headerCbRef} type="checkbox" checked={allSelected} onChange={toggleAll} aria-label="Select all on this page" disabled={pageRows.length === 0} />
                  </th>
                  <th>User</th><th>Email</th><th>Student ID</th><th>Role</th><th>Status</th><th>Requested</th><th className="pt-uac-th-actions">Actions</th>
                </tr>
              </thead>
              <tbody>
                {pageRows.length === 0 && <tr><td colSpan={8} className="pt-muted" style={{ padding: "var(--space-5)", textAlign: "center" }}>No matching accounts.</td></tr>}
                {pageRows.map((u) => (
                  <tr key={u.id} className={selected.has(u.id) ? "pt-uac-selected" : undefined}>
                    <td><input type="checkbox" checked={selected.has(u.id)} onChange={() => toggleRow(u.id)} aria-label={`Select ${u.email}`} /></td>
                    <td>
                      <div className="pt-uac-usercell">
                        <span className="pt-uac-avatar" aria-hidden="true">{initials(u.fullName, u.email)}</span>
                        <span className="pt-uac-usermeta">
                          <span className="pt-uac-username">{u.fullName || "—"}</span>
                          <span className="pt-uac-usersub">{u.email}</span>
                        </span>
                      </div>
                    </td>
                    <td className="pt-uac-emailcol">{u.email}</td>
                    <td>{u.studentNumber || "—"}</td>
                    <td><RoleBadge role={u.role} /></td>
                    <td><StatusBadge status={u.accountStatus} /></td>
                    <td>
                      <div className="pt-uac-when">
                        <span>{absDate(u.createdAt)}</span>
                        <span className="pt-uac-when-rel">{relTime(u.createdAt)}</span>
                      </div>
                    </td>
                    <td>{quickActions(u)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Mobile cards */}
          <div className="pt-uac-cards">
            {pageRows.length === 0 && <div className="pt-muted" style={{ padding: "var(--space-4)" }}>No matching accounts.</div>}
            {pageRows.map((u) => (
              <div key={u.id} className={`pt-uac-card ${selected.has(u.id) ? "sel" : ""}`}>
                <div className="pt-uac-card-top">
                  <input type="checkbox" checked={selected.has(u.id)} onChange={() => toggleRow(u.id)} aria-label={`Select ${u.email}`} />
                  <span className="pt-uac-avatar" aria-hidden="true">{initials(u.fullName, u.email)}</span>
                  <div className="pt-uac-card-id">
                    <span className="pt-uac-username">{u.fullName || "—"}</span>
                    <span className="pt-uac-usersub">{u.email}</span>
                  </div>
                </div>
                <dl className="pt-uac-card-grid">
                  <div><dt>Student ID</dt><dd>{u.studentNumber || "—"}</dd></div>
                  <div><dt>Role</dt><dd><RoleBadge role={u.role} /></dd></div>
                  <div><dt>Status</dt><dd><StatusBadge status={u.accountStatus} /></dd></div>
                  <div><dt>Requested</dt><dd>{absDate(u.createdAt)}<span className="pt-uac-when-rel"> · {relTime(u.createdAt)}</span></dd></div>
                </dl>
                <div className="pt-uac-card-actions">{quickActions(u)}</div>
              </div>
            ))}
          </div>

          {/* Pagination */}
          <div className="pt-uac-pagination">
            <span className="pt-muted">Showing {showingFrom} to {showingTo} of {filteredRows.length} {activeTab.label.toLowerCase()} user{filteredRows.length === 1 ? "" : "s"}</span>
            <div className="pt-uac-pag-right">
              <label className="pt-uac-rpp">
                Rows per page:
                <select className="pt-input" value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))}>
                  {PAGE_SIZES.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
              <div className="pt-uac-pag-btns">
                <button className="pt-iconbtn pt-iconbtn-ghost" disabled={clampedPage <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))} aria-label="Previous page">‹</button>
                {Array.from({ length: totalPages }, (_, i) => i + 1)
                  .filter((p) => p === 1 || p === totalPages || Math.abs(p - clampedPage) <= 1)
                  .map((p, idx, arr) => (
                    <span key={p} style={{ display: "inline-flex", alignItems: "center" }}>
                      {idx > 0 && arr[idx - 1] !== p - 1 && <span className="pt-muted" style={{ padding: "0 4px" }}>…</span>}
                      <button className={`pt-uac-pagnum ${p === clampedPage ? "active" : ""}`} aria-current={p === clampedPage ? "page" : undefined} onClick={() => setPage(p)}>{p}</button>
                    </span>
                  ))}
                <button className="pt-iconbtn pt-iconbtn-ghost" disabled={clampedPage >= totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))} aria-label="Next page">›</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Sticky bulk action bar */}
      {selCount > 0 && (
        <div className="pt-uac-bulkbar" role="region" aria-label="Bulk actions">
          <div className="pt-uac-bulk-left">
            <span className="pt-uac-bulk-count">{selCount} selected</span>
            <button type="button" className="pt-uac-bulk-clear" onClick={() => setSelected(new Set())}>Clear selection</button>
          </div>
          <div className="pt-uac-bulk-right">
            <button
              type="button"
              className="pt-btn pt-btn-success"
              disabled={bulkBusy || selectedPending.length === 0}
              onClick={() => setConfirm({ kind: "approve-selected", ids: selectedPending })}
            >
              <ICheck /> Approve Selected{selectedPending.length ? ` (${selectedPending.length})` : ""}
            </button>
            <button
              type="button"
              className="pt-btn pt-btn-danger"
              disabled={bulkBusy || selectedRejectable.length === 0}
              onClick={() => setConfirm({ kind: "reject-selected", ids: selectedRejectable })}
            >
              <IX /> Reject Selected{selectedRejectable.length ? ` (${selectedRejectable.length})` : ""}
            </button>
          </div>
        </div>
      )}

      {confirm && confirmContent && (
        <ConfirmModal
          title={confirmContent.title}
          body={confirmContent.body}
          confirmLabel={confirmContent.label}
          danger={confirmContent.danger}
          busy={bulkBusy}
          onConfirm={doRowConfirm}
          onCancel={() => (bulkBusy ? undefined : setConfirm(null))}
        />
      )}
    </div>
  );
}
