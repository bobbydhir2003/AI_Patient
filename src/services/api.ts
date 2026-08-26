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

/**
 * localStorage key under which AuthContext persists the bearer token. Kept in
 * sync with src/state/AuthContext.tsx (TOKEN_KEY).
 */
export const AUTH_TOKEN_STORAGE_KEY = "ptai-auth-token";

/**
 * Current bearer token (or null). The student-facing session, interview,
 * assessment and voice endpoints now REQUIRE authentication, so every request
 * that goes through this module attaches the token when present. Public
 * endpoints (cases, health) simply ignore it.
 */
export function getStoredAuthToken(): string | null {
  try {
    return localStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Build headers with the bearer token attached when available. */
export function withAuthHeaders(
  base: Record<string, string> = {},
): Record<string, string> {
  const token = getStoredAuthToken();
  return token ? { ...base, Authorization: `Bearer ${token}` } : { ...base };
}

export interface ApiMessage {
  id: string;
  sender: "student" | "patient";
  text: string;
  timestamp: string;
  speakerId?: string;
  speakerLabel?: string;
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
  /** Multi-participant speaker of the primary segment. */
  speakerId?: string;
  speakerLabel?: string;
  /** Ordered segments when more than one participant answered ("both"). */
  responses?: {
    turnId: string;
    speakerId: string;
    speakerLabel: string;
    text: string;
    speech: PatientSpeechStyle | null;
  }[];
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
  const url = `${API_BASE_URL}/api${path}`;
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      // Attach the bearer token (auth-required endpoints) while preserving any
      // caller-supplied headers.
      headers: withAuthHeaders({
        "Content-Type": "application/json",
        ...((init?.headers as Record<string, string>) ?? {}),
      }),
    });
  } catch (networkError) {
    // fetch() itself rejected (backend unreachable, DNS, CORS preflight blocked).
    // Surface the real request URL in dev so the failure is diagnosable instead
    // of only showing the generic "backend is running?" message.
    if (import.meta.env.DEV) {
      console.error(`[api] network error requesting ${url}:`, networkError);
    }
    throw new ApiError(
      "Could not reach the server. Check that the backend is running.",
      0,
      "network_error",
    );
  }
  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    let code = "unknown_error";
    let rawBody = "";
    try {
      rawBody = await response.text();
      const body = JSON.parse(rawBody) as { error?: { message?: string; code?: string } };
      if (body.error?.message) message = body.error.message;
      if (body.error?.code) code = body.error.code;
    } catch {
      // non-JSON error body; keep defaults but retain rawBody for dev logging
    }
    if (import.meta.env.DEV) {
      console.error(`[api] ${init?.method ?? "GET"} ${url} -> ${response.status}`, rawBody);
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

/** Which voice architecture the real InterviewPage should use. Always
 * narrowed to exactly these two values on the client too (see
 * fetchInterviewConfig) - an unrecognized backend value fails safe to
 * "legacy", mirroring Settings.voice_engine's own backend-side validator. */
export type VoiceEngine = "legacy" | "livekit";

/** Student-safe interview feature flags (streaming patient responses). */
export interface InterviewConfig {
  streamingEnabled: boolean;
  sentencePipeliningEnabled: boolean;
  voiceEngine: VoiceEngine;
}

let interviewConfigCache: InterviewConfig | null = null;

/** Fetch (and cache) the backend's interview feature flags. Unknown/failed
 * fetches report streaming disabled AND voiceEngine "legacy" so the stable,
 * proven path is always the default when config can't be reached. */
export async function fetchInterviewConfig(): Promise<InterviewConfig> {
  if (interviewConfigCache) return interviewConfigCache;
  try {
    const cfg = await request<InterviewConfig & { voiceEngine?: unknown }>("/interviews/config");
    interviewConfigCache = {
      streamingEnabled: cfg.streamingEnabled === true,
      sentencePipeliningEnabled: cfg.sentencePipeliningEnabled === true,
      voiceEngine: cfg.voiceEngine === "livekit" ? "livekit" : "legacy",
    };
  } catch {
    return { streamingEnabled: false, sentencePipeliningEnabled: false, voiceEngine: "legacy" }; // not cached
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
