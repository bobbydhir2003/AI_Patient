/**
 * API client for the technical System Dashboard. Every value returned here is
 * produced by real backend checks; this module never fabricates data. A bearer
 * token is attached to every request (admin-only endpoints).
 */
import { API_BASE_URL, ApiError } from "./api";

async function systemRequest<T>(
  path: string,
  token: string | null,
  init?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}/api${path}`;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(url, { ...init, headers: { ...headers, ...(init?.headers ?? {}) } });
  } catch (networkError) {
    if (import.meta.env.DEV) console.error(`[systemApi] network error ${url}:`, networkError);
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

// -------------------------------- types --------------------------------
export interface BackendHealth {
  status: string;
  responseTimeMs: number | null;
  version: string;
  environment: string;
  checkedAt: string;
}
export interface DatabaseHealth {
  status: string;
  dbType: string;
  latencyMs: number | null;
  migrationVersion: string | null;
  checkedAt: string;
}
export interface ServiceHealth {
  service: string;
  configured: boolean;
  status: string;
  model: string;
  streamingEnabled: boolean | null;
  lastSuccessAt: string | null;
  lastError: string | null;
  checkedAt: string;
}
export interface StorageHealth {
  status: string;
  usedBytes: number | null;
  totalBytes: number | null;
  freeBytes: number | null;
  percentUsed: number | null;
  checkedAt: string;
}
export interface OpenAIConfig {
  configured: boolean;
  model: string;
  streamingEnabled: boolean;
  timeoutSeconds: number | null;
  maxOutputTokens: number | null;
  status: string;
}
export interface AiConfiguration {
  openai: OpenAIConfig;
}
export interface CredentialStatus {
  service: string;
  configured: boolean;
  maskedValue: string | null;
  updatedAt: string | null;
  updatedBy: string | null;
  status: string;
}
export interface SystemAlert {
  id: string;
  severity: string;
  service: string;
  message: string;
  detectedAt: string;
  state: string;
  count: number;
}
export interface SystemActivity {
  id: string;
  admin: string;
  action: string;
  target: string;
  result: string;
  timestamp: string;
}
export interface RedisHealth {
  status: string; // connected | unavailable | not_configured
  required: boolean;
  latencyMs: number | null;
  checkedAt: string;
}
export interface WorkerRow {
  workerId: string;
  pid: number | null;
  hostname: string;
  health: string; // healthy | stale | unavailable
  uptimeSeconds: number | null;
  heartbeatAt: string | null;
  heartbeatAgeSeconds: number | null;
  requestsTotal: number | null;
  requestsPerMinute: number | null;
  httpInFlight: number | null;
  interviewInFlight: number | null;
  assessmentInFlight: number | null;
  memoryMb: number | null;
  currentTask: string | null;
}
export interface WorkerFleet {
  monitoring: string; // observed | local_only | unavailable
  status: string; // healthy | degraded | unavailable | local_only
  mode: string;
  configured: number;
  observed: number | null;
  healthy: number | null;
  heartbeatIntervalSeconds: number | null;
  heartbeatTtlSeconds: number | null;
  note: string;
  workers: WorkerRow[];
}
export interface ConcurrencyLane {
  name: string;
  active: number;
  limit: number;
  scope: string; // global | process
  waiting: number | null;
  queued: number | null;
}
export interface Concurrency {
  scope: string;
  redis: RedisHealth;
  openai: ConcurrencyLane;
  assessment: ConcurrencyLane;
}
export interface InfraCheck {
  key: string;
  label: string;
  status: string; // healthy | degraded | unavailable | misconfigured | not_configured
  detail: string;
}

/** Lean, fast-polling live payload (GET /admin/system/live). */
export interface SystemLive {
  generatedAt: string;
  backend: BackendHealth;
  database: DatabaseHealth;
  redis: RedisHealth;
  openai: ServiceHealth;
  workers: WorkerFleet;
  concurrency: Concurrency;
  checks: InfraCheck[];
  alerts: SystemAlert[];
}

export interface SystemOverview {
  generatedAt: string;
  backend: BackendHealth;
  database: DatabaseHealth;
  redis: RedisHealth;
  openai: ServiceHealth;
  storage: StorageHealth;
  aiConfig: AiConfiguration;
  credentials: CredentialStatus[];
  alerts: SystemAlert[];
  activity: SystemActivity[];
  workers: WorkerFleet;
  concurrency: Concurrency;
  checks: InfraCheck[];
}

// -------------------------------- calls --------------------------------
export function fetchSystemOverview(token: string): Promise<SystemOverview> {
  return systemRequest<SystemOverview>("/admin/system/overview", token);
}

/** Fast live snapshot for the auto-refresh loop (real runtime values only). */
export function fetchSystemLive(token: string): Promise<SystemLive> {
  return systemRequest<SystemLive>("/admin/system/live", token);
}

