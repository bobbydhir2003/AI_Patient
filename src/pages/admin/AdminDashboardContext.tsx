import {
  createContext,
  useContext,
  useCallback,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { useAuth } from "../../state/AuthContext";
import { fetchDashboard, type AdminDashboard } from "../../services/authApi";
import { ApiError } from "../../services/api";

interface AdminDashboardCtx {
  data: AdminDashboard | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
  /** Count of Needs-Attention categories that currently have items. */
  alertCount: number;
}

const Ctx = createContext<AdminDashboardCtx | undefined>(undefined);

export function AdminDashboardProvider({ children }: { children: ReactNode }) {
  const { token } = useAuth();
  const [data, setData] = useState<AdminDashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(() => {
    if (!token) return;
    setError(null);
    setLoading(true);
    fetchDashboard(token)
      .then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load the dashboard."))
      .finally(() => setLoading(false));
  }, [token]);

  useEffect(reload, [reload]);

  const na = data?.needsAttention;
  const alertCount = na
    ? [
        na.incompleteSessions,
        na.completedWithoutAssessment,
        na.studentsMultipleIncomplete,
        na.sessionsActiveOver24h,
      ].filter((n) => n > 0).length
    : 0;

  return (
    <Ctx.Provider value={{ data, error, loading, reload, alertCount }}>{children}</Ctx.Provider>
  );
}

export function useAdminDashboard(): AdminDashboardCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAdminDashboard must be used within AdminDashboardProvider");
  return ctx;
}
