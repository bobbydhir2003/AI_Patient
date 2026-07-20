import { useNavigate } from "react-router-dom";
import type { ComponentType, SVGProps } from "react";
import type { NeedsAttention } from "../../services/authApi";
import { IconAlert, IconClock, IconFileMissing, IconUsersAlert } from "./icons";

interface Row {
  key: keyof NeedsAttention;
  title: string;
  desc: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  to: string;
}

const ROWS: Row[] = [
  {
    key: "incompleteSessions",
    title: "Incomplete sessions",
    desc: "Sessions not yet completed by students",
    icon: IconClock,
    to: "/admin/sessions?status=active",
  },
  {
    key: "completedWithoutAssessment",
    title: "Completed without assessment",
    desc: "Sessions completed but not yet assessed",
    icon: IconFileMissing,
    to: "/admin/sessions?status=completed&assessment=none",
  },
  {
    key: "studentsMultipleIncomplete",
    title: "Students with multiple incomplete sessions",
    desc: "Students who have 2+ incomplete sessions",
    icon: IconUsersAlert,
    to: "/admin/students",
  },
  {
    key: "sessionsActiveOver24h",
    title: "Sessions active > 24 hours",
    desc: "Sessions in progress for more than 24 hours",
    icon: IconClock,
    to: "/admin/sessions?status=active&stale=1",
  },
];

export function NeedsAttentionPanel({ data }: { data: NeedsAttention }) {
  const navigate = useNavigate();
  return (
    <section className="pt-panel" aria-labelledby="na-title">
      <div className="pt-panel-head">
        <h2 className="pt-panel-title" id="na-title">
          <IconAlert width={18} height={18} style={{ color: "var(--color-accent)" }} />
          Needs Attention
        </h2>
        <button className="pt-panel-link" onClick={() => navigate("/admin/sessions")}>
          View All
        </button>
      </div>
      {ROWS.map((r) => {
        const count = data[r.key];
        const Icon = r.icon;
        return (
          <div className="pt-na-row" key={r.key}>
            <span className="pt-na-icon">
              <Icon width={17} height={17} />
            </span>
            <div className="pt-na-body">
              <div className="pt-na-title">{r.title}</div>
              <div className="pt-na-desc">{r.desc}</div>
            </div>
            <span className="pt-na-count" aria-label={`${count} items`}>
              {count}
            </span>
            <button
              className="pt-btn pt-btn-secondary pt-btn-sm"
              onClick={() => navigate(r.to)}
              aria-label={`Review ${r.title}`}
            >
              Review
            </button>
          </div>
        );
      })}
    </section>
  );
}
