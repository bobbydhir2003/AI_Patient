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
export interface AudioQueueHealth {
  available: boolean;
  status: string;
  pending: number | null;
  processing: number | null;
  failed: number | null;
  message: string;
  checkedAt: string;
}
export interface StorageHealth {
  status: string;
  usedBytes: number | null;
  totalBytes: number | null;
  freeBytes: number | null;
  percentUsed: number | null;
  audioCacheEntries: number | null;
  audioCacheMaxEntries: number | null;
  audioCacheBytes: number | null;
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
export interface ElevenLabsConfig {
  configured: boolean;
  enabled: boolean;
  model: string;
  outputFormat: string;
  timeoutSeconds: number | null;
  status: string;
}
export interface ConversationSettings {
  sentenceLevelStreaming: string;
  patientStreaming: string;
  disclosureControl: string;
  motivationalInterviewing: string;
  ageAppropriateLanguage: string;
  caregiverRouting: string;
  maxPatientResponseChars: number;
}
export interface AiConfiguration {
  openai: OpenAIConfig;
  elevenlabs: ElevenLabsConfig;
  conversation: ConversationSettings;
}
export interface VoiceRow {
  caseId: string;
  speakerId: string;
  patientName: string;
  speakerLabel: string;
  image: string;
  voiceName: string | null;
  maskedVoiceId: string | null;
  model: string | null;
  status: string;
  reason: string;
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
  ttsInFlight: number | null;
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
  tts: ConcurrencyLane;
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
  elevenlabs: ServiceHealth;
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
  elevenlabs: ServiceHealth;
  audioQueue: AudioQueueHealth;
  storage: StorageHealth;
  aiConfig: AiConfiguration;
  credentials: CredentialStatus[];
  voices: VoiceRow[];
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

export function fetchSystemVoices(token: string): Promise<{ voices: VoiceRow[] }> {
  return systemRequest<{ voices: VoiceRow[] }>("/admin/system/voices", token);
}

export function clearAudioCache(token: string): Promise<{ success: boolean; message: string }> {
  return systemRequest("/admin/system/audio-cache/clear", token, { method: "POST" });
}

/** Fetch a real voice-preview audio clip as a playable blob URL. Throws ApiError
 *  (with the backend's message) when the voice is not available/configured. */
export async function fetchVoicePreview(token: string, caseId: string): Promise<string> {
  const url = `${API_BASE_URL}/api/admin/system/voices/${encodeURIComponent(caseId)}/preview`;
  const response = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    let message = `Preview failed (status ${response.status})`;
    try {
      const body = (await response.json()) as { error?: { message?: string } };
      if (body.error?.message) message = body.error.message;
    } catch {
      /* non-JSON */
    }
    throw new ApiError(message, response.status, "preview_failed");
  }
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}
