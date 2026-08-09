/**
 * Admin API client for the AI Usage & Cost dashboard. Every value comes from
 * real recorded usage events (backend aggregation); this module never fabricates
 * data. A bearer token is attached to every request (admin-only endpoints).
 */
import { API_BASE_URL, ApiError } from "./api";

async function usageRequest<T>(path: string, token: string | null): Promise<T> {
  const url = `${API_BASE_URL}/api${path}`;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(url, { headers });
  } catch {
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

export type UsageRange = "live" | "5m" | "15m" | "1h" | "6h" | "24h" | "7d" | "30d" | "today" | "custom";

export interface ProviderSplit {
  openai_usd: number;
  elevenlabs_usd: number;
  openai_pct: number;
  elevenlabs_pct: number;
}

export interface ProjectedMonthly {
  available: boolean;
  message: string;
  projected_usd: number | null;
  avg_daily_usd?: number;
  days_in_month?: number;
}

export interface ProviderHealthBlock {
  configured: boolean;
  last_event_at: string | null;
}

export interface UsageSummary {
  range: string;
  start: string;
  end: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  elevenlabs_characters: number;
  openai_cost_usd: number;
  elevenlabs_cost_usd: number;
  total_cost_usd: number;
  session_count: number;
  avg_tokens_per_interview: number;
  avg_input_tokens_per_interview: number;
  avg_output_tokens_per_interview: number;
  avg_elevenlabs_chars_per_interview: number;
  avg_cost_per_interview_usd: number;
  total_cost_today_usd: number;
  total_cost_month_to_date_usd: number;
  provider_split: ProviderSplit;
  projected_monthly: ProjectedMonthly;
  pricing: {
    version: string;
    openai: { default_input_per_1k: number; default_output_per_1k: number; models: string[] };
    elevenlabs: { per_character: number };
  };
  providers: { openai: ProviderHealthBlock; elevenlabs: ProviderHealthBlock };
}

export interface TimeseriesPoint {
  ts: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}
export interface UsageTimeseries {
  range: string;
  bucket_seconds: number;
  points: TimeseriesPoint[];
}

export interface UsageSessionRow {
  session_id: string;
  student_name: string;
  case_id: string;
  status: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  elevenlabs_characters: number;
  openai_requests: number;
  tts_requests: number;
  openai_cost_usd: number;
  elevenlabs_cost_usd: number;
  total_cost_usd: number;
  last_updated: string | null;
  started_at: string | null;
  completed_at: string | null;
}
export interface UsageSessions {
  range: string;
  total: number;
  sessions: UsageSessionRow[];
}

function qs(range: UsageRange, extra: Record<string, string | number | undefined> = {}): string {
  const p = new URLSearchParams({ range });
  for (const [k, v] of Object.entries(extra)) if (v !== undefined) p.set(k, String(v));
  return `?${p.toString()}`;
}

export const fetchUsageSummary = (token: string | null, range: UsageRange) =>
  usageRequest<UsageSummary>(`/admin/usage/summary${qs(range)}`, token);

export const fetchUsageTimeseries = (token: string | null, range: UsageRange) =>
  usageRequest<UsageTimeseries>(`/admin/usage/timeseries${qs(range)}`, token);

export const fetchUsageSessions = (token: string | null, range: UsageRange, limit = 10) =>
  usageRequest<UsageSessions>(`/admin/usage/sessions${qs(range, { limit })}`, token);
