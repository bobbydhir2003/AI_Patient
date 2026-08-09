import { ErrorState, LoadingState } from "../../../portal/ui";
import { IconRefresh } from "../../../components/admin/icons";
import { useSystemOverview } from "./useSystemOverview";
import {
  AiConfigurationSection,
  ApiCredentialsSection,
  GlobalConcurrencySection,
  PatientVoicesSection,
  QuickActionsSection,
  RealtimeChecksSection,
  RecentActivitySection,
  SystemAlertsSection,
  SystemHealthOverview,
  WorkerArchitectureSection,
} from "./sections";

export function SystemDashboardPage() {
  const { data, loading, error, refresh, refreshing, lastChecked, live } = useSystemOverview();

  if (loading && !data) return <LoadingState label="Loading system status…" />;
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />;
  if (!data) return null;

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>System Dashboard</h1>
          <p className="pt-page-sub">
            Real-time system monitoring, backend operations, and AI service health.
          </p>
        </div>
        <div className="pt-header-actions">
          <span className={`pt-live-dot ${live ? "pt-live-on" : "pt-live-off"}`} aria-hidden="true" />
          <span className="pt-muted" style={{ fontSize: "0.82rem" }}>
            {live ? "Live" : "Stale"} · updated{" "}
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

      {!live && (
        <p className="pt-error-text" role="alert" style={{ marginBottom: "var(--space-4)" }}>
          Auto-refresh failed — showing the last known state (not live). {error}
        </p>
      )}

      <SystemHealthOverview data={data} />

      <WorkerArchitectureSection
        fleet={data.workers}
        concurrency={data.concurrency}
        database={data.database}
      />

      <div className="pt-sys-grid">
        <div className="pt-sys-col">
          <GlobalConcurrencySection concurrency={data.concurrency} />
          <PatientVoicesSection voices={data.voices} />
          <ApiCredentialsSection credentials={data.credentials} />
          <RecentActivitySection activity={data.activity} />
        </div>
        <div className="pt-sys-col">
          <RealtimeChecksSection checks={data.checks} />
          <AiConfigurationSection config={data.aiConfig} />
          <SystemAlertsSection alerts={data.alerts} />
          <QuickActionsSection overview={data} onRefresh={refresh} refreshing={refreshing} />
        </div>
      </div>
    </div>
  );
}
