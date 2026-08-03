/**
 * Admin API client for the Traffic Dashboard (Priority B). Every value comes
 * from real backend telemetry; this module never fabricates data. A bearer
 * token is attached to every request (admin-only endpoints).
 */
import { API_BASE_URL, ApiError } from "./api";

async function trafficRequest<T>(path: string, token: string | null): Promise<T> {
  const url = `${API_BASE_URL}/api${path}`;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(url, { headers });
  } catch (networkError) {
    if (import.meta.env.DEV) console.error(`[trafficApi] network error ${url}:`, networkError);
    throw new ApiError("Could not reach the server.", 0, "network_error");
  }
  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    let code = "unknown_error";
    try {
      const body = (await response.json()) as { error?: { message?: string; code?: string } };
      if (body.error?.message) message = body.error.message;
      if (body.error?.code) code = body.error.code;
    } catch {
      /* keep defaults */
    }
    throw new ApiError(message, response.status, code);
  }
  return (await response.json()) as T;
}

// ------------------------------ types ------------------------------
export interface ProviderBlock {
  active: number;
  requests_per_minute: number;
  requests_last_5m: number;
  success_last_5m: number;
  failure_last_5m: number;
  errors_last_5m: number;
  rate_limits_last_5m: number;
  retries_last_5m: number;
  avg_ms: number | null;
  p95_ms: number | null;
  success_rate: number | null;
  model?: string;
}

export interface CapacityInfo {
  capacity_state: "NORMAL" | "BUSY" | "PROTECTING" | "CRITICAL";
  utilization_pct: number;
  tpm_used: number;
  tpm_limit: number;
  tpm_pct: number;
  rpm_used: number;
  rpm_limit: number;
  rpm_pct: number;
  headroom_tokens: number;
  tokens_5m: number;
}

export interface OperatorInsight {
  tone: "green" | "yellow" | "orange" | "red" | "info";
  message: string;
}

export interface TrafficOverview {
  timestamp: string;
  status: "healthy" | "elevated" | "critical";
  openai_capacity: CapacityInfo;
  insights: OperatorInsight[];
  users: { active: number };
  interviews: {
    active: number;
    waiting_for_ai: number;
    streaming: number;
    by_status: Record<string, number>;
  };
  http: {
    requests_per_minute: number;
    in_flight: number;
    error_rate: number;
    rate_limited_last_5m: number;
    p50_ms: number | null;
    p95_ms: number | null;
    p99_ms: number | null;
    avg_ms: number | null;
    requests_last_5m: number;
  };
  openai: ProviderBlock;
  elevenlabs: ProviderBlock;
  assessment: {
    pending: number;
    processing: number;
    oldest_wait_seconds: number | null;
    execution: string;
    in_flight: number;
    workers: number;
    effective_workers: number;
    throttle_mode: "NORMAL" | "REDUCED" | "MINIMAL" | "PAUSED";
  };
  concurrency: {
    interview: {
      active: number;
      limit: number;
      waiting: number;
      wait_p50_ms: number | null;
      wait_p95_ms: number | null;
      timeouts_5m: number;
    };
    tts: { active: number; limit: number };
  };
  server: {
    available: boolean;
    uptime_seconds: number;
    cpu_percent?: number;
    system_memory_percent?: number;
    process_memory_mb?: number;
    note?: string;
    db_pool: {
      applicable: boolean;
      kind?: string;
      size?: number;
      checked_out?: number;
      overflow?: number;
      max_overflow?: number | null;
      utilization?: number | null;
      note?: string;
    };
  };
  alerts: TrafficAlert[];
}

export interface TrafficAlert {
  severity: "INFO" | "WARNING" | "CRITICAL";
  key: string;
  message: string;
  ts: string;
}

export interface LiveSession {
  session_id: string;
  student_name: string;
  student_number: string;
  case_id: string;
  case_name: string;
  status: string;
  started_at: string;
  last_activity_at: string;
  duration_seconds: number;
  latest_latency_ms: number | null;
}

export interface HistoryPoint {
  t: string;
  active_users: number;
  http_rpm: number;
  openai_rpm: number;
  elevenlabs_rpm: number;
  rate_limited: number;
}

export interface TrafficCapacity {
  deployment_mode: string;
  app_workers: number;
  max_ai_interview_concurrency: number;
  max_tts_concurrency: number;
  assessment_workers: number;
  rate_limiter_scope: string;
  notes: { global_rate_limiting: string; autoscaling: string };
  protection: {
    source: string;
    editable: boolean;
    restart_required_to_change: boolean;
    rate_limiting: {
      enabled: boolean;
      scope: string;
      login: string;
      interview: string;
      voice: string;
      assessment: string;
    };
    login_throttle: { enabled: boolean; max_failed_attempts: number; lockout_seconds: number };
    interview_concurrency: { active: number; limit: number };
    tts_concurrency: { active: number; limit: number };
    assessment_execution: string;
    retry_backoff: { enabled: boolean; max_retries: number };
    circuit_breaker: string;
  };
}

// ------------------------------ fetchers ------------------------------
export const fetchTrafficOverview = (token: string | null) =>
  trafficRequest<TrafficOverview>("/admin/system/traffic/overview", token);

export const fetchLiveSessions = (token: string | null) =>
  trafficRequest<{ count: number; sessions: LiveSession[] }>(
    "/admin/system/traffic/live-sessions",
    token,
  );

export const fetchTrafficHistory = (token: string | null, minutes: number) =>
  trafficRequest<{ minutes: number; points: HistoryPoint[] }>(
    `/admin/system/traffic/history?minutes=${minutes}`,
    token,
  );

export const fetchTrafficCapacity = (token: string | null) =>
  trafficRequest<TrafficCapacity>("/admin/system/traffic/capacity", token);
