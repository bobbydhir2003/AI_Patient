import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { adminSearch, type SearchResults } from "../../services/authApi";
import { useAdminDashboard } from "../../pages/admin/AdminDashboardContext";
import { caseLabel } from "../../portal/format";
import { IconBell, IconLogout, IconSearch } from "./icons";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "A";
  return (parts[0][0] + (parts[1]?.[0] ?? "")).toUpperCase();
}

export function AdminTopbar() {
  const { token, user, logout } = useAuth();
  const { alertCount } = useAdminDashboard();
  const navigate = useNavigate();

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResults | null>(null);
  const [searching, setSearching] = useState(false);
  const [openResults, setOpenResults] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

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

  // Close menus on outside click.
  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpenResults(false);
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  function go(path: string) {
    setOpenResults(false);
    setQuery("");
    setResults(null);
    navigate(path);
  }

  const hasHits = !!results && (results.students.length > 0 || results.sessions.length > 0);

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
              <div className="pt-search-item" aria-disabled="true">
                No matches for “{query.trim()}”.
              </div>
            )}
            {results && results.students.length > 0 && (
              <>
                <div className="pt-search-group-label">Students</div>
                {results.students.map((s) => (
                  <button
                    key={s.id}
                    className="pt-search-item"
                    role="option"
                    onClick={() => go(`/admin/students/${s.id}`)}
                  >
                    {s.name}
                    <small>
                      {s.email || "no email"}
                      {s.studentNumber ? ` · #${s.studentNumber}` : ""}
                    </small>
                  </button>
                ))}
              </>
            )}
            {results && results.sessions.length > 0 && (
              <>
                <div className="pt-search-group-label">Sessions</div>
                {results.sessions.map((s) => (
                  <button
                    key={s.sessionId}
                    className="pt-search-item"
                    role="option"
                    onClick={() => go(`/admin/sessions/${s.sessionId}`)}
                  >
                    {s.studentName} · {caseLabel(s.caseId)}
                    <small>
                      {s.status} · {s.sessionId.slice(0, 8)}
                    </small>
                  </button>
                ))}
              </>
            )}
          </div>
        )}
      </div>

      <div className="pt-topbar-right">
        <button
          className="pt-notif"
          type="button"
          aria-label={
            alertCount > 0
              ? `${alertCount} categories need attention`
              : "Notifications"
          }
          onClick={() => navigate("/admin")}
        >
          <IconBell width={20} height={20} />
          {alertCount > 0 && <span className="pt-notif-badge">{alertCount}</span>}
        </button>

        <div style={{ position: "relative" }}>
          <button
            className="pt-topbar-user"
            type="button"
            onClick={() => setMenuOpen((v) => !v)}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
          >
            <span className="pt-avatar" aria-hidden="true">
              {initials(user?.fullName ?? "Admin")}
            </span>
            <span className="pt-user-text">
              <span className="nm">{user?.fullName ?? "Admin"}</span>
              <span className="em">{user?.email}</span>
            </span>
          </button>
          {menuOpen && (
            <div className="pt-user-menu" role="menu">
              <button role="menuitem" onClick={() => go("/admin/profile")}>
                Profile
              </button>
              <button
                role="menuitem"
                onClick={() => {
                  logout();
                  navigate("/login");
                }}
              >
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
