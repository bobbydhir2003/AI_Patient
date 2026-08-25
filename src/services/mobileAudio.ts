/**
 * Mobile-aware, user-facing labels for the audio-setup control. The INTERNAL
 * values are unchanged ("speakers" | "headphones") so all VAD / interruption
 * logic keeps working; only the wording adapts to the device. Pure + testable.
 */
import type { AudioSetup } from "./voiceActivityDetector";

export interface AudioSetupOption {
  value: AudioSetup;
  label: string;
}

/** Options for the audio-setup <select>. On phones we never claim "laptop". */
export function audioSetupOptions(isMobile: boolean): AudioSetupOption[] {
  if (isMobile) {
    return [
      { value: "speakers", label: "Phone / Device Speaker" },
      { value: "headphones", label: "Headphones / Earbuds" },
    ];
  }
  return [
    { value: "speakers", label: "Laptop speakers" },
    { value: "headphones", label: "Headphones / headset" },
  ];
}

/** Note shown when automatic interruption is enabled. Mobile phones put the
 * speaker and mic close together, so the guidance is stronger there. */
export function autoInterruptNote(isMobile: boolean): string {
  return isMobile
    ? "Best with headphones or earbuds. On the phone speaker the patient's voice can cause false interruptions — leave this off unless using headphones."
    : "Works best with headphones. Laptop speakers may cause false interruptions.";
}

/** Coarse, non-identifying device category for telemetry only. */
export function deviceCategory(): "ios" | "android" | "desktop" {
  if (typeof navigator === "undefined") return "desktop";

  const ua = navigator.userAgent || "";
  const isIOS =
    /iPhone|iPod|iPad/.test(ua) ||
    (navigator.platform === "MacIntel" &&
      typeof navigator.maxTouchPoints === "number" &&
      navigator.maxTouchPoints > 1);

  if (isIOS) return "ios";
  if (/Android/.test(ua)) return "android";

  return "desktop";
}

