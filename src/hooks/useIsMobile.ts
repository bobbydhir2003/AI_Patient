import { useEffect, useState } from "react";

/**
 * True on phone-sized screens (<= 767px) OR coarse-pointer touch devices.
 * CSS handles the visual layout; this hook only drives behavioural differences
 * (mobile-specific labels/defaults). Uses matchMedia — no resize thrashing.
 */
const QUERY = "(max-width: 767px), (pointer: coarse)";

function evaluate(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia(QUERY).matches;
}

export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState<boolean>(evaluate);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(QUERY);
    const onChange = () => setIsMobile(mql.matches);
    onChange();
    // addEventListener('change') is the modern API; older Safari used addListener.
    if (typeof mql.addEventListener === "function") {
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    }
    mql.addListener(onChange);
    return () => mql.removeListener(onChange);
  }, []);

  return isMobile;
}
