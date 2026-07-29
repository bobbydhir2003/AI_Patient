import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import {
  adminSearch,
  fetchNotifications,
  markAllNotificationsRead,
  type AdminNotification,
  type SearchResults,
} from "../../services/authApi";
import { caseLabel } from "../../portal/format";
import {
  IconAssessments,
  IconAudit,
  IconBell,
  IconChevronDown,
  IconCpu,
  IconKey,
  IconLogout,
  IconMic,
  IconPulse,
  IconServer,
  IconSearch,
  IconSessions,
  IconSettings,
  IconStudents,
  type IconProps,
} from "./icons";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "A";
  return (parts[0][0] + (parts[1]?.[0] ?? "")).toUpperCase();
}

type Icon = (p: IconProps) => ReactElement;

// The system pages, moved out of the sidebar into the header "System Settings"
// dropdown. Routes are unchanged, so active highlighting still works.
const SYSTEM_ITEMS: { to: string; label: string; icon: Icon; end?: boolean }[] = [
  { to: "/admin/system", label: "System Dashboard", icon: IconServer, end: true },
  { to: "/admin/system/voices", label: "Patient Voices", icon: IconMic },
  { to: "/admin/system/config", label: "AI Configuration", icon: IconCpu },
  { to: "/admin/system/credentials", label: "API Credentials", icon: IconKey },
  { to: "/admin/system/health", label: "System Health", icon: IconPulse },
  { to: "/admin/audit-log", label: "Admin Activity Log", icon: IconAudit, end: true },
];

const NOTIF_ICON: Record<string, Icon> = {
  voice: IconMic, credential: IconKey, config: IconCpu, system: IconServer,
  student: IconStudents, session: IconSessions, assessment: IconAssessments, activity: IconAudit,
};

function relTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function AdminTopbar() {
  const { token, user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResults | null>(null);
  const [searching, setSearching] = useState(false);
  const [openResults, setOpenResults] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [sysOpen, setSysOpen] = useState(false);
  const [notifOpen, setNotifOpen] = useState(false);
  const [feed, setFeed] = useState<AdminNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);

  const loadNotifications = useCallback(() => {
    if (!token) return;
    fetchNotifications(token)
      .then((f) => {
        setFeed(f.notifications);
        setUnread(f.unreadCount);
      })
      .catch(() => undefined);
  }, [token]);

  // Real notification feed: initial load + gentle 60s refresh.
  useEffect(() => {
    loadNotifications();
    const id = window.setInterval(loadNotifications, 60_000);
    return () => window.clearInterval(id);
  }, [loadNotifications]);

  // Debounced global search across students + sessions.
  useEffect(() => {
    if (!token || query.trim().length < 2) {
      setResults(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    const handle = setTimeout(() => {
      adminSearch(token, query.trim())
        .then((r) => {
          setResults(r);
          setOpenResults(true);
        })
        .catch(() => setResults(null))
        .finally(() => setSearching(false));
    }, 250);
    return () => clearTimeout(handle);
  }, [token, query]);

  // Close all popovers on outside click / Escape.
  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpenResults(false);
        setMenuOpen(false);
        setSysOpen(false);
        setNotifOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setSysOpen(false);
        setNotifOpen(false);
        setMenuOpen(false);
        setOpenResults(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, []);

  function go(path: string) {
    setOpenResults(false);
    setQuery("");
    setResults(null);
    setSysOpen(false);
    setNotifOpen(false);
    navigate(path);
  }

  async function markAll() {
    if (!token) return;
    try {
      await markAllNotificationsRead(token);
    } catch {
      /* non-fatal */
    }
    loadNotifications();
  }

  const hasHits = !!results && (results.students.length > 0 || results.sessions.length > 0);
  const systemActive = location.pathname.startsWith("/admin/system") || location.pathname === "/admin/audit-log";

  return (
    <header className="pt-topbar" ref={rootRef}>
      <div className="pt-topbar-brand">
        <img className="pt-topbar-logo" src="/branding/unmc-logo.png" alt="UNMC" />
        <span className="pt-topbar-title">PT AI Patient Simulator</span>
      </div>

      <div className="pt-topbar-search" role="search">
        <IconSearch className="pt-search-icon" width={16} height={16} />
        <label className="pt-visually-hidden" htmlFor="admin-global-search">
          Search students, sessions and cases
        </label>
        <input
          id="admin-global-search"
          className="pt-input"
          type="search"
          placeholder="Search student, ID, email, case, or session…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={() => hasHits && setOpenResults(true)}
          autoComplete="off"
          aria-expanded={openResults}
          aria-controls="admin-search-results"
        />
        {openResults && query.trim().length >= 2 && (
          <div className="pt-search-dropdown" id="admin-search-results" role="listbox">
            {searching && <div className="pt-search-group-label">Searching…</div>}
            {!searching && !hasHits && (
              <div className="pt-search-item" aria-disabled="true">No matches for “{query.trim()}”.</div>
            )}
            {results && results.students.length > 0 && (
              <>
                <div className="pt-search-group-label">Students</div>
                {results.students.map((s) => (
                  <button key={s.id} className="pt-search-item" role="option" onClick={() => go(`/admin/students/${s.id}`)}>
                    {s.name}
                    <small>{s.email || "no email"}{s.studentNumber ? ` · #${s.studentNumber}` : ""}</small>
                  </button>
                ))}
              </>
            )}
            {results && results.sessions.length > 0 && (
              <>
                <div className="pt-search-group-label">Sessions</div>
                {results.sessions.map((s) => (
                  <button key={s.sessionId} className="pt-search-item" role="option" onClick={() => go(`/admin/sessions/${s.sessionId}`)}>
                    {s.studentName} · {caseLabel(s.caseId)}
                    <small>{s.status} · {s.sessionId.slice(0, 8)}</small>
                  </button>
                ))}
              </>
            )}
          </div>
        )}
      </div>

      <div className="pt-topbar-right">
        {/* System Settings dropdown (system pages moved out of the sidebar) */}
        <div style={{ position: "relative" }}>
          <button
            type="button"
            className={`pt-sysbtn ${sysOpen || systemActive ? "open" : ""}`}
            onClick={() => { setSysOpen((v) => !v); setNotifOpen(false); }}
            aria-haspopup="menu"
            aria-expanded={sysOpen}
          >
            <IconSettings width={16} height={16} />
            <span className="pt-sysbtn-label">System Settings</span>
            <IconChevronDown width={14} height={14} />
          </button>
          {sysOpen && (
            <div className="pt-sysmenu" role="menu" aria-label="System settings">
              {SYSTEM_ITEMS.map(({ to, label, icon: Icon, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  role="menuitem"
                  className={({ isActive }) => `pt-navlink ${isActive ? "active" : ""}`}
                  onClick={() => setSysOpen(false)}
                >
                  <Icon />
                  <span>{label}</span>
                </NavLink>
              ))}
            </div>
          )}
        </div>

        {/* Real notification bell */}
        <div style={{ position: "relative" }}>
          <button
            className="pt-notif"
            type="button"
            aria-label={unread > 0 ? `${unread} unread notifications` : "Notifications"}
            aria-haspopup="menu"
            aria-expanded={notifOpen}
            onClick={() => { setNotifOpen((v) => !v); setSysOpen(false); if (!notifOpen) loadNotifications(); }}
          >
            <IconBell width={20} height={20} />
            {unread > 0 && <span className="pt-notif-badge">{unread > 99 ? "99+" : unread}</span>}
          </button>
          {notifOpen && (
            <div className="pt-notif-panel" role="menu" aria-label="Notifications">
              <div className="pt-notif-head">
                <span>Notifications{unread > 0 ? ` (${unread} unread)` : ""}</span>
                {unread > 0 && (
                  <button type="button" className="pt-notif-markall" onClick={markAll}>Mark all as read</button>
                )}
              </div>
              <div className="pt-notif-list">
                {feed.length === 0 ? (
                  <div className="pt-notif-empty">No new notifications</div>
                ) : (
                  feed.map((n) => {
                    const Icon = NOTIF_ICON[n.type] ?? IconAudit;
                    return (
                      <button
                        key={n.id}
                        type="button"
                        role="menuitem"
                        className={`pt-notif-item ${n.isRead ? "" : "unread"}`}
                        onClick={() => (n.link ? go(n.link) : setNotifOpen(false))}
                      >
                        <span className="pt-notif-ic" aria-hidden="true"><Icon width={16} height={16} /></span>
                        <span className="pt-notif-body">
                          <span className="pt-notif-title">{n.title}</span>
                          <span className="pt-notif-msg">{n.message}</span>
                          <span className="pt-notif-time">{relTime(n.createdAt)}</span>
                        </span>
                        {!n.isRead && <span className="pt-notif-dot" aria-label="unread" />}
                      </button>
                    );
                  })
                )}
              </div>
              <div className="pt-notif-foot">
                <button type="button" onClick={() => go("/admin/audit-log")}>View all activity</button>
              </div>
            </div>
          )}
        </div>

        <div style={{ position: "relative" }}>
          <button
            className="pt-topbar-user"
            type="button"
            onClick={() => { setMenuOpen((v) => !v); setSysOpen(false); setNotifOpen(false); }}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
          >
            <span className="pt-avatar" aria-hidden="true">{initials(user?.fullName ?? "Admin")}</span>
            <span className="pt-user-text">
              <span className="nm">{user?.fullName ?? "Admin"}</span>
              <span className="em">{user?.email}</span>
            </span>
          </button>
          {menuOpen && (
            <div className="pt-user-menu" role="menu">
              <button role="menuitem" onClick={() => go("/admin/profile")}>Profile</button>
              <button role="menuitem" onClick={() => { logout(); navigate("/login"); }}>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                  <IconLogout width={15} height={15} /> Logout
                </span>
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
