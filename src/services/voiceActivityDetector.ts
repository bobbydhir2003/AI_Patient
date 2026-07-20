/**
 * Voice-activity detector used ONLY while the patient is speaking AND
 * automatic interruption is enabled. It measures microphone audio level
 * (RMS) and feeds a pure InterruptionJudge - it never transcribes audio and
 * never creates patient text.
 *
 * Echo safety: the judge ignores everything during a protection window after
 * TTS actually starts, then samples the speaker-echo level to build a dynamic
 * baseline, and only fires on SUSTAINED sound above hysteresis thresholds.
 */
import {
  createInterruptionJudge,
  getJudgeProfile,
  type AudioSetup,
  type InterruptionSensitivity,
} from "./interruptionJudge";

export type { AudioSetup, InterruptionSensitivity } from "./interruptionJudge";

const FRAME_MS = 50;
const DEV_LOG_EVERY_MS = 1000;

function rms(analyser: AnalyserNode, buffer: Uint8Array<ArrayBuffer>): number {
  analyser.getByteTimeDomainData(buffer);
  let sum = 0;
  for (let i = 0; i < buffer.length; i += 1) {
    const centered = (buffer[i] - 128) / 128;
    sum += centered * centered;
  }
  return Math.sqrt(sum / buffer.length);
}

export interface MonitorOptions {
  sensitivity: InterruptionSensitivity;
  audioSetup: AudioSetup;
  onSustainedVoice: () => void;
}

export interface VoiceActivityDetector {
  /** Sample ambient noise (used as the judge's initial baseline floor). */
  calibrate: (ms?: number) => Promise<number>;
  /** Arm the detector. Call from the TTS `onstart` event so the protection
   * window begins when speech playback actually starts. Fires at most once
   * per arming. */
  startMonitoring: (options: MonitorOptions) => void;
  stopMonitoring: () => void;
  dispose: () => void;
}

export function createVoiceActivityDetector(stream: MediaStream): VoiceActivityDetector | null {
  const AudioContextCtor =
    (window as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext })
      .AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextCtor) return null;

  const audioContext = new AudioContextCtor();
  const source = audioContext.createMediaStreamSource(stream);
  const analyser = audioContext.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const buffer = new Uint8Array(new ArrayBuffer(analyser.fftSize));

  let ambientBaseline = 0.008;
  let intervalId: number | null = null;
  let disposed = false;

  function stopMonitoring() {
    if (intervalId !== null) {
      window.clearInterval(intervalId);
      intervalId = null;
    }
  }

  return {
    async calibrate(ms = 600): Promise<number> {
      if (disposed) return ambientBaseline;
      if (audioContext.state === "suspended") await audioContext.resume();
      const samples: number[] = [];
      const frames = Math.max(4, Math.floor(ms / FRAME_MS));
      for (let i = 0; i < frames; i += 1) {
        samples.push(rms(analyser, buffer));
        await new Promise((r) => window.setTimeout(r, FRAME_MS));
      }
      samples.sort((a, b) => a - b);
      ambientBaseline = samples[Math.floor(samples.length / 2)]; // median
      return ambientBaseline;
    },

    startMonitoring({ sensitivity, audioSetup, onSustainedVoice }: MonitorOptions) {
      if (disposed) return;
      stopMonitoring();
      const profile = getJudgeProfile(audioSetup, sensitivity);
      const judge = createInterruptionJudge(profile, performance.now(), ambientBaseline);
      let fired = false;
      let lastLogAt = 0;

      if (audioContext.state === "suspended") void audioContext.resume();
      intervalId = window.setInterval(() => {
        if (fired) return;
        const now = performance.now();
        const level = rms(analyser, buffer);
        const verdict = judge.feed(now, level);

        if (import.meta.env.DEV && now - lastLogAt > DEV_LOG_EVERY_MS) {
          lastLogAt = now;
          const snap = judge.snapshot();
          console.debug("[VAD]", {
            audioSetup,
            sensitivity,
            verdict,
            phase: snap.phase,
            baselineRms: Number(snap.baseline.toFixed(4)),
            currentRms: Number(level.toFixed(4)),
            startThreshold: Number(snap.startThreshold.toFixed(4)),
            continueThreshold: Number(snap.continueThreshold.toFixed(4)),
            sustainedMs: Math.round(snap.sustainedMs),
          });
        }

        if (verdict === "interrupt") {
          fired = true; // fires exactly once per arming
          stopMonitoring();
          if (import.meta.env.DEV) console.debug("[VAD] interruption confirmed");
          onSustainedVoice();
        }
      }, FRAME_MS);
    },

    stopMonitoring,

    dispose() {
      disposed = true;
      stopMonitoring();
      try {
        source.disconnect();
      } catch {
        // already disconnected
      }
      void audioContext.close().catch(() => {});
    },
  };
}
