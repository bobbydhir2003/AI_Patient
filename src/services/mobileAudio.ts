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

/**
 * iOS/iPadOS detection, isolated and documented on purpose.
 *
 * This is NOT general "is this mobile" detection (see useIsMobile, a separate
 * viewport/pointer-based hook used for UI labels only) - it exists for exactly
 * one reason: MediaSource.isTypeSupported("audio/mpeg") reports `true` on iOS
 * Safari, but iOS's real-world audio-only MSE playback has a long history of
 * unreliability that the feature-detection cannot see. There is no capability
 * check for "will progressive audio actually play reliably here" - only a
 * platform check achieves what we need, so UA/platform sniffing is used
 * deliberately here (see the mobile voice reliability audit).
 *
 * iPadOS 13+ reports as "MacIntel" in the UA (desktop-Safari spoofing), so it
 * is distinguished from a real Mac via maxTouchPoints (a real Mac reports 0).
 */
export function isIOSDevice(): boolean {
  if (typeof navigator === "undefined") return false;
  const ua = navigator.userAgent || "";
  const isIPhoneOrIPod = /iPhone|iPod/.test(ua);
  const isIPadUA = /iPad/.test(ua);
  const isIPadOSSpoofingMac =
    navigator.platform === "MacIntel" && typeof navigator.maxTouchPoints === "number" && navigator.maxTouchPoints > 1;
  return isIPhoneOrIPod || isIPadUA || isIPadOSSpoofingMac;
}

/** Coarse, non-identifying device category for telemetry only (never sent
 * with any identifying detail beyond this one label). */
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
