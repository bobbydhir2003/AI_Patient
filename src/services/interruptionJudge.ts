/**
 * Pure interruption-decision logic (no browser APIs) so it can be unit-tested
 * in Node. The voiceActivityDetector feeds it timestamped microphone RMS
 * levels while the patient is speaking; the judge decides if the sound is
 * speaker echo (ignore) or sustained nearby student speech (interrupt).
 *
 * Phases per patient utterance:
 *   1. protection  - ignore everything (speaker audio just started)
 *   2. sampling    - measure the speaker-echo level to build a baseline
 *   3. active      - hysteresis thresholds derived from the echo baseline;
 *                    sustained sound above the start threshold interrupts.
 *
 * The echo baseline keeps updating slowly (EMA) from sub-threshold frames so
 * volume drift during long utterances doesn't invalidate it.
 */

export type AudioSetup = "speakers" | "headphones";
export type InterruptionSensitivity = "low" | "medium" | "high";

export interface JudgeProfile {
  /** ignore everything for this long after TTS actually starts (ms) */
  protectionMs: number;
  /** after protection, sample echo for this long to build the baseline (ms) */
  echoSampleMs: number;
  /** start threshold = max(baseline * multiplier + margin, minThreshold) */
  multiplier: number;
  margin: number;
  minThreshold: number;
  /** continue threshold = start threshold * continueRatio (hysteresis) */
  continueRatio: number;
  /** sound must stay above thresholds for this long to interrupt (ms) */
  sustainMs: number;
  /** slow EMA factor for baseline updates from sub-threshold frames */
  baselineAlpha: number;
}

/** All tuning lives here so values can be adjusted in one place. */
export const JUDGE_PROFILES: Record<
  AudioSetup,
  Record<InterruptionSensitivity, JudgeProfile>
> = {
  speakers: {
    low: base("speakers", 3.2, 900),
    medium: base("speakers", 2.8, 750),
    high: base("speakers", 2.4, 600),
  },
  headphones: {
    low: base("headphones", 2.4, 700),
    medium: base("headphones", 2.0, 550),
    high: base("headphones", 1.7, 400),
  },
};

function base(setup: AudioSetup, multiplier: number, sustainMs: number): JudgeProfile {
  const speakers = setup === "speakers";
  return {
    protectionMs: speakers ? 1500 : 700,
    echoSampleMs: speakers ? 700 : 500,
    multiplier,
    margin: speakers ? 0.012 : 0.008,
    minThreshold: speakers ? 0.03 : 0.02,
    continueRatio: 0.75,
    sustainMs,
    baselineAlpha: 0.05,
  };
}

export function getJudgeProfile(
  setup: AudioSetup,
  sensitivity: InterruptionSensitivity,
): JudgeProfile {
  return JUDGE_PROFILES[setup][sensitivity];
}

export type JudgeVerdict =
  | "protected" // inside the initial protection window
  | "sampling" // building the echo baseline
  | "quiet" // active, below start threshold, not tracking
  | "tracking" // active, above threshold but not yet sustained
  | "interrupt"; // sustained student voice confirmed (fires exactly once)

export interface InterruptionJudge {
  /** Feed one microphone level sample. `nowMs` must be monotonic. */
  feed: (nowMs: number, rms: number) => JudgeVerdict;
  /** Debug snapshot for development telemetry. */
  snapshot: () => {
    phase: "protection" | "sampling" | "active" | "fired";
    baseline: number;
    startThreshold: number;
    continueThreshold: number;
    sustainedMs: number;
  };
}

export function createInterruptionJudge(
  profile: JudgeProfile,
  armTimeMs: number,
  ambientBaseline = 0,
): InterruptionJudge {
  let phase: "protection" | "sampling" | "active" | "fired" = "protection";
  let baseline = ambientBaseline;
  const echoSamples: number[] = [];
  let trackingSinceMs: number | null = null;
  let lastSampleMs = armTimeMs;

  function thresholds() {
    const start = Math.max(baseline * profile.multiplier + profile.margin, profile.minThreshold);
    return { start, cont: start * profile.continueRatio };
  }

  return {
    feed(nowMs: number, rms: number): JudgeVerdict {
      if (phase === "fired") return "interrupt";
      lastSampleMs = nowMs;
      const elapsed = nowMs - armTimeMs;

      if (elapsed < profile.protectionMs) {
        return "protected"; // nothing accumulates during protection
      }

      if (elapsed < profile.protectionMs + profile.echoSampleMs) {
        phase = "sampling";
        echoSamples.push(rms);
        return "sampling";
      }

      if (phase !== "active") {
        phase = "active";
        if (echoSamples.length) {
          echoSamples.sort((a, b) => a - b);
          const median = echoSamples[Math.floor(echoSamples.length / 2)];
          baseline = Math.max(median, ambientBaseline);
        }
      }

      const { start, cont } = thresholds();

      if (trackingSinceMs === null) {
        if (rms >= start) {
          trackingSinceMs = nowMs;
          return "tracking";
        }
        // Slow rolling update: sub-threshold frames refine the echo baseline.
        baseline = baseline * (1 - profile.baselineAlpha) + rms * profile.baselineAlpha;
        return "quiet";
      }

      // Already tracking: hysteresis - keep tracking while above the lower
      // continuation threshold; reset if it drops below.
      if (rms < cont) {
        trackingSinceMs = null;
        return "quiet";
      }
      if (nowMs - trackingSinceMs >= profile.sustainMs) {
        phase = "fired";
        return "interrupt";
      }
      return "tracking";
    },

    snapshot() {
      const { start, cont } = thresholds();
      return {
        phase,
        baseline,
        startThreshold: start,
        continueThreshold: cont,
        sustainedMs: trackingSinceMs === null ? 0 : lastSampleMs - trackingSinceMs,
      };
    },
  };
}
