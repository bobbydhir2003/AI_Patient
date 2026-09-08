/**
 * Editable runtime-configuration API client. Every write goes to the backend,
 * which validates + persists it. No value here is ever fabricated on the client,
 * and full API keys are never sent back from the server.
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
    if (import.meta.env.DEV) console.error(`[runtimeApi] network error ${url}`, e);
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
export interface AiConfig {
  openai: {
    configured: boolean;
    model: string;
    streamingEnabled: boolean;
    timeoutSeconds: number | null;
    maxOutputTokens: number | null;
    status: string;
    modelAllowlist: string[];
  };
}

export interface RuntimeCredential {
  service: string;
  configured: boolean;
  source: string; // "database" | "environment" | "none"
  maskedValue: string | null;
  lastTestStatus: string;
  lastTestMessage: string;
  lastTestedAt: string | null;
  updatedAt: string | null;
  updatedBy: string | null;
  status: string;
  secureStorageAvailable: boolean;
}

export interface ApplyResult {
  success: boolean;
  applyMode: string;
  message: string;
}
export interface TestResult {
  service: string;
  status: string;
  message: string;
}
export interface HistoryItem {
  id: string;
  type: string;
  key: string;
  entityId: string;
  previousValue: string;
  newValue: string;
  changedBy: string;
  changedAt: string;
}

// ------------------------------ AI configuration ------------------------------
export const getAiConfig = (t: string) => req<AiConfig>("/admin/runtime/ai-configuration", t);
export const patchOpenAI = (t: string, body: Record<string, unknown>) =>
  req<ApplyResult>("/admin/runtime/ai-configuration/openai", t, { method: "PATCH", body: JSON.stringify(body) });

// ------------------------------ credentials ------------------------------
export const getRuntimeCredentials = (t: string) =>
  req<{ credentials: RuntimeCredential[] }>("/admin/runtime/credentials", t);
export const replaceCredential = (t: string, service: string, key: string) =>
  req<ApplyResult>(`/admin/runtime/credentials/${service}`, t, { method: "POST", body: JSON.stringify({ key }) });
export const testCredential = (t: string, service: string) =>
  req<TestResult>(`/admin/runtime/credentials/${service}/test`, t, { method: "POST" });
export const removeCredential = (t: string, service: string) =>
  req<ApplyResult>(`/admin/runtime/credentials/${service}`, t, { method: "DELETE" });

// ------------------------------ history ------------------------------
export const getHistory = (t: string) => req<{ history: HistoryItem[] }>("/admin/runtime/history", t);
