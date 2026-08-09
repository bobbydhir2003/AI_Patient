import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "../../../state/AuthContext";
import { ApiError } from "../../../services/api";
import {
  fetchSystemLive,
  fetchSystemOverview,
  type SystemOverview,
} from "../../../services/systemApi";

interface State {
  data: SystemOverview | null;
  loading: boolean;
  error: string | null;
  refreshing: boolean;
  lastChecked: Date | null;
  live: boolean; // last auto-refresh poll succeeded (true) vs stale (false)
  refresh: () => void;
}

const POLL_MS = 4000; // 3-5s per requirements

/**
 * Loads the full System Overview once (heavy config sections) and then keeps
 * the LIVE sections (backend/db/redis health, observed worker fleet, global
 * concurrency, infra checks, alerts) fresh by polling the lean /live endpoint
 * every ~4s. On a failed poll we KEEP the last known state and flip `live` to
 * false so the UI can warn the data is stale - we never silently present old
 * data as live. Never continuously calls paid external services.
 */
export function useSystemOverview(): State {
  const { token } = useAuth();
  const [data, setData] = useState<SystemOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [live, setLive] = useState(true);
  const mounted = useRef(true);

  const loadFull = useCallback(
    (isRefresh: boolean) => {
      if (!token) return;
      if (isRefresh) setRefreshing(true);
      else setLoading(true);
      setError(null);
      fetchSystemOverview(token)
        .then((d) => {
          if (!mounted.current) return;
          setData(d);
          setLastChecked(new Date());
          setLive(true);
        })
        .catch((e) => {
          if (!mounted.current) return;
          setError(e instanceof ApiError ? e.message : "Could not load system status.");
          setLive(false);
        })
        .finally(() => {
          if (!mounted.current) return;
          setLoading(false);
          setRefreshing(false);
        });
    },
    [token],
  );

  // Lean poll: merge only the live subset into the existing overview.
  const poll = useCallback(() => {
    if (!token) return;
    fetchSystemLive(token)
      .then((d) => {
        if (!mounted.current) return;
        setData((prev) =>
          prev
            ? {
                ...prev,
                generatedAt: d.generatedAt,
                backend: d.backend,
                database: d.database,
                redis: d.redis,
                openai: d.openai,
                elevenlabs: d.elevenlabs,
                workers: d.workers,
                concurrency: d.concurrency,
                checks: d.checks,
                alerts: d.alerts,
              }
            : prev,
        );
        setLastChecked(new Date());
        setLive(true);
        setError(null);
      })
      .catch((e) => {
        if (!mounted.current) return;
        // Keep last-known state; surface a stale warning instead of blanking.
        setLive(false);
        setError(e instanceof ApiError ? e.message : "Live refresh failed; showing last known state.");
      });
  }, [token]);

  useEffect(() => {
    mounted.current = true;
    loadFull(false);
    const id = window.setInterval(poll, POLL_MS);
    return () => {
      mounted.current = false;
      window.clearInterval(id);
    };
  }, [loadFull, poll]);

  return {
    data,
    loading,
    error,
    refreshing,
    lastChecked,
    live,
    refresh: () => loadFull(true),
  };
}
