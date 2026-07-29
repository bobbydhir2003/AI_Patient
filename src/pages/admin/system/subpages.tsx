import { ErrorState, LoadingState } from "../../../portal/ui";
import { useSystemOverview } from "./useSystemOverview";
import { SystemHealthOverview } from "./sections";

/** Focused System Health page (live checks). Editing pages live in their own
 * files (AiConfigurationPage, PatientVoicesPage, ApiCredentialsPage). */
export function SystemHealthPage() {
  const { data, loading, error, refresh } = useSystemOverview();
  if (loading && !data) return <LoadingState label="Loading system health…" />;
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />;
  if (!data) return null;
  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>System Health</h1>
          <p className="pt-page-sub">Live backend, database, AI service, audio, and storage checks.</p>
        </div>
      </div>
      <SystemHealthOverview data={data} />
    </div>
  );
}
