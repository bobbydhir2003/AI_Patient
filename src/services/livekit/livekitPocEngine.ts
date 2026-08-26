/**
 * LiveKit voice engine - powers both the admin POC page and (behind the
 * voice_engine feature flag) the real student InterviewPage.
 *
 * Deliberately bypasses the entire legacy playback stack: no MediaSource, no
 * Blob download/playback, no `new Audio()` per turn, no browser
 * speechSynthesis fallback. The whole point of this experiment is a SINGLE
 * persistent WebRTC remote-audio element, established once at room-join
 * time, reused for every patient turn - see the LiveKit feasibility audit.
 *
 * STT strategy (deliberately minimal): reuses the EXISTING browser
 * speech-recognition service unchanged (speechRecognitionService.ts) rather
 * than adding server-side STT. The recognized text is sent to the agent over
 * a LiveKit data message (topic "student_text") instead of the legacy
 * POST /api/interviews/{id}/messages call - the agent then calls the SAME
 * production patient-generation pipeline itself (see
 * backend/app/livekit_agent/patient_adapter.py). This engine is
 * specifically about PATIENT AUDIO delivery, not STT.
 *
 * Turn-boundary detection: a continuously-open WebRTC track has no natural
 * "clip ended" event, so THINKING -> SPEAKING -> LISTENING transitions are
 * driven by explicit "patient_turn_status" data messages from the agent
 * (see worker.py's _send_turn_status), not by inferring state from
 * HTMLMediaElement play/pause/ended events.
 *
 * Phase C protocol (production reliability): a confirmed production
 * incident showed the browser could enter LISTENING and publish a
 * student_text packet BEFORE the patient agent had joined the room -
 * LiveKit's reliable data delivery only applies to participants already
 * present, so the publish could resolve successfully while reaching zero
 * recipients, and the student's turn silently vanished. This engine now
 * implements a full agent-ready handshake + turn-delivery ACK + bounded
 * automatic retry protocol (see PocState/sendText/handleAgentControl below)
 * so recovery from a lost packet is entirely internal - there is
 * deliberately NO student-facing retry button, "Tap to hear patient", or
 * browser speechSynthesis fallback anywhere in this engine or its callers.
 *
 * Phase C2 protocol (mobile startup race): a confirmed production incident
 * showed room-connect -> await microphone -> wait-for-agent was wrongly
 * SERIALIZED - on iOS, the un-timed-out setMicrophoneEnabled(true) call
 * could hang indefinitely (leaving the UI stuck on "Requesting
 * microphone..." forever), and a real agent_ready arriving during that hang
 * was silently discarded (the old handler only accepted it while already
 * WAITING_FOR_AGENT, a state the engine never reached). Microphone
 * acquisition and the agent-ready wait now run INDEPENDENTLY after
 * room.connect() (see runMicrophoneAcquisition/armAgentReadyWatchdog) -
 * whichever finishes first is simply remembered (micReady/
 * agentReadyReceived), and maybeEnterListening() is the single place that
 * transitions to LISTENING once BOTH are true, order-independent:
 *   Case A: agent_ready arrives first -> remembered -> mic finishes later -> LISTENING
 *   Case B: mic finishes first -> remembered -> agent_ready arrives later -> LISTENING
 *   Case C: both finish close together -> transitions exactly once
 * Microphone acquisition is also now bounded (MIC_START_TIMEOUT_MS) with one
 * automatic internal retry - never a user-facing button - before surfacing
 * an explicit, distinctly-categorized ERROR. A startupGeneration counter
 * (bumped on every start()/end()) guards every async continuation involved
 * so a stale mic/agent-ready resolution from a previous attempt can never
 * mutate a newer one.
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
  | "waiting_for_agent"
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

// ---------------------------------------------------------------------------
// Timeouts - each stage of the protocol has its OWN bounded wait, so a
// failure can be attributed to exactly one of: agent never became ready,
// the student's turn was never delivered, the agent never finished
// processing it, or the patient's audio reply never completed. See
// emitError()'s `reason` argument at each call site below for how these map
// onto the "keep student-facing messages simple, but distinguish internally"
// requirement.
// ---------------------------------------------------------------------------

/** How long to wait for the agent's "agent_ready" control message after the
 * room connects and the mic publishes, before giving up. Generous because it
 * covers LiveKit job dispatch + a brand new agent OS process starting up
 * (see worker.py's module docstring: JobExecutorType.PROCESS). */
const AGENT_READY_TIMEOUT_MS = 20_000;

