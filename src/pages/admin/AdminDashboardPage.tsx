import { useNavigate } from "react-router-dom";
import { ErrorState, LoadingState } from "../../portal/ui";
import { useAdminDashboard } from "./AdminDashboardContext";
import { DashboardMetricCard } from "../../components/admin/DashboardMetricCard";
import { NeedsAttentionPanel } from "../../components/admin/NeedsAttentionPanel";
import { AssessmentSummary } from "../../components/admin/AssessmentSummary";
import { RecentSessionsTable } from "../../components/admin/RecentSessionsTable";
import { RecentStudentsPanel } from "../../components/admin/RecentStudentsPanel";
import {
  IconArchive,
  IconAssessments,
  IconAlert,
  IconCheckCircle,
  IconReport,
  IconSessions,
  IconStudents,
} from "../../components/admin/icons";

export function AdminDashboardPage() {
  const navigate = useNavigate();
  const { data, error, loading, reload } = useAdminDashboard();

  if (error) return <ErrorState message={error} onRetry={reload} />;
  if (loading || !data) return <LoadingState label="Loading dashboard…" />;

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">Dashboard</h1>
          <p className="pt-page-sub">
            Monitor student interviews, sessions, transcripts, and assessments.
          </p>
        </div>
        <div className="pt-header-actions">
          <select
            className="pt-select"
            aria-label="Date range (all-time data shown)"
            title="Date filtering coming soon — showing all-time data"
            defaultValue="all"
            disabled
          >
            <option value="all">All time</option>
            <option value="7">Last 7 days</option>
            <option value="30">Last 30 days</option>
          </select>
          <button className="pt-btn" onClick={() => navigate("/admin/assessments")}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              <IconReport width={16} height={16} /> View All Reports
            </span>
          </button>
        </div>
      </div>

      <div className="pt-metrics">
        <DashboardMetricCard
          icon={IconStudents}
          color="red"
          count={data.totalStudents}
          label="Students"
          actionLabel="View all students"
          to="/admin/students"
        />
        <DashboardMetricCard
          icon={IconSessions}
          color="blue"
          count={data.totalSessions}
          label="Sessions"
          actionLabel="View all sessions"
          to="/admin/sessions"
        />
        <DashboardMetricCard
          icon={IconCheckCircle}
          color="green"
          count={data.completedSessions}
          label="Completed"
          actionLabel="View completed"
          to="/admin/sessions?status=completed"
        />
        <DashboardMetricCard
          icon={IconAlert}
          color="orange"
          count={data.incompleteSessions}
          label="Needs Attention"
          actionLabel="Review sessions"
          to="/admin/sessions?status=active"
        />
        <DashboardMetricCard
          icon={IconAssessments}
          color="purple"
          count={data.totalAssessments}
          label="Assessments"
          actionLabel="View assessments"
          to="/admin/assessments"
        />
        <DashboardMetricCard
          icon={IconArchive}
          color="gray"
          count={data.archivedSessions}
          label="Archived"
          actionLabel="View archived"
          to="/admin/archived"
        />
      </div>

      <div className="pt-dash-grid">
        {data.needsAttention && <NeedsAttentionPanel data={data.needsAttention} />}
        <AssessmentSummary levels={data.assessmentLevels} />
      </div>

      <div className="pt-dash-grid-2">
        <RecentSessionsTable sessions={data.recentSessions} onChanged={reload} />
        <RecentStudentsPanel students={data.recentStudents} />
      </div>
    </div>
  );
}
