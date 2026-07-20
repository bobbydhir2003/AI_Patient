import { useNavigate } from "react-router-dom";
import type { RecentStudentItem } from "../../services/authApi";
import { EmptyState } from "../../portal/ui";
import { fmtDateTime } from "../../portal/format";
import { IconChevronRight } from "./icons";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  return (parts[0][0] + (parts[1]?.[0] ?? "")).toUpperCase();
}

export function RecentStudentsPanel({ students }: { students: RecentStudentItem[] }) {
  const navigate = useNavigate();
  return (
    <section className="pt-panel" aria-labelledby="recent-students-title">
      <div className="pt-panel-head">
        <h2 className="pt-panel-title" id="recent-students-title">
          Recent Students
        </h2>
        <button className="pt-panel-link" onClick={() => navigate("/admin/students")}>
          View All
        </button>
      </div>

      {students.length === 0 ? (
        <EmptyState title="No students yet" hint="Recently active students will appear here." />
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {students.map((s) => (
            <li key={s.id}>
              <div
                className="pt-rs-item"
                role="button"
                tabIndex={0}
                onClick={() => navigate(`/admin/students/${s.id}`)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    navigate(`/admin/students/${s.id}`);
                  }
                }}
                aria-label={`Open ${s.name}`}
              >
                <span className="pt-avatar" style={{ background: "#4a4a4a" }} aria-hidden="true">
                  {initials(s.name)}
                </span>
                <div className="pt-rs-body">
                  <div className="pt-rs-name">
                    {s.name}
                    {s.studentNumber ? (
                      <span className="pt-rs-meta"> · ID: {s.studentNumber}</span>
                    ) : null}
                  </div>
                  <div className="pt-rs-meta">
                    {s.sessionCount} session{s.sessionCount === 1 ? "" : "s"} · {s.assessmentCount}{" "}
                    assessment{s.assessmentCount === 1 ? "" : "s"}
                  </div>
                  <div className="pt-rs-meta">Last active: {fmtDateTime(s.lastActivityAt)}</div>
                </div>
                <IconChevronRight className="pt-rs-chevron" width={18} height={18} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
