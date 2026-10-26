import type { PatientCase } from "../types/case";
import type { Assessment } from "../types/assessment";
import { API_BASE_URL, ApiError } from "./api";
import type { AuthUser, SessionSummary, TranscriptMessage } from "./authApi";

/**
 * AI iEXCEL course launches (LTI 1.3). A launch hands this app a one-time code
 * in the URL fragment; it is exchanged once for a short-lived session token
 * scoped to ONE course offering. That token lives in sessionStorage (this tab
 * only, gone when the tab closes) - never localStorage, never a URL - and it
 * takes precedence over a direct-login token in the same tab.
 */
export const LTI_SESSION_STORAGE_KEY = "ptai-lti-session";

export type LtiRole = "learner" | "instructor";

export interface LtiContext {
  id: string;
  label: string;
  title: string;
}

export interface LtiSession {
  token: string;
  /** Epoch milliseconds after which the token is no longer valid. */
  expiresAt: number;
  role: LtiRole;
  context: LtiContext;
  user: AuthUser;
  assignedCaseIds: string[];
  devFakeAi: boolean;
}

interface LtiSessionInfo {
  role: LtiRole;
  context: LtiContext;
  user: AuthUser;
  assignedCaseIds: string[];
  devFakeAi: boolean;
}

export function readLtiSession(): LtiSession | null {
  try {
    const raw = sessionStorage.getItem(LTI_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as LtiSession;
    if (!parsed.token || !parsed.expiresAt || parsed.expiresAt <= Date.now()) {
      sessionStorage.removeItem(LTI_SESSION_STORAGE_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function saveLtiSession(session: LtiSession): void {
  try {
    sessionStorage.setItem(LTI_SESSION_STORAGE_KEY, JSON.stringify(session));
  } catch {
    /* storage unavailable: the session lasts until reload */
  }
}

export function clearLtiSession(): void {
  try {
    sessionStorage.removeItem(LTI_SESSION_STORAGE_KEY);
  } catch {
    /* storage unavailable */
  }
}

async function ltiRequest<T>(path: string, token: string | null, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api${path}`, { ...init, headers, cache: "no-store" });
  } catch {
    throw new ApiError("Could not reach the server. Please try again.", 0, "network_error");
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

/** Trade the one-time launch code for a course session (single use, 60 s). */
export async function exchangeLaunchCode(code: string): Promise<LtiSession> {
  const res = await ltiRequest<{
    accessToken: string;
    expiresIn: number;
    session: LtiSessionInfo;
  }>("/lti/session/exchange", null, { method: "POST", body: JSON.stringify({ code }) });
  return {
    token: res.accessToken,
    expiresAt: Date.now() + res.expiresIn * 1000,
    ...res.session,
  };
}

export function fetchLtiSessionInfo(token: string): Promise<LtiSessionInfo> {
  return ltiRequest("/lti/session", token);
}

export function fetchAssignedCases(token: string): Promise<PatientCase[]> {
  return ltiRequest("/lti/cases", token);
}

// ---------------- course instructor (launched offering only) ----------------

export interface RosterMember {
  userId: string;
  fullName: string;
  firstLaunchAt: string;
  lastLaunchAt: string;
  sessionCount: number;
  completedCount: number;
}

export interface CaseAssignment {
  assignedCaseIds: string[];
  assignableCases: PatientCase[];
}

export interface InstructorSessionDetail {
  session: SessionSummary;
  assessment: Assessment | null;
  transcriptsEnabled: boolean;
}

export function fetchRoster(token: string): Promise<RosterMember[]> {
  return ltiRequest("/lti/instructor/roster", token);
}

export function fetchCourseSessions(token: string): Promise<SessionSummary[]> {
  return ltiRequest("/lti/instructor/sessions", token);
}

export function fetchCourseSession(token: string, sessionId: string): Promise<InstructorSessionDetail> {
  return ltiRequest(`/lti/instructor/sessions/${encodeURIComponent(sessionId)}`, token);
}

export function fetchCourseTranscript(token: string, sessionId: string): Promise<TranscriptMessage[]> {
  return ltiRequest(`/lti/instructor/sessions/${encodeURIComponent(sessionId)}/transcript`, token);
}

export function fetchCaseAssignment(token: string): Promise<CaseAssignment> {
  return ltiRequest("/lti/instructor/cases", token);
}

export function saveCaseAssignment(token: string, caseIds: string[]): Promise<CaseAssignment> {
  return ltiRequest("/lti/instructor/cases", token, {
    method: "PUT",
    body: JSON.stringify({ caseIds }),
  });
}

// ---------------- development only ----------------

/** Development stand-in for the LiveKit voice interview (AI_SP_DEV_FAKE_AI). */
export function simulateInterview(token: string, sessionId: string): Promise<{ turnsWritten: number }> {
  return ltiRequest(`/dev/sessions/${encodeURIComponent(sessionId)}/simulate-interview`, token, {
    method: "POST",
  });
}

/** User-facing text for a launch failure reason code (never token contents). */
export function launchErrorMessage(reason: string | null): string {
  switch (reason) {
    case "expired":
      return "Your course session has expired. Return to AI iEXCEL and open the course again.";
    case "ended":
      return "You have left the course session. Return to AI iEXCEL to open the course again.";
    case "state_mismatch":
    case "state_replayed":
    case "state_expired":
    case "code_replayed":
    case "code_expired":
    case "invalid_code":
      return "This launch link was already used or has expired. Return to AI iEXCEL and select Open Course again.";
    case "platform_refused_launch":
      return "AI iEXCEL did not authorize this launch. You may no longer be enrolled in or assigned to this course.";
    case "account_disabled":
      return "Your AI Standardized Patient account is disabled. Please contact your instructor.";
    case "unknown_platform":
    case "unknown_client":
    case "unknown_deployment":
    case "foreign_target_link":
    case "unsupported_contract_version":
      return "This course is not connected to the AI Standardized Patient application. Please contact your administrator.";
    case "unsupported_role":
      return "Your role in this course does not have access to the AI Standardized Patient application.";
    default:
      return "This course launch could not be completed. Return to AI iEXCEL and open the course again.";
  }
}
