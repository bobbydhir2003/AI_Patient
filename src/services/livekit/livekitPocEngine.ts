/**
 * Phase 1 LiveKit POC engine - NOT used by the production interview path.
 *
 * Deliberately bypasses the entire legacy playback stack: no MediaSource, no
 * Blob download/playback, no `new Audio()` per turn, no browser
 * speechSynthesis fallback. The whole point of this experiment is a SINGLE
 * persistent WebRTC remote-audio element, established once at room-join
 * time, reused for every patient turn - see the LiveKit feasibility audit.
 *
 * STT strategy (Phase 1, deliberately minimal): reuses the EXISTING browser
 * speech-recognition service unchanged (speechRecognitionService.ts) rather
 * than adding server-side STT. The recognized text is sent to the agent over
 * a LiveKit data message (topic "student_text") instead of the legacy
 * POST /api/interviews/{id}/messages call - the agent then calls the SAME
 * production patient-generation pipeline itself (see
 * backend/app/livekit_agent/patient_adapter.py). This experiment is
 * specifically about PATIENT AUDIO delivery, not STT.
 *
 * Turn-boundary detection: a continuously-open WebRTC track has no natural
 * "clip ended" event, so THINKING -> SPEAKING -> LISTENING transitions are
 * driven by explicit "patient_turn_status" data messages from the agent
 * (see worker.py's _send_turn_status), not by inferring state from
 * HTMLMediaElement play/pause/ended events.
 *
 * NEVER logs patient text, transcript content, audio bytes, the LiveKit
 * token, or API secrets - only the same safe metadata voiceDiagnostics.ts
 * already restricts itself to.
 */
import { Room, RoomEvent, Track, type RemoteParticipant, type RemoteTrack } from "livekit-client";
import { API_BASE_URL, withAuthHeaders } from "../api";
import { createRecognizer, type Recognizer } from "../speechRecognitionService";
import { describeError, logVoiceEvent } from "../voiceDiagnostics";

export type PocState =
  | "idle"
  | "connecting"
  | "listening"
  | "thinking"
  | "speaking"
  | "reconnecting"
  | "error"
  | "ended";

export interface LiveKitTokenResponse {
  token: string;
  url: string;
  roomName: string;
  participantIdentity: string;
}

/** How long to wait for the agent's "speaking_started" status after sending
 * student text before treating the turn as failed. Generous because it
 * covers the SAME OpenAI + ElevenLabs round-trip the legacy path makes. */
const THINKING_TIMEOUT_MS = 20_000;

/** Injectable so this ONE engine can serve both the admin POC page and the
 * real student InterviewPage (Phase B) - each passes a function pointing at
 * its own token endpoint; the engine itself has no opinion on which. */
export type FetchLiveKitToken = (sessionId: string) => Promise<LiveKitTokenResponse>;

async function postForToken(url: string, sessionId: string): Promise<LiveKitTokenResponse> {
  const response = await fetch(url, {
    method: "POST",
    headers: withAuthHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ sessionId }),
  });
  if (!response.ok) {
    throw new Error(`livekit_token_http_${response.status}`);
  }
  return (await response.json()) as LiveKitTokenResponse;
}

/** Phase 1/2 admin POC token source (require_admin-gated) - the DEFAULT for
 * LiveKitPocEngine.start(), so LiveKitTestPage.tsx needs no changes at all. */
export function fetchAdminPocLiveKitToken(sessionId: string): Promise<LiveKitTokenResponse> {
  return postForToken(`${API_BASE_URL}/api/livekit/token`, sessionId);
}

/** Phase A/B student-safe token source (require_session_access-gated) - the
 * real InterviewPage's token source (see useLiveKitInterviewVoice.ts). That
 * endpoint takes session_id from the URL path, not the body - the body sent
 * here is simply unread server-side, kept only for a uniform call shape. */
export function fetchStudentLiveKitToken(sessionId: string): Promise<LiveKitTokenResponse> {
  return postForToken(
    `${API_BASE_URL}/api/interviews/${encodeURIComponent(sessionId)}/livekit-token`,
    sessionId,
  );
}

interface TurnStatusPayload {
  clientTurnId?: string;
  status?: "speaking_started" | "speaking_ended" | "failed";
}

/** Coarse connection-milestone flags for the POC's diagnostic panel only -
 * mirrors the SAME milestones already sent to voiceDiagnostics.ts, just also
 * surfaced synchronously to the page. Never used to drive turn state. */
