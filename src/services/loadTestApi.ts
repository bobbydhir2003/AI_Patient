/**
 * Load & Capacity Testing API client (Priority J). Super-admin only on the
 * backend. Every metric returned here is a REAL runtime measurement; the client
 * never fabricates values. Missing measurements arrive as null and the UI shows
 * "Not available".
 */
import { API_BASE_URL, ApiError } from "./api";

async function req<T>(path: string, token: string | null, init?: RequestInit): Promise<T> {
  const url = `${API_BASE_URL}/api${path}`;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let res: Response;
  try {
    res = await fetch(url, { ...init, headers: { ...headers, ...(init?.headers ?? {}) } });
  } catch (e) {
    if (import.meta.env.DEV) console.error(`[loadTestApi] network error ${url}`, e);
    throw new ApiError("Could not reach the server.", 0, "network_error");
  }
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    let code = "unknown_error";
    try {
      const b = (await res.json()) as { error?: { message?: string; code?: string } };
      if (b.error?.message) message = b.error.message;
      if (b.error?.code) code = b.error.code;
    } catch {
      /* keep default */
    }
    throw new ApiError(message, res.status, code);
  }
  return (await res.json()) as T;
}

// ------------------------------ types ------------------------------
export interface LoadTestConfig {
  enabled: boolean;
  environment: string;
  awsReady: boolean;
  maxUsers: number;
  maxDurationSeconds: number;
  targetBaseUrl: string;
  testTypes: string[];
  providerModes: string[];
}

export interface LoadTestJob {
  id: string;
  createdBy: string;
  environment: string;
  testType: string;
  providerMode: string;
  targetUsers: number;
  rampSeconds: number;
  durationSeconds: number;
  status: string;
  workerIdentifier: string | null;
  errorMessage: string | null;
  createdAt: string | null;
  startedAt: string | null;
  endedAt: string | null;
}

export interface CapacitySummary {
  totalRequests: number;
  successRatePct: number | null;
  p95Ms: number | null;
  p99Ms: number | null;
  peakConcurrency: number;
  rateLimited429: number;
  overloaded503: number;
  serverErrors5xx: number;
  networkErrors: number;
}

export interface CapacityAnalysis {
  overallStatus: "PASS" | "PASS_WITH_WARNING" | "FAIL" | "INCONCLUSIVE";
  reason: string;
  observedBottleneck: { observed: boolean; kind?: string; detail: string };
  recommendedSafeCapacity: { value: number | null; basis: string; reason: string };
  summary: CapacitySummary;
  criteria: Record<string, number>;
  notes: string[];
}

export interface MetricsSample {
  t?: number;
  activeUsers: number | null;
  windowRequests: number;
  requestsPerSec: number;
  windowSuccess: number;
  windowFailed: number;
  successRate: number | null;
  p50: number | null;
  p95: number | null;
  p99: number | null;
}

export interface ProviderBlock {
  name?: string;
  requests_per_minute?: number;
  success_rate?: number | null;
  in_flight?: number;
  tokens?: Record<string, unknown>;
  [k: string]: unknown;
}

export interface MetricsResponse {
  job: LoadTestJob;
  live: boolean;
  elapsedSeconds?: number | null;
  overall: {
    requests: number;
    success: number;
    failed: number;
    networkErrors: number;
    successRate: number | null;
    statusCounts: Record<string, number>;
    maxActiveUsers: number;
    latencyMs: { p50: number | null; p95: number | null; p99: number | null };
    turnLatencyMs: { p50: number | null; p95: number | null; p99: number | null };
  } | null;
  series: MetricsSample[];
  capacity: CapacityAnalysis | null;
  telemetry: {
    providers: { openai: ProviderBlock; elevenlabs: ProviderBlock };
    infrastructure: {
      server: Record<string, unknown>;
      dbPool: Record<string, unknown>;
      assessmentQueue: Record<string, unknown>;
      httpInFlight: number;
    };
  };
}

export interface CreateJobInput {
  testType: string;
  providerMode: string;
  targetUsers: number;
  rampSeconds: number;
  durationSeconds: number;
  confirmRealProvider?: boolean;
}

// ------------------------------ calls ------------------------------
export const getLoadTestConfig = (token: string | null) =>
  req<LoadTestConfig>("/admin/system/load-tests/config", token);

export const getRecentJobs = (token: string | null) =>
  req<{ jobs: LoadTestJob[] }>("/admin/system/load-tests/recent", token);

export const getActiveJob = (token: string | null) =>
  req<{ job: LoadTestJob | null }>("/admin/system/load-tests/active", token);

export const createJob = (token: string | null, input: CreateJobInput) =>
  req<LoadTestJob>("/admin/system/load-tests", token, {
    method: "POST",
    body: JSON.stringify(input),
  });

export const getJobMetrics = (token: string | null, jobId: string) =>
  req<MetricsResponse>(`/admin/system/load-tests/${jobId}/metrics`, token);

export const stopJob = (token: string | null, jobId: string) =>
  req<LoadTestJob>(`/admin/system/load-tests/${jobId}/stop`, token, { method: "POST" });