/** How long to wait for the agent's "turn_ack" control message after
 * publishing a student_text packet before treating it as lost and resending
 * (same clientTurnId) automatically. Short and bounded deliberately - an ACK
 * is a same-process, near-instant round trip once the agent is alive, unlike
 * the OpenAI/ElevenLabs round trip THINKING_TIMEOUT_MS below covers. */
const TURN_ACK_TIMEOUT_MS = 4_000;

/** Bounded number of automatic delivery retries (same clientTurnId) before
 * giving up and surfacing an error. Internal only - never a user-facing
 * "Retry" affordance (see the module docstring). */
const MAX_DELIVERY_RETRIES = 2;

/** How long to wait for the agent's "speaking_started" status after a
 * turn_ack before treating the turn as failed. Unchanged value from the
 * pre-Phase-C engine - covers the SAME OpenAI + ElevenLabs round-trip the
 * legacy path makes. Now armed only once delivery is confirmed (on ack),
 * not from the moment the packet was first sent - it should measure
 * PROCESSING time only, not delivery/handshake time. */
const THINKING_TIMEOUT_MS = 20_000;

/** How long to wait for "speaking_ended" after "speaking_started" before
 * treating the audio delivery itself as failed. A continuously-open WebRTC
 * track has no natural end-of-clip signal (see the module docstring), so
 * without this bound a lost "speaking_ended" status message would leave the
 * engine stuck in SPEAKING forever. */
const SPEAKING_TIMEOUT_MS = 45_000;

/** Phase C2: bounded wait for room.localParticipant.setMicrophoneEnabled(true)
 * to settle before treating it as stuck and retrying. A confirmed production
 * incident showed this call can hang indefinitely on iOS with no timeout at
 * all (the underlying getUserMedia() promise neither resolved nor rejected).
 * Deliberately short relative to AGENT_READY_TIMEOUT_MS (20s) so a hang is
 * detected and retried well before the agent-ready wait itself would time
 * out, while remaining generous enough that a real permission prompt or a
 * normal getUserMedia() call on a loaded mobile page still completes well
 * within it. Two attempts worst-case (this value x2) still finishes with
 * room to spare before AGENT_READY_TIMEOUT_MS. */
const MIC_START_TIMEOUT_MS = 7_000;

/** One bounded automatic retry (never a user-facing button) if the first
 * microphone attempt times out or rejects. Calling setMicrophoneEnabled(true)
 * again cannot create a second, overlapping getUserMedia() request: the SDK
 * (LocalParticipant.setTrackEnabled, inspected directly in
 * node_modules/livekit-client) already de-dupes concurrent enable calls for
 * the same source via its own pendingPublishing/pendingPublishPromises
 * bookkeeping - a still-pending first attempt is awaited/reused rather than
 * restarted, and a REJECTED first attempt is already fully cleaned up by the
 * SDK itself (it stops any partial track and clears its own pending-state
 * before rethrowing) - so no separate manual "unpublish partial track" step
 * is needed before this retry in either case. */
const MAX_MIC_RETRIES = 1;

const STUDENT_TEXT_TOPIC = "student_text";
const PATIENT_TURN_STATUS_TOPIC = "patient_turn_status";
const AGENT_CONTROL_TOPIC = "agent_control";

/** Injectable so this ONE engine can serve both the admin POC page and the
 * real student InterviewPage - each passes a function pointing at its own
 * token endpoint; the engine itself has no opinion on which. */
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

/** Admin POC token source (require_admin-gated) - the DEFAULT for
 * LiveKitPocEngine.start(), so LiveKitTestPage.tsx needs no changes at all. */
export function fetchAdminPocLiveKitToken(sessionId: string): Promise<LiveKitTokenResponse> {
  return postForToken(`${API_BASE_URL}/api/livekit/token`, sessionId);
}

/** Student-safe token source (require_session_access-gated) - the real
 * InterviewPage's token source (see useLiveKitInterviewVoice.ts). That
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

/** Control-plane messages the agent sends on AGENT_CONTROL_TOPIC - readiness
 * and turn-delivery acknowledgement. Distinct from patient_turn_status
 * (turn/audio lifecycle) so the two concerns can evolve independently. */
interface AgentControlPayload {
  type?: "agent_ready" | "turn_ack";
  clientTurnId?: string;
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