export interface PocDiagnostics {
  roomConnected: boolean;
  micPublished: boolean;
  patientTrackSubscribed: boolean;
  agentConnected: boolean;
}

const INITIAL_DIAGNOSTICS: PocDiagnostics = {
  roomConnected: false,
  micPublished: false,
  patientTrackSubscribed: false,
  agentConnected: false,
};

export interface LiveKitPocCallbacks {
  onStateChange: (state: PocState) => void;
  /** Recognized student speech (final results only forwarded as a turn). */
  onStudentTranscript: (text: string, isFinal: boolean) => void;
  onError: (message: string) => void;
  onTurnCompleted: (turnCount: number) => void;
  onDiagnostics: (diagnostics: PocDiagnostics) => void;
  /** The server-derived room name (see livekit_token_service.poc_room_name) -
   * surfaced so the tester can copy the EXACT value into the agent worker's
   * --room flag rather than reconstruct it by hand. */
  onRoomName: (roomName: string) => void;
}

/** Agent process's fixed participant identity (see worker.py AGENT_IDENTITY). */
const AGENT_IDENTITY = "patient-agent";

function newClientTurnId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `turn-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export class LiveKitPocEngine {
  private room: Room | null = null;
  private recognizer: Recognizer | null = null;
  private state: PocState = "idle";
  private turnCount = 0;
  private audioEl: HTMLMediaElement | null = null;
  private pendingClientTurnId: string | null = null;
  /** Wall-clock time the current turn's text was sent to the agent - used
   * only to compute duration_ms for diagnostics (real-device latency
   * validation), never persisted or sent anywhere but the telemetry ping. */
  private turnSentAt: number | null = null;
  private thinkingWatchdog: number | null = null;
  private ended = false;
  private diagnostics: PocDiagnostics = { ...INITIAL_DIAGNOSTICS };
  private readonly callbacks: LiveKitPocCallbacks;

  constructor(callbacks: LiveKitPocCallbacks) {
    this.callbacks = callbacks;
  }

  private patchDiagnostics(patch: Partial<PocDiagnostics>): void {
    this.diagnostics = { ...this.diagnostics, ...patch };
    this.callbacks.onDiagnostics(this.diagnostics);
  }

  getState(): PocState {
    return this.state;
  }

  getTurnCount(): number {
    return this.turnCount;
  }

  private setState(next: PocState): void {
    this.state = next;
    this.callbacks.onStateChange(next);
  }

  /** Cancels the armed thinking-timeout watchdog, if any, and logs WHY it was
   * cancelled (e.g. "speaking_started", "turn_failed", "engine_end") - the
   * counterpart to livekit_thinking_timeout_started, so the full arm/cancel/
   * fire lifecycle of every turn's watchdog is reconstructable from logs
   * alone. A no-op (and no log) when nothing was armed. */
  private clearThinkingWatchdog(reason: string): void {
    if (this.thinkingWatchdog !== null) {
      window.clearTimeout(this.thinkingWatchdog);
      this.thinkingWatchdog = null;
      logVoiceEvent("livekit_thinking_timeout_cancelled", { reason, engineState: this.state });
    }
  }

  /** Every user-facing error path funnels through here so
   * livekit_engine_error is a true catch-all - a single event that, counted
   * alone, answers "how many LiveKit sessions hit ANY error" regardless of
   * which specific failure category it was. */
  private emitError(message: string, reason: string): void {
    logVoiceEvent("livekit_engine_error", { reason, engineState: this.state });
    this.callbacks.onError(message);
  }

  /**
   * The Start Interview gesture: join the room, publish the microphone, and
   * begin listening - all inside this one call/gesture, exactly once for the
   * whole session. Everything after this is automatic.
   */
  async start(
    sessionId: string,
    fetchToken: FetchLiveKitToken = fetchAdminPocLiveKitToken,
  ): Promise<void> {
    if (this.state !== "idle" && this.state !== "ended" && this.state !== "error") return;
    this.ended = false;
    this.diagnostics = { ...INITIAL_DIAGNOSTICS };
    this.callbacks.onDiagnostics(this.diagnostics);
    this.setState("connecting");
    logVoiceEvent("livekit_room_connecting", {});

    let tokenInfo: LiveKitTokenResponse;
    try {
      tokenInfo = await fetchToken(sessionId);
    } catch {
      this.emitError("Could not get a LiveKit connection token from the server.", "token_fetch_failed");
      this.setState("error");
      return;
    }
    this.callbacks.onRoomName(tokenInfo.roomName);

    const room = new Room();
    this.room = room;

    room.on(RoomEvent.Disconnected, () => {
      logVoiceEvent("livekit_room_disconnected", {});
      if (!this.ended) {
        this.emitError("The LiveKit room disconnected unexpectedly.", "room_disconnected");
        this.setState("error");
      }
    });
    room.on(RoomEvent.Reconnecting, () => {
      logVoiceEvent("livekit_room_reconnecting", {});
      this.setState("reconnecting");
    });
    room.on(RoomEvent.Reconnected, () => {
      logVoiceEvent("livekit_room_reconnected", {});
      this.setState("listening");
    });
    room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
      if (track.kind !== Track.Kind.Audio) return;
      logVoiceEvent("livekit_patient_track_subscribed", {});
      // ONE persistent element for this remote track's ENTIRE lifetime - it
      // is never recreated for subsequent turns, unlike the legacy engine's
      // per-turn new Audio(). This is the structural property the experiment
      // is testing.
      const el = track.attach();
      el.autoplay = true;
      this.audioEl = el;
      this.patchDiagnostics({ patientTrackSubscribed: true });
      logVoiceEvent("livekit_audio_element_attached", {});

      // 'error' fires for real media errors (decode/network) - it does NOT
      // fire for an autoplay-policy rejection, which surfaces only as a
      // rejected play() promise (handled below). Both are logged separately
      // so a "browser silently blocked audio" report is distinguishable from
      // a genuine media error.
      el.onerror = () => {
        logVoiceEvent("livekit_audio_play_failed", {
          reason: "media_error",
          errorMessage: el.error ? `code_${el.error.code}` : undefined,
        });
      };

      const playResult = el.play();
      if (playResult && typeof playResult.then === "function") {
        playResult
          .then(() => logVoiceEvent("livekit_audio_playing", {}))
          .catch((err: unknown) => {
            logVoiceEvent("livekit_audio_play_failed", { reason: "play_rejected", ...describeError(err) });
          });
      }
    });
    room.on(RoomEvent.ParticipantConnected, (participant: RemoteParticipant) => {
      if (participant.identity !== AGENT_IDENTITY) return;
      logVoiceEvent("livekit_agent_started", {});
      this.patchDiagnostics({ agentConnected: true });
    });
    room.on(RoomEvent.DataReceived, (payload: Uint8Array, _participant, _kind, topic?: string) => {
      if (topic !== "patient_turn_status") return;
      // Logged BEFORE any parsing/correlation filtering, so "did a
      // patient_turn_status message arrive at all" is answerable from logs
      // even when the payload is malformed or for a turn we've already
      // timed out - this is the critical arrival signal the 4-device
      // incident inspection found was previously invisible.
      logVoiceEvent("livekit_turn_status_received", { engineState: this.state });
      this.handleTurnStatus(payload);
    });

    try {
      await room.connect(tokenInfo.url, tokenInfo.token);
      logVoiceEvent("livekit_room_connected", {});
      this.patchDiagnostics({ roomConnected: true });
      // The agent may already have joined before we did (or via ParticipantConnected above).
      if (room.remoteParticipants.has(AGENT_IDENTITY)) {
        logVoiceEvent("livekit_agent_started", {});
        this.patchDiagnostics({ agentConnected: true });
      }
      await room.localParticipant.setMicrophoneEnabled(true);
      logVoiceEvent("livekit_mic_published", {});
      this.patchDiagnostics({ micPublished: true });
    } catch {
      this.emitError(
        "Could not connect to the LiveKit room or publish the microphone.",
        "room_connect_failed",
      );
      this.setState("error");
      return;
    }

    this.setState("listening");
    this.startRecognition();
  }

  /** Abort and clear the current recognizer, if any. A spent
   * BrowserSpeechRecognition instance (createRecognizer's "finalDelivered"
   * latch fires at most once per instance - see speechRecognitionService.ts)
   * must never be reused for a later turn; this is always called before a
   * fresh one is created. */
  private stopRecognition(): void {
    try {
      this.recognizer?.abort();
    } catch {
      /* ignore */
    }
    this.recognizer = null;
  }

  /** Every LISTENING cycle gets a BRAND NEW recognizer - exactly the pattern
   * useVoiceConversation.ts's startListening() already uses in production.
   * Never restart a spent instance: browser SpeechRecognition delivers at
   * most one final result per instance, so calling .start() again on the
   * SAME object would silently swallow every future turn's transcript (this
   * is the turn-2-never-recognized bug this fixes). */
  private startRecognition(): void {
    this.stopRecognition();
    this.recognizer = createRecognizer({
      onInterim: (text) => this.callbacks.onStudentTranscript(text, false),
      onFinal: (text) => {
        this.callbacks.onStudentTranscript(text, true);
        void this.sendText(text);
      },
      onError: () => {
        // Non-fatal: the recognizer restarts itself via onEnd below, matching
        // the legacy useVoiceConversation.ts pattern (one-shot recognition,
        // auto-restart while still LISTENING).
      },
      onEnd: () => {
        // Browser one-shot recognition ended without a usable final result
        // (e.g. silence/no-speech) - keep listening with a FRESH recognizer,
        // not by restarting this (now-spent) instance. Debounced like
        // useVoiceConversation.ts's onEnd restart, to avoid a tight
        // start/end loop if the browser ends instantly with no speech.
        if (this.state === "listening") {
          window.setTimeout(() => {
            if (this.state === "listening") this.startRecognition();
          }, 150);
        }
      },
    });
    if (!this.recognizer) {
      this.emitError("This browser does not support speech recognition.", "recognition_unsupported");
      return;
    }
    try {
      this.recognizer.start();
    } catch {
      // start() throws (InvalidStateError) if a session is already active -
      // bounded single retry, mirroring useVoiceConversation.ts's
      // startListening() catch block. Re-checks state so a turn that has
      // already moved on (e.g. straight to "error") does not retry forever.
      window.setTimeout(() => {
        if (this.state === "listening") this.startRecognition();
      }, 250);
    }
  }

  /** Send one turn of student text to the agent - called internally by the
   * recognizer's onFinal, and PUBLICLY by a caller wanting to submit typed
   * text through the exact same path (Phase B: InterviewPage's typed chat
   * input while LiveKit mode is active - see useLiveKitInterviewVoice.ts).
   * A no-op while not "listening" (already thinking/speaking/etc.) - the
   * same guard the recognizer path relies on; barge-in is out of scope. */
  async sendText(text: string): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed || !this.room || this.state !== "listening") return;
    const clientTurnId = newClientTurnId();
    this.pendingClientTurnId = clientTurnId;
    this.turnSentAt = Date.now();
    this.setState("thinking");
    this.clearThinkingWatchdog("new_turn_started");
    logVoiceEvent("livekit_thinking_timeout_started", { correlationId: clientTurnId, engineState: "thinking" });
    this.thinkingWatchdog = window.setTimeout(() => {
      // Logged unconditionally: proves the raw timer actually fired,
      // independent of what the guard below decides to do with it.
      logVoiceEvent("livekit_thinking_timeout_fired", {
        correlationId: clientTurnId,
        engineState: this.state,
        turnStatus: this.pendingClientTurnId === clientTurnId ? "still_pending" : "already_resolved",
      });
      if (this.pendingClientTurnId === clientTurnId && this.state === "thinking") {
        logVoiceEvent("livekit_patient_audio_failed", { correlationId: clientTurnId, reason: "thinking_timeout" });
        // Proven bug fix: invalidate the timed-out turn's identity BEFORE
        // transitioning to error. Without this, a late "speaking_started"
        // for this SAME clientTurnId would still pass handleTurnStatus's
        // correlation check (parsed.clientTurnId === this.pendingClientTurnId)
        // and silently move the UI from ERROR back to SPEAKING after the
        // student already saw the error message.
        this.thinkingWatchdog = null;
        this.pendingClientTurnId = null;
        this.turnSentAt = null;
        this.emitError("Patient audio connection failed (no response from the agent).", "thinking_timeout");
        this.setState("error");
      }
    }, THINKING_TIMEOUT_MS);

    logVoiceEvent("livekit_first_turn_sent", { correlationId: clientTurnId, engineState: "thinking" });
    const payload = new TextEncoder().encode(JSON.stringify({ text: trimmed, clientTurnId }));
    try {
      await this.room.localParticipant.publishData(payload, { reliable: true, topic: "student_text" });
    } catch {
      this.clearThinkingWatchdog("publish_failed");
      this.emitError("Could not send your message to the patient agent.", "publish_data_failed");
      this.setState("error");
    }
  }

  private handleTurnStatus(payload: Uint8Array): void {
    let parsed: TurnStatusPayload;
    try {
      parsed = JSON.parse(new TextDecoder().decode(payload)) as TurnStatusPayload;
    } catch {
      logVoiceEvent("livekit_turn_status_ignored", { reason: "parse_error", engineState: this.state });
      return;
    }
    if (!parsed.clientTurnId || parsed.clientTurnId !== this.pendingClientTurnId) {
      // Stale (already timed-out/completed) or foreign turn. This is the
      // exact case the timeout-state fix above makes possible to observe
      // safely: a late "speaking_started" for an already-timed-out
      // clientTurnId now lands here (pendingClientTurnId was cleared) instead
      // of being able to match and resurrect a finished/errored turn.
      logVoiceEvent("livekit_turn_status_ignored", {
        reason: "client_turn_id_mismatch",
        correlationId: parsed.clientTurnId || undefined,
        turnStatus: parsed.status,
        engineState: this.state,
      });
      return;
    }
    logVoiceEvent("livekit_turn_status_matched", {
      correlationId: parsed.clientTurnId,
      turnStatus: parsed.status,
      engineState: this.state,
    });

    if (parsed.status === "speaking_started") {
      this.clearThinkingWatchdog("speaking_started");
      // Time from the student's transcript being sent to the agent's first
      // audio signal - "total speech-to-first-patient-audio latency" for
      // real-device validation (see the Phase 1 validation plan).
      const timeToFirstAudioMs = this.turnSentAt ? Date.now() - this.turnSentAt : undefined;
      logVoiceEvent("livekit_patient_audio_started", {
        correlationId: parsed.clientTurnId,
        durationMs: timeToFirstAudioMs,
      });
      this.setState("speaking");
      return;
    }
    if (parsed.status === "speaking_ended") {
      const totalTurnMs = this.turnSentAt ? Date.now() - this.turnSentAt : undefined;
      logVoiceEvent("livekit_patient_audio_completed", {
        correlationId: parsed.clientTurnId,
        durationMs: totalTurnMs,
      });
      this.turnSentAt = null;
      this.turnCount += 1;
      this.pendingClientTurnId = null;
      this.callbacks.onTurnCompleted(this.turnCount);
      this.setState("listening");
      // Resume listening with a FRESH recognizer - this was the missing
      // call: nothing else restarts recognition after a turn completes (see
      // the turn-2-never-recognized bug this fixes).
      this.startRecognition();
      return;
    }
    if (parsed.status === "failed") {
      this.clearThinkingWatchdog("turn_failed");
      logVoiceEvent("livekit_patient_audio_failed", { correlationId: parsed.clientTurnId, reason: "agent_failed" });
      this.pendingClientTurnId = null;
      this.turnSentAt = null;
      // Explicit POC diagnostic state - deliberately NOT a silent fallback to
      // legacy browser TTS (see the POC's "no hidden fallback" requirement).
      this.emitError("Patient audio generation failed for this turn.", "agent_turn_failed");
      this.setState("error");
      return;
    }
    // Matched this turn's clientTurnId, but the status value itself is
    // unrecognized (future/malformed agent payload) - distinct from a parse
    // error or a mismatched turn, and deliberately NOT treated as a failure:
    // the turn simply stays "thinking" until either a recognized status
    // arrives or the watchdog fires.
    logVoiceEvent("livekit_turn_status_ignored", {
      reason: "unsupported_status",
      correlationId: parsed.clientTurnId,
      turnStatus: parsed.status,
      engineState: this.state,
    });
  }

  /** The End Interview gesture: tears everything down cleanly - mirrors
   * patientVoiceService.ts's cancelPatientSpeech()/stopActivePlayback()
   * discipline (stop pending work, detach media, never leave a dangling
   * recognizer or a stale "speaking" state). */
  async end(): Promise<void> {
    this.ended = true;
    this.clearThinkingWatchdog("engine_end");
    this.pendingClientTurnId = null;
    this.turnSentAt = null;
    this.stopRecognition();
    if (this.audioEl) {
      this.audioEl.pause();
      this.audioEl = null;
    }
    if (this.room) {
      await this.room.disconnect();
      this.room = null;
    }
    this.diagnostics = { ...INITIAL_DIAGNOSTICS };
    this.callbacks.onDiagnostics(this.diagnostics);
    this.setState("ended");
  }
}
