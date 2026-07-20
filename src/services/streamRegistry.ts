/**
 * Tiny registry decoupling patientVoiceService (the single cancellation entry
 * point) from patientStreamService (the streaming pipeline), avoiding a
 * circular import. cancelPatientSpeech() cancels BOTH the classic playback
 * and any active streaming exchange through this registry.
 */

let activeCancel: (() => void) | null = null;

export function registerActiveStreamCancel(cancel: (() => void) | null): void {
  activeCancel = cancel;
}

/** Cancel the active streaming exchange, if any. Safe to call repeatedly. */
export function cancelActiveStream(): void {
  const cancel = activeCancel;
  activeCancel = null;
  cancel?.();
}
