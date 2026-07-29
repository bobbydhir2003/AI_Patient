import { NavLink, useNavigate } from "react-router-dom";
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
    ],
  },
];

export function AdminSidebar() {
  const { logout } = useAuth();
  const navigate = useNavigate();

  return (
    <nav className="pt-sidebar" aria-label="Admin navigation">
      {SECTIONS.map((section) => (
        <div key={section.title}>
          <div className="pt-nav-section-label">{section.title}</div>
          {section.items.map(({ to, label, icon: Icon, end }) => (
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
          to="/admin/profile"
          className={({ isActive }) => `pt-navlink ${isActive ? "active" : ""}`}
        >
          <IconProfile />
          <span>Profile</span>
        </NavLink>
        <button
          type="button"
          className="pt-navlink"
          onClick={() => {
            logout();
            navigate("/login");
          }}
        >
          <IconLogout />
          <span>Logout</span>
        </button>
      </div>
    </nav>
  );
}
