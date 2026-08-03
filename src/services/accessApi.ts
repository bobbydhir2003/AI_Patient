/**
 * Email access-request API client.
 * - Public: submit an access request (no auth).
 * - Admin: list / approve / reject (bearer token).
 * There is intentionally no "status by email" lookup (no enumeration).
 */
import { API_BASE_URL, ApiError } from "./api";

export interface AccessRequestResult {
  result: "PENDING" | "ALREADY_PENDING" | "ALREADY_APPROVED";
  message: string;
}

export interface AccessRequest {
  id: string;
  email: string;
  status: "PENDING" | "APPROVED" | "REJECTED";
  requestedAt: string;
  reviewedBy: string | null;
  reviewedAt: string | null;
  reviewerNote: string | null;
}

async function req<T>(path: string, init: RequestInit, token?: string | null): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api${path}`, {
      ...init,
      headers: { ...headers, ...(init.headers as Record<string, string>) },
    });
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
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// public
export function requestAccess(email: string): Promise<AccessRequestResult> {
  return req<AccessRequestResult>("/access/request", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

// admin
export function fetchAccessRequests(token: string | null, status?: string): Promise<AccessRequest[]> {
  const q = status && status !== "ALL" ? `?status=${encodeURIComponent(status)}` : "";
  return req<AccessRequest[]>(`/admin/access-requests${q}`, { method: "GET" }, token);
}

export function approveAccessRequest(token: string | null, id: string, note = ""): Promise<AccessRequest> {
  return req<AccessRequest>(`/admin/access-requests/${id}/approve`, {
    method: "POST",
    body: JSON.stringify({ note }),
  }, token);
}

export function rejectAccessRequest(token: string | null, id: string, note = ""): Promise<AccessRequest> {
  return req<AccessRequest>(`/admin/access-requests/${id}/reject`, {
    method: "POST",
    body: JSON.stringify({ note }),
  }, token);
}
