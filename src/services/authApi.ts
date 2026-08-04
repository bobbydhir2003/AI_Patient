/**
 * Authenticated API client for the auth, student self-service, and admin
 * endpoints. Shares the base URL and ApiError type with services/api.ts.
 * A bearer token is attached to every request; 401s should trigger logout.
 */
import { API_BASE_URL, ApiError } from "./api";
import type { Assessment } from "../types/assessment";

export type UserRole = "student" | "admin" | "super_admin";

export interface AuthUser {
  id: string;
  fullName: string;
  email: string;
  studentNumber: string;
  role: UserRole;
  isActive: boolean;
  /** True only for the seeded/default system admin (created via create_admin).
   * Distinguishes it from a user who was later promoted to admin: the system
   * admin lands directly on the Admin Dashboard, a promoted admin lands on the
   * Patient Simulator and opens the dashboard via the Admin Management control. */
  isSystemAdmin: boolean;
  studentId: string | null;
  createdAt: string;
  lastLoginAt: string | null;
}

export interface TokenResponse {
  accessToken: string;
  tokenType: string;
  expiresIn: number;
  user: AuthUser;
}

async function authRequest<T>(
  path: string,
  token: string | null,
  init?: RequestInit,
): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API_BASE_URL}/api${path}`, {
    ...init,
    headers: { ...headers, ...(init?.headers as Record<string, string>) },
  });
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

// ---------------- auth ----------------
export function apiLogin(email: string, password: string): Promise<TokenResponse> {
  return authRequest<TokenResponse>("/auth/login", null, {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

/** D2: registration creates a PENDING account and returns a status message
 * (no token / no auto-login). */
export interface RegisterResult {
  status: string; // "pending"
  message: string;
}

export function apiRegister(input: {
  fullName: string;
  email: string;
  password: string;
  studentNumber: string;
}): Promise<RegisterResult> {
  return authRequest<RegisterResult>("/auth/register", null, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function apiMe(token: string): Promise<AuthUser> {
  return authRequest<AuthUser>("/auth/me", token);
}

export function apiLogout(token: string): Promise<{ success: boolean }> {
  return authRequest("/auth/logout", token, { method: "POST" });
}

// ---------------- shared shapes ----------------
export interface SessionSummary {
  sessionId: string;
  studentId: string;
  studentName: string;
  caseId: string;
  caseCategory: string;
  status: string;
  locked: boolean;
  turnCount: number;
  studentTurnCount: number;
  durationSeconds: number | null;
  hasAssessment: boolean;
  overallLevel: string | null;
  startedAt: string;
  completedAt: string | null;
}

export interface TranscriptMessage {
  id: string;
  sessionId: string;
  speaker: "student" | "patient";
  content: string;
  source: string | null;
  turnIndex: number;
  createdAt: string;
  speakerId?: string;
  speakerLabel?: string;
}

// ---------------- student self-service ----------------
export function fetchMySessions(token: string): Promise<SessionSummary[]> {
  return authRequest<SessionSummary[]>("/students/me/sessions", token);
}

export function fetchMySession(token: string, sessionId: string): Promise<SessionSummary> {
  return authRequest<SessionSummary>(`/students/me/sessions/${sessionId}`, token);
}

export function fetchMyTranscript(token: string, sessionId: string): Promise<TranscriptMessage[]> {
  return authRequest<TranscriptMessage[]>(`/students/me/sessions/${sessionId}/transcript`, token);
}

export function fetchMyAssessment(token: string, sessionId: string): Promise<Assessment | null> {
  return authRequest<Assessment | null>(`/students/me/sessions/${sessionId}/assessment`, token);
}

// ---------------- admin ----------------
export interface RecentActivity {
  sessionId: string;
  studentId: string;
  studentName: string;
  caseId: string;
  status: string;
  startedAt: string;
  completedAt: string | null;
}

export interface AssessmentLevelCount {
  level: string;
  count: number;
}

export interface NeedsAttention {
  incompleteSessions: number;
  completedWithoutAssessment: number;
  studentsMultipleIncomplete: number;
  sessionsActiveOver24h: number;
}

export interface RecentSessionItem {
  sessionId: string;
  studentId: string;
  studentName: string;
  studentNumber: string;
  caseId: string;
  caseCategory: string;
  status: string;
  hasAssessment: boolean;
  overallLevel: string | null;
  startedAt: string;
  completedAt: string | null;
}

export interface RecentStudentItem {
  id: string;
  name: string;
  studentNumber: string;
  sessionCount: number;
  assessmentCount: number;
  lastActivityAt: string | null;
}

export interface AdminDashboard {
  totalStudents: number;
  activeStudents: number;
  inactiveStudents: number;
  totalSessions: number;
  completedSessions: number;
  incompleteSessions: number;
  archivedSessions: number;
  totalAssessments: number;
  recentActivity: RecentActivity[];
  assessmentLevels: AssessmentLevelCount[];
  needsAttention: NeedsAttention | null;
  recentSessions: RecentSessionItem[];
  recentStudents: RecentStudentItem[];
}

export interface SearchStudentHit {
  id: string;
  name: string;
  email: string;
  studentNumber: string;
  isActive: boolean;
}

export interface SearchSessionHit {
  sessionId: string;
  studentId: string;
  studentName: string;
  caseId: string;
  status: string;
  startedAt: string;
}

export interface SearchResults {
  students: SearchStudentHit[];
  sessions: SearchSessionHit[];
}

export interface StudentListItem {
  id: string;
  name: string;
  email: string;
  studentNumber: string;
  isActive: boolean;
  hasAccount: boolean;
  sessionCount: number;
  completedCount: number;
  assessmentCount: number;
  createdAt: string;
  lastActivityAt: string | null;
}

export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
}

export interface StudentDetail {
  id: string;
  name: string;
  email: string;
  studentNumber: string;
  isActive: boolean;
  hasAccount: boolean;
  accountEmail: string | null;
  role: string | null;
  createdAt: string;
  lastLoginAt: string | null;
  sessionCount: number;
  completedCount: number;
  assessmentCount: number;
}

export interface AuditLogEntry {
  id: string;
  adminUserId: string | null;
  adminEmail: string;
  actionType: string;
  recordType: string;
  recordId: string;
  description: string;
  createdAt: string;
}

export function fetchDashboard(token: string): Promise<AdminDashboard> {
  return authRequest<AdminDashboard>("/admin/dashboard", token);
}

export function adminSearch(token: string, query: string): Promise<SearchResults> {
  const q = new URLSearchParams({ q: query, limit: "6" });
  return authRequest<SearchResults>(`/admin/search?${q.toString()}`, token);
}

export function fetchStudents(
  token: string,
  params: { search?: string; status?: string; sort?: string; page?: number; pageSize?: number },
): Promise<Paginated<StudentListItem>> {
  const q = new URLSearchParams();
  if (params.search) q.set("search", params.search);
  if (params.status) q.set("status", params.status);
  if (params.sort) q.set("sort", params.sort);
  q.set("page", String(params.page ?? 1));
  q.set("page_size", String(params.pageSize ?? 20));
  return authRequest<Paginated<StudentListItem>>(`/admin/students?${q.toString()}`, token);
}

export function fetchStudentDetail(token: string, studentId: string): Promise<StudentDetail> {
  return authRequest<StudentDetail>(`/admin/students/${studentId}`, token);
}

export function fetchStudentSessions(token: string, studentId: string): Promise<SessionSummary[]> {
  return authRequest<SessionSummary[]>(`/admin/students/${studentId}/sessions`, token);
}

export function fetchAdminSessions(
  token: string,
  params: { caseId?: string; status?: string; sort?: string; page?: number; pageSize?: number },
): Promise<Paginated<SessionSummary>> {
  const q = new URLSearchParams();
  if (params.caseId) q.set("case_id", params.caseId);
  if (params.status) q.set("status", params.status);
  if (params.sort) q.set("sort", params.sort);
  q.set("page", String(params.page ?? 1));
  q.set("page_size", String(params.pageSize ?? 20));
  return authRequest<Paginated<SessionSummary>>(`/admin/sessions?${q.toString()}`, token);
}

export function fetchAdminSession(token: string, sessionId: string): Promise<SessionSummary> {
  return authRequest<SessionSummary>(`/admin/sessions/${sessionId}`, token);
}

export function fetchAdminTranscript(token: string, sessionId: string): Promise<TranscriptMessage[]> {
  return authRequest<TranscriptMessage[]>(`/admin/sessions/${sessionId}/transcript`, token);
}

export function fetchAdminAssessment(token: string, sessionId: string): Promise<Assessment | null> {
  return authRequest<Assessment | null>(`/admin/sessions/${sessionId}/assessment`, token);
}

export function fetchAuditLogs(
  token: string,
  params: { page?: number; pageSize?: number },
): Promise<Paginated<AuditLogEntry>> {
  const q = new URLSearchParams();
  q.set("page", String(params.page ?? 1));
  q.set("page_size", String(params.pageSize ?? 25));
  return authRequest<Paginated<AuditLogEntry>>(`/admin/audit-logs?${q.toString()}`, token);
}

// ---------------- admin notifications ----------------
export interface AdminNotification {
  id: string;
  title: string;
  message: string;
  type: string;
  createdAt: string;
  isRead: boolean;
  link?: string | null;
}
export interface NotificationFeed {
  notifications: AdminNotification[];
  unreadCount: number;
}
export function fetchNotifications(token: string): Promise<NotificationFeed> {
  return authRequest<NotificationFeed>("/admin/notifications", token);
}
export function markAllNotificationsRead(token: string): Promise<{ success: boolean }> {
  return authRequest("/admin/notifications/read-all", token, { method: "POST" });
}

// ---------------- admin mutations ----------------
export function setStudentStatus(token: string, studentId: string, isActive: boolean) {
  return authRequest(`/admin/students/${studentId}/status`, token, {
    method: "PATCH",
    body: JSON.stringify({ isActive }),
  });
}

export function deleteStudent(token: string, studentId: string, confirm: string) {
  return authRequest(`/admin/students/${studentId}`, token, {
    method: "DELETE",
    body: JSON.stringify({ confirm }),
  });
}

export function archiveSession(token: string, sessionId: string) {
  return authRequest(`/admin/sessions/${sessionId}/archive`, token, { method: "PATCH" });
}

export function deleteSession(token: string, sessionId: string) {
  return authRequest(`/admin/sessions/${sessionId}`, token, { method: "DELETE" });
}

export function deleteAssessment(token: string, assessmentId: string) {
  return authRequest(`/admin/assessments/${assessmentId}`, token, { method: "DELETE" });
}

export function deleteMessage(token: string, messageId: string) {
  return authRequest(`/admin/messages/${messageId}`, token, { method: "DELETE" });
}