  // --- Turn-delivery/processing correlation (Phase C: split into two IDs so
  // "did the agent receive this" and "did the agent finish processing this"
  // can be tracked, timed out, and invalidated independently). ---
  private pendingDeliveryTurnId: string | null = null;
  private pendingProcessingTurnId: string | null = null;
  private deliveryRetryCount = 0;
  /** Wall-clock time the current turn's text was FIRST sent to the agent -
   * used only to compute duration_ms for diagnostics (real-device latency
   * validation), never persisted or sent anywhere but the telemetry ping. */
  private turnSentAt: number | null = null;

  private agentReadyWatchdog: number | null = null;
  private deliveryWatchdog: number | null = null;
  private thinkingWatchdog: number | null = null;
  private speakingWatchdog: number | null = null;
  private micTimeoutId: number | null = null;

  // --- Phase C2: order-independent startup readiness. Bumped on every
  // start()/end() call; every async continuation started during start()
  // (token fetch, room.connect(), microphone acquisition/retry, and every
  // room event handler registered inside start()) re-checks
  // isCurrentGeneration() before mutating instance state, so a late
  // resolution/event from a PREVIOUS attempt (a stale mic promise settling
  // after end(), or an old room's event firing after a new start()) can
  // never mutate the current engine. ---
  private startupGeneration = 0;
  private micReady = false;
  private agentReadyReceived = false;

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

  /** Every user-facing error path funnels through here so
   * livekit_engine_error is a true catch-all - a single event that, counted
   * alone, answers "how many LiveKit sessions hit ANY error" regardless of
   * which specific failure category it was. `reason` is one of the internal
   * failure categories (agent_not_ready / turn_delivery_failed /
   * turn_processing_timeout / audio_transport_failed / agent_turn_failed /
   * token_fetch_failed / room_connect_failed / room_disconnected /
   * recognition_unsupported / microphone_start_failed) - the student-facing
   * `message` stays simple. */
  private emitError(message: string, reason: string): void {
    logVoiceEvent("livekit_engine_error", { reason, engineState: this.state });
    this.callbacks.onError(message);
  }

  // --- Watchdog helpers: one pair per protocol stage. Each "clear" logs WHY
  // it was cancelled (except agent-ready/delivery, whose specific outcomes
  // are already covered by the dedicated Part 9 events logged at their call
  // sites) so the arm/cancel/fire lifecycle of every stage is reconstructable
  // from logs alone. ---

  private clearAgentReadyWatchdog(): void {
    if (this.agentReadyWatchdog !== null) {
      window.clearTimeout(this.agentReadyWatchdog);
      this.agentReadyWatchdog = null;
    }
  }

  private clearDeliveryWatchdog(): void {
    if (this.deliveryWatchdog !== null) {
      window.clearTimeout(this.deliveryWatchdog);
      this.deliveryWatchdog = null;
    }
  }

  private clearThinkingWatchdog(reason: string): void {
    if (this.thinkingWatchdog !== null) {
      window.clearTimeout(this.thinkingWatchdog);
      this.thinkingWatchdog = null;
      logVoiceEvent("livekit_thinking_timeout_cancelled", { reason, engineState: this.state });
    }
  }

  private clearSpeakingWatchdog(): void {
    if (this.speakingWatchdog !== null) {
      window.clearTimeout(this.speakingWatchdog);
      this.speakingWatchdog = null;
    }
  }

  private clearMicTimeout(): void {
    if (this.micTimeoutId !== null) {
      window.clearTimeout(this.micTimeoutId);
      this.micTimeoutId = null;
    }
  }

  /** True iff `generation` is still the CURRENT startup attempt and the
   * engine has not been told to end - the single guard every async
   * continuation started during start() must pass before mutating instance
   * state (see the startupGeneration field's own comment). */
  private isCurrentGeneration(generation: number): boolean {
    return generation === this.startupGeneration && !this.ended;
  }

