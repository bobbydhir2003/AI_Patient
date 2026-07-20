import type { SyntheticEvent } from "react";

const DEFAULT_FALLBACK = "/avatars/default-user.webp";

interface AppImageProps {
  src: string;
  alt: string;
  className?: string;
  loading?: "lazy" | "eager";
  /** Image to swap in on load failure. Pass null to disable fallback swapping. */
  fallbackSrc?: string | null;
}

export function AppImage({
  src,
  alt,
  className,
  loading = "lazy",
  fallbackSrc = DEFAULT_FALLBACK,
}: AppImageProps) {
  function handleError(event: SyntheticEvent<HTMLImageElement>) {
    if (!fallbackSrc || event.currentTarget.src.endsWith(fallbackSrc)) return;
    event.currentTarget.src = fallbackSrc;
  }

  return (
    <img
      src={src}
      alt={alt}
      className={className}
      loading={loading}
      onError={handleError}
    />
  );
}
