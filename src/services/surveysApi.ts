/**
 * Pre/Post experience survey API client.
 *
 * The REDCap token is NEVER referenced here — it lives only server-side. This
 * client just sends the student's answers (keyed by the exact REDCap variable
 * names) to the authenticated, session-scoped backend endpoints. The backend
 * resolves the REDCap record_id (the student's NUID) itself.
 */
import { API_BASE_URL, ApiError, getStoredAuthToken } from "./api";

async function surveyRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getStoredAuthToken();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api${path}`, {
      ...init,
      headers: { ...headers, ...((init?.headers as Record<string, string>) ?? {}) },
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
  return (await response.json()) as T;
}

export interface SurveyStatus {
  sessionId: string;
  caseName: string;
  nuidOnFile: boolean;
  /** Case-level lifecycle: "not_started" | "in_progress" | "completed". This is
   * the field the survey flow gates on (one package per student+case). */
  overallStatus: string;
  preSubmitted: boolean;
  postSubmitted: boolean;
  preSyncStatus: string | null;
  postSyncStatus: string | null;
}

export interface SurveySubmitResult {
  phase: "pre" | "post";
  syncStatus: string;
  /** Case-level lifecycle after this stage: "in_progress" | "completed". */
  overallStatus: string;
  alreadySubmitted: boolean;
}

export function getSurveyStatus(sessionId: string): Promise<SurveyStatus> {
  return surveyRequest<SurveyStatus>(
    `/interviews/${encodeURIComponent(sessionId)}/surveys/status`,
  );
}

/** answers keyed by exact REDCap variable names (e.g. pre_conf_begin: 4). */
export function submitPreSurvey(
  sessionId: string,
  answers: Record<string, number>,
): Promise<SurveySubmitResult> {
  return surveyRequest<SurveySubmitResult>(
    `/interviews/${encodeURIComponent(sessionId)}/surveys/pre`,
    { method: "POST", body: JSON.stringify(answers) },
  );
}

/** answers keyed by exact REDCap variable names (12 Likert ints + 5 OE strings). */
export function submitPostSurvey(
  sessionId: string,
  answers: Record<string, number | string>,
): Promise<SurveySubmitResult> {
  return surveyRequest<SurveySubmitResult>(
    `/interviews/${encodeURIComponent(sessionId)}/surveys/post`,
    { method: "POST", body: JSON.stringify(answers) },
  );
}
