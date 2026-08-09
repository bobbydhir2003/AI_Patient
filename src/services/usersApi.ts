/**
 * Admin user-account management API (D3). Bearer token required; the backend
 * enforces every permission rule (only student/admin are assignable, last-admin
 * protection, no self-role-change). There is no public role-change endpoint.
 */
import { API_BASE_URL, ApiError } from "./api";

export type AccountStatus = "PENDING" | "ACTIVE" | "REJECTED" | "DISABLED";
export type UserRole = "student" | "admin";

export interface AdminUser {
  id: string;
  fullName: string;
  email: string;
  studentNumber: string;
  role: UserRole;
  accountStatus: AccountStatus;
  isActive: boolean;
  studentId: string | null;
  createdAt: string;
  lastLoginAt: string | null;
  reviewedBy: string | null;
  reviewedAt: string | null;
  reviewNote: string | null;
}

async function req<T>(path: string, token: string | null, init?: RequestInit): Promise<T> {
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

export interface UserSummary {
  total: number;
  pending: number;
  active: number;
  disabled: number;
  rejected: number;
  admins: number;
}

export interface BulkUserResult {
  requested: number;
  succeeded: string[];
  skipped: { userId: string; reason: string }[];
  summary: UserSummary;
}

export function fetchUsers(token: string | null, status?: string): Promise<AdminUser[]> {
  const q = status && status !== "ALL" ? `?status=${encodeURIComponent(status)}` : "";
  return req<AdminUser[]>(`/admin/users${q}`, token);
}

/** Real per-status account counts for the summary cards. */
export function fetchUserSummary(token: string | null): Promise<UserSummary> {
  return req<UserSummary>(`/admin/users/summary`, token);
}

const post = (token: string | null, id: string, action: string, body?: unknown) =>
  req<AdminUser>(`/admin/users/${id}/${action}`, token, { method: "POST", body: body ? JSON.stringify(body) : undefined });

export const approveUser = (t: string | null, id: string) => post(t, id, "approve");
export const enableUser = (t: string | null, id: string) => post(t, id, "enable");
export const rejectUser = (t: string | null, id: string, note = "") => post(t, id, "reject", { note });
export const disableUser = (t: string | null, id: string, note = "") => post(t, id, "disable", { note });
export const changeUserRole = (t: string | null, id: string, role: UserRole) => post(t, id, "role", { role });

// ------------------------------- bulk actions -------------------------------
const bulk = (token: string | null, path: string, body?: unknown) =>
  req<BulkUserResult>(`/admin/users/${path}`, token, {
    method: "POST",
    body: body ? JSON.stringify(body) : undefined,
  });

export const bulkApproveUsers = (t: string | null, userIds: string[]) =>
  bulk(t, "bulk-approve", { userIds });
export const bulkRejectUsers = (t: string | null, userIds: string[], note = "") =>
  bulk(t, "bulk-reject", { userIds, note });
export const approveAllPending = (t: string | null) => bulk(t, "approve-all-pending");
