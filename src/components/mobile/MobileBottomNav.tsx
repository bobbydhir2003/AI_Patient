import type { ReactElement } from "react";
import { NavLink } from "react-router-dom";

/**
 * Fixed bottom navigation for the mobile patient app (Screenshot 2). Hidden on
 * tablet/desktop via CSS (`.pt-bottomnav` only displays <=767px). Every item
 * routes to a REAL student destination backed by the existing APIs; the active
 * route is highlighted in red. Respects the iOS safe-area inset (see CSS).
 */
const IHome = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 11l8-7 8 7" /><path d="M6 10v9h12v-9" /></svg>
);
const ICases = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="4" y="4" width="7" height="7" rx="1.5" /><rect x="13" y="4" width="7" height="7" rx="1.5" /><rect x="4" y="13" width="7" height="7" rx="1.5" /><rect x="13" y="13" width="7" height="7" rx="1.5" /></svg>
);
const IAssess = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="6" y="4" width="12" height="17" rx="2" /><path d="M9 4V3h6v1" /><path d="M8.5 13l2 2 3.5-4" /></svg>
);
const IActivity = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 12h4l2 6 4-14 2 8h6" /></svg>
);
const IProfile = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="8.5" r="3.5" /><path d="M5 20c0-3.6 3.1-6 7-6s7 2.4 7 6" /></svg>
);

const ITEMS: { to: string; label: string; icon: () => ReactElement; end?: boolean }[] = [
  { to: "/student/dashboard", label: "Home", icon: IHome, end: true },
  { to: "/student/cases", label: "Cases", icon: ICases },
  { to: "/student/assessments", label: "Assessments", icon: IAssess },
  { to: "/student/activity", label: "Activity", icon: IActivity },
  { to: "/student/profile", label: "Profile", icon: IProfile },
];

export function MobileBottomNav() {
  return (
    <nav className="pt-bottomnav" aria-label="Primary">
      {ITEMS.map(({ to, label, icon: Icon, end }) => (
        <NavLink key={to} to={to} end={end} className={({ isActive }) => (isActive ? "active" : undefined)}>
          <Icon />
          <span>{label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
