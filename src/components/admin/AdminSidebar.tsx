import { NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import {
  IconArchive,
  IconAssessments,
  IconAudit,
  IconBolt,
  IconDashboard,
  IconExport,
  IconImport,
  IconLogout,
  IconPlus,
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
    title: "Overview",
    items: [{ to: "/admin", label: "Dashboard", icon: IconDashboard, end: true }],
  },
  {
    title: "Student data",
    items: [
      { to: "/admin/students", label: "Students", icon: IconStudents },
      { to: "/admin/sessions", label: "Sessions", icon: IconSessions },
      { to: "/admin/transcripts", label: "Transcripts", icon: IconTranscript },
      { to: "/admin/assessments", label: "Assessments", icon: IconAssessments },
    ],
  },
  {
    title: "Management",
    items: [
      { to: "/admin/archived", label: "Archived", icon: IconArchive },
      { to: "/admin/audit-log", label: "Admin Activity Log", icon: IconAudit, end: true },
    ],
  },
];

// Quick actions with no safe backend action yet are surfaced as disabled.
const QUICK_ACTIONS: { label: string; icon: Icon }[] = [
  { label: "Add Student", icon: IconPlus },
  { label: "Import Students", icon: IconImport },
  { label: "Export Reports", icon: IconExport },
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

      <div className="pt-quick-actions">
        <div className="pt-quick-actions-title">
          <IconBolt width={15} height={15} />
          Quick Actions
        </div>
        {QUICK_ACTIONS.map(({ label, icon: Icon }) => (
          <button
            key={label}
            type="button"
            className="pt-navlink disabled"
            disabled
            aria-disabled="true"
            title="Coming soon"
          >
            <Icon />
            <span>{label}</span>
            <span className="pt-coming-soon">Soon</span>
          </button>
        ))}
      </div>
    </nav>
  );
}
