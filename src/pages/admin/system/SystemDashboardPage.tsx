import { ErrorState, LoadingState } from "../../../portal/ui";
import { IconRefresh } from "../../../components/admin/icons";
import { useSystemOverview } from "./useSystemOverview";
import {
  AiConfigurationSection,
  ApiCredentialsSection,
  PatientVoicesSection,
  QuickActionsSection,
  RecentActivitySection,
  SystemAlertsSection,
  SystemHealthOverview,
} from "./sections";

export function SystemDashboardPage() {
  const { data, loading, error, refresh, refreshing, lastChecked } = useSystemOverview();

  if (loading && !data) return <LoadingState label="Loading system status…" />;
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />;
  if (!data) return null;

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>System Dashboard</h1>
          <p className="pt-page-sub">
            Monitor system health, AI services, patient voices, and platform configuration.
          </p>
        </div>
        <div className="pt-header-actions">
          <span className="pt-muted" style={{ fontSize: "0.82rem" }}>
            Last checked:{" "}
            {lastChecked ? lastChecked.toLocaleTimeString(undefined, { timeStyle: "medium" }) : "never"}
          </span>
          <button
            type="button"
            className="pt-btn pt-btn-secondary pt-btn-sm"
            onClick={refresh}
            disabled={refreshing}
            aria-label="Refresh health checks"
          >
            <IconRefresh width={15} height={15} /> {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {error && (
        <p className="pt-error-text" role="alert" style={{ marginBottom: "var(--space-4)" }}>
          Some data may be stale: {error}
        </p>
      )}

      <SystemHealthOverview data={data} />

      <div className="pt-sys-grid">
        <div className="pt-sys-col">
          <PatientVoicesSection voices={data.voices} />
          <ApiCredentialsSection credentials={data.credentials} />
          <RecentActivitySection activity={data.activity} />
        </div>
        <div className="pt-sys-col">
          <AiConfigurationSection config={data.aiConfig} />
          <SystemAlertsSection alerts={data.alerts} />
          <QuickActionsSection overview={data} onRefresh={refresh} refreshing={refreshing} />
        </div>
      </div>
    </div>
  );
}
