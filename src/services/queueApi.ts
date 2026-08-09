/**
 * Interview waiting-queue API client. Every value is real backend queue state.
 * Leaving the queue only removes the waiting entry — it never affects sessions,
 * transcripts, assessments, or grading.
 */
import { API_BASE_URL, ApiError } from "./api";

async function queueRequest<T>(path: string, token: string | null, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api${path}`, { ...init, headers: { ...headers, ...(init?.headers as Record<string, string>) } });
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

export interface QueueState {
  admitted: boolean;
  entry_id: string | null;
  position: number | null;
  ahead?: number;
  total_waiting?: number;
  limit?: number;
  state: "admitted" | "waiting" | "expired";
  estimated_wait_minutes?: number | null;
}

export const joinQueue = (token: string | null, caseId: string) =>
  queueRequest<QueueState>("/queue/join", token, { method: "POST", body: JSON.stringify({ caseId }) });

export const queueStatus = (token: string | null, entryId: string) =>
  queueRequest<QueueState>(`/queue/status/${entryId}`, token);

export const leaveQueue = (token: string | null, entryId: string) =>
  queueRequest<{ left: boolean }>(`/queue/leave/${entryId}`, token, { method: "POST" });