  /**
   * The Start Interview gesture: join the room, then coordinate microphone
   * readiness and agent readiness INDEPENDENTLY of each other (Phase C2) -
   * all inside this one call/gesture, exactly once for the whole session.
   *
   * A confirmed production incident showed these two used to be wrongly
   * serialized (await mic BEFORE ever listening for agent_ready), which had
   * two consequences on iOS: (1) a hung, un-timed-out
   * setMicrophoneEnabled(true) left the UI stuck on "Requesting
   * microphone..." forever, and (2) a real agent_ready arriving during that
   * hang was silently discarded (the old handler only accepted it while
   * state === "waiting_for_agent", which was never reached). Neither
   * ordering is assumed correct anymore: both readiness signals are tracked
   * independently (micReady/agentReadyReceived) and LISTENING is entered
   * exactly once, by maybeEnterListening(), whichever signal finishes last.
   */
  async start(
    sessionId: string,
    fetchToken: FetchLiveKitToken = fetchAdminPocLiveKitToken,
  ): Promise<void> {
    if (this.state !== "idle" && this.state !== "ended" && this.state !== "error") return;
    this.ended = false;
    const generation = ++this.startupGeneration;
    this.micReady = false;
    this.agentReadyReceived = false;
    this.diagnostics = { ...INITIAL_DIAGNOSTICS };
    this.callbacks.onDiagnostics(this.diagnostics);
    this.setState("connecting");
    logVoiceEvent("livekit_room_connecting", {});

    let tokenInfo: LiveKitTokenResponse;
    try {
      tokenInfo = await fetchToken(sessionId);
    } catch {
      if (!this.isCurrentGeneration(generation)) return;
      this.emitError("Could not get a LiveKit connection token from the server.", "token_fetch_failed");
      this.setState("error");
      return;
    }
    if (!this.isCurrentGeneration(generation)) return;
    this.callbacks.onRoomName(tokenInfo.roomName);

    const room = new Room();
    this.room = room;

    room.on(RoomEvent.Disconnected, () => {
      if (!this.isCurrentGeneration(generation)) return;
      logVoiceEvent("livekit_room_disconnected", {});
      if (!this.ended) {
        this.emitError("The LiveKit room disconnected unexpectedly.", "room_disconnected");
        this.setState("error");
      }
    });
    room.on(RoomEvent.Reconnecting, () => {
      if (!this.isCurrentGeneration(generation)) return;
      logVoiceEvent("livekit_room_reconnecting", {});
      this.setState("reconnecting");
    });
    room.on(RoomEvent.Reconnected, () => {
      if (!this.isCurrentGeneration(generation)) return;
      logVoiceEvent("livekit_room_reconnected", {});
      this.setState("listening");
    });
    room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
      if (!this.isCurrentGeneration(generation)) return;
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
      // a genuine media error. Deliberately no fallback/UI prompt either way
      // (see the module docstring) - diagnostics only.
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
      if (!this.isCurrentGeneration(generation)) return;
      if (participant.identity !== AGENT_IDENTITY) return;
      logVoiceEvent("livekit_agent_started", {});
      this.patchDiagnostics({ agentConnected: true });
    });
    room.on(RoomEvent.DataReceived, (payload: Uint8Array, _participant, _kind, topic?: string) => {
      if (!this.isCurrentGeneration(generation)) return;
      if (topic === PATIENT_TURN_STATUS_TOPIC) {
        // Logged BEFORE any parsing/correlation filtering, so "did a
        // patient_turn_status message arrive at all" is answerable from logs
        // even when the payload is malformed or for a turn we've already
        // timed out - this is the critical arrival signal a prior production
        // incident found was previously invisible.
        logVoiceEvent("livekit_turn_status_received", { engineState: this.state });
        this.handleTurnStatus(payload);
        return;
      }
      if (topic === AGENT_CONTROL_TOPIC) {
        this.handleAgentControl(payload, generation);
      }
    });

    try {
      await room.connect(tokenInfo.url, tokenInfo.token);
    } catch {
      if (!this.isCurrentGeneration(generation)) return;
      this.emitError("Could not connect to the LiveKit room.", "room_connect_failed");
      this.setState("error");
      return;
    }
    if (!this.isCurrentGeneration(generation)) return;
    logVoiceEvent("livekit_room_connected", {});
    this.patchDiagnostics({ roomConnected: true });
    // The agent may already have joined before we did (or via ParticipantConnected above).
    if (room.remoteParticipants.has(AGENT_IDENTITY)) {
      logVoiceEvent("livekit_agent_started", {});
      this.patchDiagnostics({ agentConnected: true });
    }

    // Independent coordination (Phase C2): from this point, microphone
    // acquisition and the agent-ready wait race each other - neither blocks
    // the other from being detected or recorded, and maybeEnterListening()
    // transitions to LISTENING exactly once, whichever finishes last (Cases
    // A/B/C in the module docstring).
    this.setState("waiting_for_agent");
    this.armAgentReadyWatchdog(generation);
    void this.runMicrophoneAcquisition(generation, room);
  }

  /** Room connected is NOT sufficient proof the patient agent can receive
   * turns (see the module docstring's confirmed root cause) - wait for an
   * explicit "agent_ready" control message, bounded by
   * AGENT_READY_TIMEOUT_MS. Runs independently of (in parallel with)
   * microphone acquisition - see runMicrophoneAcquisition. */
  private armAgentReadyWatchdog(generation: number): void {
    this.clearAgentReadyWatchdog();
    this.agentReadyWatchdog = window.setTimeout(() => {
      this.agentReadyWatchdog = null;
      if (!this.isCurrentGeneration(generation)) return;
      if (this.agentReadyReceived) return; // already resolved; this firing is stale/moot
      if (this.state === "waiting_for_agent") {
        this.emitError(
          "The patient connection could not be established in time.",
          "agent_not_ready",
        );
        this.setState("error");
      }
    }, AGENT_READY_TIMEOUT_MS);
  }

  /**
   * Phase C2 single reconciliation point (requirement 8): the ONLY place
   * that transitions WAITING_FOR_AGENT -> LISTENING. Called from both the
   * agent_ready handler and the microphone-acquisition success path,
   * whichever runs last - the `this.state !== "waiting_for_agent"` guard
   * below is what guarantees the transition (and startRecognition()) happen
   * EXACTLY ONCE even if both signals resolve in the same tick (Case C).
   */
  private maybeEnterListening(generation: number): void {
    if (!this.isCurrentGeneration(generation)) return;
    if (this.state !== "waiting_for_agent") return; // already transitioned, or in error/ended
    if (!this.micReady || !this.agentReadyReceived) return;
    logVoiceEvent("livekit_startup_reconciled", { engineState: this.state, startupGeneration: generation });
    this.setState("listening");
    this.startRecognition();
  }

  private handleAgentControl(payload: Uint8Array, generation: number): void {
    let parsed: AgentControlPayload;
    try {
      parsed = JSON.parse(new TextDecoder().decode(payload)) as AgentControlPayload;
    } catch {
      return;
    }
    if (parsed.type === "agent_ready") {
      // Recorded UNCONDITIONALLY (as long as this is still the current
      // startup generation) - NEVER gated on the engine's current state.
      // This is the exact fix for the confirmed bug: a valid agent_ready
      // arriving while still "connecting" (mic acquisition in flight) is
      // now remembered instead of discarded, satisfying Case A.
      if (this.agentReadyReceived) return; // duplicate/late resend - already recorded
      this.agentReadyReceived = true;
      this.clearAgentReadyWatchdog();
      logVoiceEvent("livekit_agent_ready_received", { startupGeneration: generation });
      this.maybeEnterListening(generation);
      return;
    }
    if (parsed.type === "turn_ack") {
      this.handleTurnAck(parsed.clientTurnId);
    }
  }

  /** Runs microphone acquisition independently of the agent-ready wait
   * (Phase C2). Bounded by MIC_START_TIMEOUT_MS per attempt, with one
   * automatic internal retry (MAX_MIC_RETRIES) on a timeout or rejection -
   * never a user-facing button, never more than the bounded attempt count.
   * On success, records micReady and calls maybeEnterListening(); if every
   * attempt is exhausted, transitions to an explicit ERROR distinct from
   * every other failure category (never the generic "no response from the
   * agent" message, never a silent fallback). */
  private async runMicrophoneAcquisition(generation: number, room: Room): Promise<void> {
    for (let attempt = 0; attempt <= MAX_MIC_RETRIES; attempt += 1) {
      if (!this.isCurrentGeneration(generation)) return;
      logVoiceEvent("livekit_mic_request_started", { attempt, engineState: this.state });
      const startedAt = Date.now();
      const outcome = await this.attemptEnableMicrophone(room, generation);
      if (!this.isCurrentGeneration(generation)) return;
      const elapsedMs = Date.now() - startedAt;

      if (outcome.ok) {
        logVoiceEvent("livekit_mic_request_resolved", { attempt, engineState: this.state, durationMs: elapsedMs });
        logVoiceEvent("livekit_mic_published", {});
        this.patchDiagnostics({ micPublished: true });
        this.micReady = true;
        logVoiceEvent("livekit_mic_ready", { engineState: this.state, durationMs: elapsedMs, startupGeneration: generation });
        this.maybeEnterListening(generation);
        return;
      }

      if (outcome.reason === "timeout") {
        logVoiceEvent("livekit_mic_request_timeout", { attempt, engineState: this.state, durationMs: elapsedMs });
      } else {
        logVoiceEvent("livekit_mic_request_failed", {
          attempt, engineState: this.state, durationMs: elapsedMs, ...describeError(outcome.error),
        });
      }

      if (attempt < MAX_MIC_RETRIES) {
        logVoiceEvent("livekit_mic_retry_started", { attempt: attempt + 1, engineState: this.state });
      }
    }

    if (!this.isCurrentGeneration(generation)) return;
    // Exhausted retries. The underlying getUserMedia()/publish call can
    // never be forcibly cancelled - only asked to disable if/when it
    // eventually settles - so this is fire-and-forget, never awaited, and
    // never allowed to affect the ERROR transition below.
    room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
    this.emitError("Could not access your microphone.", "microphone_start_failed");
    this.setState("error");
  }

  /** One bounded microphone attempt: races setMicrophoneEnabled(true)
   * against MIC_START_TIMEOUT_MS. Never rejects - always resolves with a
   * discriminated outcome so the caller's retry loop stays simple.
   *
   * Every branch checks isCurrentGeneration(generation) BEFORE touching
   * `this.micTimeoutId` - not just before acting on the outcome. Without
   * this, a stale getUserMedia() promise from an ENDED attempt settling
   * late could call clearMicTimeout() and wipe out the timer belonging to a
   * BRAND NEW attempt started in the meantime (two different closures over
   * two different `settled` flags, but ONE shared `this.micTimeoutId`
   * field) - this is exactly the class of cross-generation bug this whole
   * mechanism exists to prevent. */
  private attemptEnableMicrophone(
    room: Room,
    generation: number,
  ): Promise<{ ok: true } | { ok: false; reason: "timeout" | "error"; error?: unknown }> {
    return new Promise((resolve) => {
      let settled = false;
      this.micTimeoutId = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        if (this.isCurrentGeneration(generation)) this.micTimeoutId = null;
        resolve({ ok: false, reason: "timeout" });
      }, MIC_START_TIMEOUT_MS);
      room.localParticipant.setMicrophoneEnabled(true).then(
        () => {
          if (settled) return;
          settled = true;
          if (this.isCurrentGeneration(generation)) this.clearMicTimeout();
          resolve({ ok: true });
        },
        (error: unknown) => {
          if (settled) return;
          settled = true;
          if (this.isCurrentGeneration(generation)) this.clearMicTimeout();
          resolve({ ok: false, reason: "error", error });
        },
      );
    });
  }

  private handleTurnAck(clientTurnId: string | undefined): void {
    if (!clientTurnId || clientTurnId !== this.pendingDeliveryTurnId) return; // stale/foreign ack
    this.clearDeliveryWatchdog();
    logVoiceEvent("livekit_turn_ack_received", { correlationId: clientTurnId, engineState: this.state });
    this.pendingDeliveryTurnId = null;
    this.deliveryRetryCount = 0;
    this.pendingProcessingTurnId = clientTurnId;
    this.armThinkingWatchdog(clientTurnId);
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
   * text through the exact same path (InterviewPage's typed chat input while
   * LiveKit mode is active - see useLiveKitInterviewVoice.ts). A no-op while
   * not "listening" (already waiting for the agent/thinking/speaking/etc.) -
   * the same guard the recognizer path relies on; this is also what
   * guarantees a turn can never be sent while WAITING_FOR_AGENT. Barge-in is
   * out of scope. */
  async sendText(text: string): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed || !this.room || this.state !== "listening") return;
    const clientTurnId = newClientTurnId();
    this.pendingDeliveryTurnId = clientTurnId;
    this.pendingProcessingTurnId = null;
    this.deliveryRetryCount = 0;
    this.turnSentAt = Date.now();
    this.setState("thinking");
    await this.publishStudentText(clientTurnId, trimmed);
  }

  /** Publishes (or re-publishes, on an automatic retry) the student_text
   * packet, targeted directly at the patient agent's identity (Phase C: no
   * more blind broadcast), and arms the delivery/ACK watchdog. Called both
   * by sendText() (first attempt) and by the delivery watchdog itself (retry
   * attempts) - always with the SAME clientTurnId. */
  private async publishStudentText(clientTurnId: string, text: string): Promise<void> {
    if (!this.room) return;
    logVoiceEvent("livekit_turn_publish_started", { correlationId: clientTurnId, engineState: this.state });
    this.armDeliveryWatchdog(clientTurnId, text);
    const payload = new TextEncoder().encode(JSON.stringify({ text, clientTurnId }));
    try {
      await this.room.localParticipant.publishData(payload, {
        reliable: true,
        topic: STUDENT_TEXT_TOPIC,
        destinationIdentities: [AGENT_IDENTITY],
      });
      logVoiceEvent("livekit_turn_publish_resolved", { correlationId: clientTurnId, engineState: this.state });
      // publishData resolving proves only that the LOCAL SDK accepted the
      // packet for send - NOT that the agent (or anyone) received it. Only
      // an explicit turn_ack (or, as a fallback, an unambiguous
      // patient_turn_status for this SAME clientTurnId - see
      // handleTurnStatus) counts as delivery confirmation.
    } catch {
      this.clearDeliveryWatchdog();
      this.pendingDeliveryTurnId = null;
      this.emitError("Could not deliver your message to the patient.", "turn_delivery_failed");
      this.setState("error");
    }
  }

  /** Arms the bounded ACK wait for one publish attempt. On timeout: resend
   * the SAME clientTurnId/text automatically (up to MAX_DELIVERY_RETRIES
   * times, entirely internally - no UI prompt, no retry button anywhere),
   * then give up with an explicit, internally-categorized error. */
  private armDeliveryWatchdog(clientTurnId: string, text: string): void {
    this.deliveryWatchdog = window.setTimeout(() => {
      this.deliveryWatchdog = null;
      if (this.pendingDeliveryTurnId !== clientTurnId) return; // already acked/invalidated
      logVoiceEvent("livekit_turn_ack_timeout", {
        correlationId: clientTurnId, engineState: this.state, attempt: this.deliveryRetryCount,
      });
      if (this.deliveryRetryCount < MAX_DELIVERY_RETRIES) {
        this.deliveryRetryCount += 1;
        logVoiceEvent("livekit_turn_auto_retry", {
          correlationId: clientTurnId, engineState: this.state, attempt: this.deliveryRetryCount,
        });
        void this.publishStudentText(clientTurnId, text);
      } else {
        this.pendingDeliveryTurnId = null;
        logVoiceEvent("livekit_turn_delivery_failed", {
          correlationId: clientTurnId, engineState: this.state, attempt: this.deliveryRetryCount,
        });
        this.emitError("Could not deliver your message to the patient.", "turn_delivery_failed");
        this.setState("error");
      }
    }, TURN_ACK_TIMEOUT_MS);
  }

  /** Arms the processing (OpenAI + TTS) timeout - unchanged
   * THINKING_TIMEOUT_MS value, now started only once delivery is confirmed
   * (see handleTurnAck/handleTurnStatus), so it measures processing time
   * only, not delivery/handshake time. */
  private armThinkingWatchdog(clientTurnId: string): void {
    this.clearThinkingWatchdog("rearm");
    logVoiceEvent("livekit_thinking_timeout_started", { correlationId: clientTurnId, engineState: "thinking" });
    this.thinkingWatchdog = window.setTimeout(() => {
      // Logged unconditionally: proves the raw timer actually fired,
      // independent of what the guard below decides to do with it.
      logVoiceEvent("livekit_thinking_timeout_fired", {
        correlationId: clientTurnId,
        engineState: this.state,
        turnStatus: this.pendingProcessingTurnId === clientTurnId ? "still_pending" : "already_resolved",
      });
      if (this.pendingProcessingTurnId === clientTurnId && this.state === "thinking") {
        logVoiceEvent("livekit_patient_audio_failed", { correlationId: clientTurnId, reason: "thinking_timeout" });
        // Proven bug fix (kept from the prior phase): invalidate the
        // timed-out turn's identity BEFORE transitioning to error, so a
        // late status message for this SAME clientTurnId can never silently
        // move the UI from ERROR back to SPEAKING.
        this.thinkingWatchdog = null;
        this.pendingProcessingTurnId = null;
        this.turnSentAt = null;
        this.emitError("Patient audio connection failed (no response from the agent).", "turn_processing_timeout");
        this.setState("error");
      }
    }, THINKING_TIMEOUT_MS);
  }

  /** Arms the speaking-lifecycle bound - see SPEAKING_TIMEOUT_MS. */
  private armSpeakingWatchdog(clientTurnId: string): void {
    this.clearSpeakingWatchdog();
    this.speakingWatchdog = window.setTimeout(() => {
      this.speakingWatchdog = null;
      if (this.pendingProcessingTurnId === clientTurnId && this.state === "speaking") {
        // Same invalidate-before-error discipline as the processing
        // watchdog above, applied to this SAME class of stale-state bug.
        this.pendingProcessingTurnId = null;
        this.turnSentAt = null;
        this.emitError("The patient's response could not be delivered.", "audio_transport_failed");
        this.setState("error");
      }
    }, SPEAKING_TIMEOUT_MS);
  }

  private handleTurnStatus(payload: Uint8Array): void {
    let parsed: TurnStatusPayload;
    try {
      parsed = JSON.parse(new TextDecoder().decode(payload)) as TurnStatusPayload;
    } catch {
      logVoiceEvent("livekit_turn_status_ignored", { reason: "parse_error", engineState: this.state });
      return;
    }
    const turnId = parsed.clientTurnId;
    const matchesProcessing = !!turnId && turnId === this.pendingProcessingTurnId;
    // A patient_turn_status for a turn we're still awaiting the ACK for is
    // still a legitimate, strong signal the agent received it - treat it as
    // an implicit delivery confirmation (see below) rather than ignoring it
    // purely because the ack itself was lost in transit.
    const matchesDelivery = !!turnId && turnId === this.pendingDeliveryTurnId;
    if (!turnId || (!matchesProcessing && !matchesDelivery)) {
      // Stale (already timed-out/completed) or foreign turn.
      logVoiceEvent("livekit_turn_status_ignored", {
        reason: "client_turn_id_mismatch",
        correlationId: turnId || undefined,
        turnStatus: parsed.status,
        engineState: this.state,
      });
      return;
    }
    logVoiceEvent("livekit_turn_status_matched", {
      correlationId: turnId, turnStatus: parsed.status, engineState: this.state,
    });

    if (matchesDelivery && !matchesProcessing) {
      this.clearDeliveryWatchdog();
      logVoiceEvent("livekit_turn_ack_received", { correlationId: turnId, engineState: this.state });
      this.pendingDeliveryTurnId = null;
      this.deliveryRetryCount = 0;
      this.pendingProcessingTurnId = turnId;
    }

    if (parsed.status === "speaking_started") {
      this.clearThinkingWatchdog("speaking_started");
      this.armSpeakingWatchdog(turnId);
      // Time from the student's transcript being sent to the agent's first
      // audio signal - "total speech-to-first-patient-audio latency" for
      // real-device validation (see the Phase 1 validation plan).
      const timeToFirstAudioMs = this.turnSentAt ? Date.now() - this.turnSentAt : undefined;
      logVoiceEvent("livekit_patient_audio_started", {
        correlationId: turnId,
        durationMs: timeToFirstAudioMs,
      });
      this.setState("speaking");
      return;
    }
    if (parsed.status === "speaking_ended") {
      this.clearSpeakingWatchdog();
      const totalTurnMs = this.turnSentAt ? Date.now() - this.turnSentAt : undefined;
      logVoiceEvent("livekit_patient_audio_completed", {
        correlationId: turnId,
        durationMs: totalTurnMs,
      });
      this.turnSentAt = null;
      this.turnCount += 1;
      this.pendingProcessingTurnId = null;
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
      this.clearSpeakingWatchdog();
      logVoiceEvent("livekit_patient_audio_failed", { correlationId: turnId, reason: "agent_failed" });
      this.pendingProcessingTurnId = null;
      this.turnSentAt = null;
      // Explicit diagnostic state - deliberately NOT a silent fallback to
      // legacy browser TTS (see the module docstring's "no hidden fallback"
      // requirement).
      this.emitError("Patient audio generation failed for this turn.", "agent_turn_failed");
      this.setState("error");
      return;
    }
    // Matched this turn's clientTurnId, but the status value itself is
    // unrecognized (future/malformed agent payload) - distinct from a parse
    // error or a mismatched turn, and deliberately NOT treated as a failure:
    // the turn simply stays "thinking" until either a recognized status
    // arrives or the relevant watchdog fires.
    logVoiceEvent("livekit_turn_status_ignored", {
      reason: "unsupported_status",
      correlationId: turnId,
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
    // Invalidate any outstanding startup work (Phase C2): a mic attempt or
    // agent-ready watchdog still in flight from this (or an even earlier)
    // start() call must never mutate state after end() - isCurrentGeneration()
    // is what every such continuation checks before acting.
    this.startupGeneration += 1;
    this.micReady = false;
    this.agentReadyReceived = false;
    this.clearMicTimeout();
    this.clearAgentReadyWatchdog();
    this.clearDeliveryWatchdog();
    this.clearThinkingWatchdog("engine_end");
    this.clearSpeakingWatchdog();
    this.pendingDeliveryTurnId = null;
    this.pendingProcessingTurnId = null;
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
