import { NavLink } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import {
  IconAssessments,
  IconDashboard,
  IconLogout,
  IconProfile,
  IconSessions,
  IconStudents,
  IconTranscript,
} from "./icons";
import type { ComponentType, SVGProps } from "react";

type Icon = ComponentType<SVGProps<SVGSVGElement>>;

interface NavItem {
  to: string;
  label: string;
  icon: Icon;
  end?: boolean;
}

const SECTIONS: { title: string; items: NavItem[] }[] = [
  {
    title: "Academic Management",
    items: [
      { to: "/admin", label: "Dashboard", icon: IconDashboard, end: true },
      { to: "/admin/students", label: "Students", icon: IconStudents },
      { to: "/admin/sessions", label: "Sessions", icon: IconSessions },
      { to: "/admin/transcripts", label: "Transcripts", icon: IconTranscript },
      { to: "/admin/assessments", label: "Assessments", icon: IconAssessments },
      { to: "/admin/users", label: "User Accounts", icon: IconStudents },
    ],
  },
  {
    title: "System Administration",
    items: [
      { to: "/admin/system", label: "System Dashboard", icon: IconDashboard, end: true },
      { to: "/admin/system/traffic", label: "Traffic Dashboard", icon: IconSessions },
      { to: "/admin/system/load-testing", label: "Load & Capacity Testing", icon: IconSessions },
      { to: "/admin/system/usage", label: "AI Usage & Cost", icon: IconAssessments },
    ],
  },
];

export function AdminSidebar({ open = false, onClose }: { open?: boolean; onClose?: () => void }) {
  const { logout } = useAuth();

  return (
    <>
      {/* Backdrop only matters on mobile when the drawer is open. */}
      <div
        className={`pt-sidebar-backdrop ${open ? "show" : ""}`}
        onClick={onClose}
        aria-hidden="true"
      />
      <nav
        className={`pt-sidebar ${open ? "open" : ""}`}
        aria-label="Admin navigation"
        aria-hidden={undefined}
      >
      {SECTIONS.map((section) => (
        <div key={section.title}>
          <div className="pt-nav-section-label">{section.title}</div>
          {section.items
            .map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) => `pt-navlink ${isActive ? "active" : ""}`}
            >
              <Icon />
              <span>{label}</span>
            </NavLink>
          ))}
        </div>
      ))}

      <div>
        <div className="pt-nav-section-label">Account</div>
        <NavLink
          to="/student/dashboard"
          className={({ isActive }) => `pt-navlink ${isActive ? "active" : ""}`}
        >
          <IconDashboard />
          <span>Patient Simulator</span>
        </NavLink>
        <NavLink
          to="/admin/profile"
          className={({ isActive }) => `pt-navlink ${isActive ? "active" : ""}`}
        >
          <IconProfile />
          <span>Profile</span>
        </NavLink>
        <button
          type="button"
          className="pt-navlink"
          onClick={logout}
        >
          <IconLogout />
          <span>Logout</span>
        </button>
      </div>
      </nav>
    </>
  );
}
