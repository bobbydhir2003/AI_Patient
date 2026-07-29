import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../../state/AuthContext";
import { ApiError } from "../../../services/api";
import { fetchSystemOverview, type SystemOverview } from "../../../services/systemApi";

interface State {
  data: SystemOverview | null;
  loading: boolean;
  error: string | null;
  refreshing: boolean;
  lastChecked: Date | null;
  refresh: () => void;
}

/**
 * Loads the real System Overview and re-runs the backend health checks on
 * demand (Refresh button) and every 60s while mounted. Never continuously
 * calls paid external services - the overview endpoint only reports
 * configuration + local checks; live external tests are explicit-only.
 */
export function useSystemOverview(): State {
  const { token } = useAuth();
  const [data, setData] = useState<SystemOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);

  const load = useCallback(
    (isRefresh: boolean) => {
      if (!token) return;
      if (isRefresh) setRefreshing(true);
      else setLoading(true);
      setError(null);
      fetchSystemOverview(token)
        .then((d) => {
          setData(d);
          setLastChecked(new Date());
        })
        .catch((e) =>
          setError(e instanceof ApiError ? e.message : "Could not load system status."),
        )
        .finally(() => {
          setLoading(false);
          setRefreshing(false);
        });
    },
    [token],
  );

  useEffect(() => {
    load(false);
    const id = window.setInterval(() => load(true), 60_000);
    return () => window.clearInterval(id);
  }, [load]);

  return {
    data,
    loading,
    error,
    refreshing,
    lastChecked,
    refresh: () => load(true),
  };
}
