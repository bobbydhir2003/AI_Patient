/**
 * Maps an error thrown while starting an interview session to the correct UI
 * state + message. Pure and DEPENDENCY-FREE so it can be unit-tested with the
 * project's `node --test` harness (no bundler/env needed).
 *
 * Only genuine connection/network failures are treated as "offline". An
 * authorization error (403), an expired session (401) or a server error (5xx)
 * are NOT backend-unreachable conditions and must not be shown as "Not connected
 * to the interview backend".
 */
export type InterviewInitState = "offline" | "error";

export interface InterviewInitResult {
  connection: InterviewInitState;
  message: string;
  /** True only for genuine network/backend-unreachable failures. */
  offline: boolean;
}

const OFFLINE_MESSAGE = "Not connected to the interview backend.";

/** Structural check: our ApiError carries a numeric `status` and a `code`. */
function asApiError(err: unknown): { status: number; code?: string; message?: string } | null {
  if (err && typeof err === "object" && typeof (err as { status?: unknown }).status === "number") {
    return err as { status: number; code?: string; message?: string };
  }
  return null;
}

export function classifyInterviewInitError(err: unknown): InterviewInitResult {
  const api = asApiError(err);
  if (api) {
    // status === 0 (code "network_error") is our fetch-level failure.
    if (api.status === 0 || api.code === "network_error") {
      return { connection: "offline", message: OFFLINE_MESSAGE, offline: true };
    }
    if (api.status === 403) {
      return {
        connection: "error",
        message:
          "Admin accounts cannot start student interview sessions. Please sign in with a student account.",
        offline: false,
      };
    }
    if (api.status === 401) {
      return {
        connection: "error",
        message: "Your session has expired. Please sign in again.",
        offline: false,
      };
    }
    if (api.status >= 500) {
      return {
        connection: "error",
        message: "Interview service is temporarily unavailable. Please try again.",
        offline: false,
      };
    }
    // Any other API-level error (e.g. 404/422): show the server's message, not "offline".
    return {
      connection: "error",
      message: api.message || "The interview could not be started.",
      offline: false,
    };
  }
  // A non-ApiError means fetch itself rejected or an unexpected JS failure -
  // treat it as a genuine connectivity problem.
  return { connection: "offline", message: OFFLINE_MESSAGE, offline: true };
}
