/**
 * User-friendly mapping of getUserMedia / microphone failures, plus a
 * pre-flight capability check. Pure and dependency-free so it can be unit
 * tested in Node.
 *
 * The interview must NEVER get stuck: every failure resolves to a clear message
 * and the student can always continue by typing.
 */

export interface MicErrorInfo {
  /** Message shown to the student. */
  message: string;
  /** Stable code for tests / analytics. */
  code:
    | "blocked"
    | "no_microphone"
    | "in_use"
    | "insecure_context"
    | "unsupported"
    | "overconstrained"
    | "unknown";
}

/** Shape we read from a DOMException-like error without depending on the DOM. */
interface ErrorLike {
  name?: string;
  message?: string;
}

/**
 * Map a getUserMedia rejection to a friendly message. Handles the standard
 * DOMException names across Safari, Chrome and Firefox.
 */
export function describeMicError(err: unknown): MicErrorInfo {
  const e = (err ?? {}) as ErrorLike;
  const name = e.name ?? "";

  switch (name) {
    case "NotAllowedError":
    case "SecurityError":
    case "PermissionDeniedError": // legacy Chrome
      return {
        code: "blocked",
        message:
          "Microphone access is blocked. Enable microphone permission for this site in your browser settings, then tap Retry — or continue by typing.",
      };
    case "NotFoundError":
    case "DevicesNotFoundError": // legacy
      return {
        code: "no_microphone",
        message:
          "No microphone was detected on this device. You can continue the interview by typing.",
      };
    case "NotReadableError":
    case "TrackStartError": // legacy
    case "AbortError":
      return {
        code: "in_use",
        message:
          "The microphone is unavailable — it may be in use by another app (e.g. a call). Close it and tap Retry, or continue by typing.",
      };
    case "OverconstrainedError":
    case "ConstraintNotSatisfiedError":
      return {
        code: "overconstrained",
        message:
          "This microphone couldn't be configured for voice input. You can continue by typing.",
      };
    default:
      return {
        code: "unknown",
        message:
          "Microphone access failed. Allow access and tap Retry, or continue the interview by typing.",
      };
  }
}

export interface VoicePreconditions {
  ok: boolean;
  info?: MicErrorInfo;
}

/**
 * Check the environment can support microphone capture BEFORE prompting.
 * `deps` is injected so this is fully testable without a browser.
 */
export function checkVoicePreconditions(deps: {
  isSecureContext: boolean;
  hasMediaDevices: boolean;
  hasGetUserMedia: boolean;
}): VoicePreconditions {
  // A non-secure origin (http:// other than localhost) disables getUserMedia.
  if (!deps.isSecureContext) {
    return {
      ok: false,
      info: {
        code: "insecure_context",
        message:
          "Voice input requires a secure (https) connection. Open this page over https, or continue by typing.",
      },
    };
  }
  if (!deps.hasMediaDevices || !deps.hasGetUserMedia) {
    return {
      ok: false,
      info: {
        code: "unsupported",
        message:
          "Voice input isn't available in this browser. You can continue the interview by typing.",
      },
    };
  }
  return { ok: true };
}

/** Convenience wrapper that reads the real browser globals. */
export function checkBrowserVoicePreconditions(): VoicePreconditions {
  const secure =
    typeof window !== "undefined" && typeof window.isSecureContext === "boolean"
      ? window.isSecureContext
      : true; // assume secure if the flag is unavailable (very old browsers)
  const md =
    typeof navigator !== "undefined" &&
    (navigator as Navigator & { mediaDevices?: MediaDevices }).mediaDevices;
  return checkVoicePreconditions({
    isSecureContext: secure,
    hasMediaDevices: Boolean(md),
    hasGetUserMedia: Boolean(md && typeof md.getUserMedia === "function"),
  });
}
