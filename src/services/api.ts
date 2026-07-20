import type { CaseCatalog, PatientCase } from "../types/case";
import type { Assessment, AssessmentTurn, Rubric } from "../types/assessment";
import type { PatientSpeechStyle } from "../types/interview";

/**
 * API client for the FastAPI backend. The backend is REQUIRED: this module
 * never fabricates data, and callers must surface failures to the user.
 * Base URL comes from VITE_API_BASE_URL (defaults to the local dev backend).
 */
export const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";

export interface ApiMessage {
  id: string;
  sender: "student" | "patient";
  text: string;
  timestamp: string;
}

export interface ApiSession {
  sessionId: string;
  caseId: string;
  studentName: string;
  studentId: string;
  status: "active" | "completed";
  locked: boolean;
  startedAt: string;
  completedAt: string | null;
  messages: ApiMessage[];
}

export interface ApiTurn {
  turnId: string;
  patientText: string;
  status: string;
  sessionStatus: string;
  /** Controlled delivery labels for TTS (null on replays / when omitted). */
  speech: PatientSpeechStyle | null;
}

/** Whether the realistic (ElevenLabs) patient voice is available for a case.
 * Never contains voice IDs or key material. */
export interface VoiceStatus {
  caseId: string;
  available: boolean;
  provider: "elevenlabs" | "browser";
  fallbackRate: number;
}

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(message: string, status: number, code = "unknown_error") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    let code = "unknown_error";
    try {
      const body = (await response.json()) as { error?: { message?: string; code?: string } };
      if (body.error?.message) message = body.error.message;
      if (body.error?.code) code = body.error.code;
    } catch {
      // keep defaults
    }
    throw new ApiError(message, response.status, code);
  }
  return (await response.json()) as T;
}

export function fetchHealth(): Promise<{ status: string; database: string }> {
  return request("/health");
}

/** Student-safe case catalog, grouped into sections by case category. */
export function fetchCaseCatalog(): Promise<CaseCatalog> {
  return request<CaseCatalog>("/cases");
}

export function fetchCase(caseId: string): Promise<PatientCase> {
  return request<PatientCase>(`/cases/${encodeURIComponent(caseId)}`);
}

export function createSession(
  studentName: string,
  studentId: string,
  caseId: string,
): Promise<ApiSession> {
  return request<ApiSession>("/sessions", {
    method: "POST",
    body: JSON.stringify({ studentName, studentId, caseId }),
  });
}

export function fetchSession(sessionId: string): Promise<ApiSession> {
  return request<ApiSession>(`/sessions/${encodeURIComponent(sessionId)}`);
}

export function completeSession(sessionId: string): Promise<ApiSession> {
  return request<ApiSession>(`/sessions/${encodeURIComponent(sessionId)}/complete`, {
    method: "POST",
  });
}

/**
 * Send a student message. `caseId` is the case shown in the UI; the backend
 * rejects the request (409 case_session_mismatch) if it does not match the
 * session's case. `clientTurnId` makes the exchange idempotent: retries with
 * the same id return the already-saved turns instead of regenerating.
 */
export function sendStudentMessage(
  sessionId: string,
  text: string,
  caseId: string,
  clientTurnId: string,
  source: "typed" | "speech" = "typed",
): Promise<ApiTurn> {
  return request<ApiTurn>(`/interviews/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ text, caseId, clientTurnId, source }),
  });
}

/** Voice availability for a case (used to pick elevenlabs vs. browser TTS). */
export function fetchVoiceStatus(caseId: string): Promise<VoiceStatus> {
  return request<VoiceStatus>(`/voice/status/${encodeURIComponent(caseId)}`);
}

/** Student-safe interview feature flags (streaming patient responses). */
export interface InterviewConfig {
  streamingEnabled: boolean;
  sentencePipeliningEnabled: boolean;
}

let interviewConfigCache: InterviewConfig | null = null;

/** Fetch (and cache) the backend's interview feature flags. Unknown/failed
 * fetches report streaming disabled so the stable path is always the default. */
export async function fetchInterviewConfig(): Promise<InterviewConfig> {
  if (interviewConfigCache) return interviewConfigCache;
  try {
    const cfg = await request<InterviewConfig>("/interviews/config");
    interviewConfigCache = {
      streamingEnabled: cfg.streamingEnabled === true,
      sentencePipeliningEnabled: cfg.sentencePipeliningEnabled === true,
    };
  } catch {
    return { streamingEnabled: false, sentencePipeliningEnabled: false }; // not cached
  }
  return interviewConfigCache;
}

/** Test/dev hook: clear the cached interview config. */
export function clearInterviewConfigCache(): void {
  interviewConfigCache = null;
}

export interface SavedTurn {
  id: string;
  sessionId: string;
  clientTurnId: string | null;
  speaker: "student" | "patient";
  content: string;
  source: string | null;
  turnIndex: number;
  createdAt: string;
}

/** All saved transcript turns, in stable conversation order. */
export function fetchSessionTurns(sessionId: string): Promise<SavedTurn[]> {
  return request<SavedTurn[]>(`/sessions/${encodeURIComponent(sessionId)}/turns`);
}

// ---------------- AI assessment ----------------

/** Create (run) the AI assessment for a completed session. May take a while. */
export async function createAssessment(sessionId: string, retry: boolean = false): Promise<Assessment> {
  const url = retry 
    ? `/sessions/${encodeURIComponent(sessionId)}/assessment?retry=true`
    : `/sessions/${encodeURIComponent(sessionId)}/assessment`;
  return request<Assessment>(url, {
    method: "POST",
  });
}

export async function getAssessmentStatus(sessionId: string): Promise<{
  session_id: string;
  assessment_id: string | null;
  status: string;
  stage: string;
  assessment_mode: string | null;
  error_code: string | null;
}> {
  return request(`/sessions/${encodeURIComponent(sessionId)}/assessment/status`);
}

export function fetchLatestAssessment(sessionId: string): Promise<Assessment> {
  return request<Assessment>(`/sessions/${encodeURIComponent(sessionId)}/assessment`);
}

export function fetchAssessment(assessmentId: string): Promise<Assessment> {
  return request<Assessment>(`/assessments/${encodeURIComponent(assessmentId)}`);
}

export function fetchAssessmentTranscript(assessmentId: string): Promise<AssessmentTurn[]> {
  return request<AssessmentTurn[]>(
    `/assessments/${encodeURIComponent(assessmentId)}/transcript`,
  );
}

export function fetchRubrics(): Promise<Rubric[]> {
  return request<Rubric[]>("/rubrics");
}
