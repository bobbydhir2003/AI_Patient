import { useEffect, useState } from "react";

interface InterviewTimerProps {
  startTime: number | null;
  onTick?: (elapsedSeconds: number) => void;
}

function formatElapsed(totalSeconds: number): string {
  const safe = Math.max(0, totalSeconds);
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60)
    .toString()
    .padStart(2, "0");
  const seconds = (safe % 60).toString().padStart(2, "0");
  return hours > 0 ? `${hours}:${minutes}:${seconds}` : `${minutes}:${seconds}`;
}

export function InterviewTimer({ startTime, onTick }: InterviewTimerProps) {
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  useEffect(() => {
    if (!startTime) {
      setElapsedSeconds(0);
      return;
    }
    setElapsedSeconds(Math.floor((Date.now() - startTime) / 1000));

    const intervalId = window.setInterval(() => {
      const elapsed = Math.floor((Date.now() - startTime) / 1000);
      setElapsedSeconds(elapsed);
      onTick?.(elapsed);
    }, 1000);

    return () => window.clearInterval(intervalId);
  }, [startTime, onTick]);

  return (
    <span aria-label="Time elapsed">{formatElapsed(elapsedSeconds)}</span>
  );
}
