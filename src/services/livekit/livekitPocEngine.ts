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
  | "interrupting"
  | "reconnecting"
  | "error"
  | "ended";

export interface LiveKitTokenResponse {
  token: string;
  url: string;
  roomName: string;
  participantIdentity: string;
  /** Phase C3: the server-generated UUID4 baked into the student room name
   * (see backend livekit_token_service.student_room_name) - "" or absent for
   * the admin POC path, whose room stays deterministic. Used ONLY for
   * telemetry/log correlation, never for any protocol/state decision - the
   * engine already treats `roomName` as fully opaque. */
  connectionId?: string;
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

/** Phase D2: how long to wait for the worker's "interrupted" acknowledgment
 * (patient_turn_status) after sending interrupt_patient before giving up and
 * returning to LISTENING anyway. Comparable to TURN_ACK_TIMEOUT_MS - this is
 * also a same-process, near-instant round trip once the agent is alive (no
 * OpenAI/ElevenLabs call is on this path), so it stays short rather than
 * stranding the student in INTERRUPTING. Never tears down the room/worker on
 * timeout - see interruptPatient()'s own docstring. */
const INTERRUPT_ACK_TIMEOUT_MS = 4_000;

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
// Phase G (Realtime engine only): agent->browser transcript-sync events (see
// worker.py TRANSCRIPT_SYNC_TOPIC). Additive - a legacy session never receives
// these, so the browser-recognizer path below is completely unaffected.
const TRANSCRIPT_SYNC_TOPIC = "transcript_sync";

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
  /** Phase D2: "interrupted" is the worker's explicit acknowledgment that a
   * SPEAKING-only interrupt_patient request was honored (see worker.py's
   * _on_interrupt_patient) - reuses this SAME channel/correlation-by-
   * clientTurnId mechanism as speaking_started/speaking_ended/failed rather
   * than inventing a second acknowledgment path. */
  status?: "speaking_started" | "speaking_ended" | "failed" | "interrupted";
}

/** Control-plane messages the agent sends on AGENT_CONTROL_TOPIC - readiness
 * and turn-delivery acknowledgement. Distinct from patient_turn_status
 * (turn/audio lifecycle) so the two concerns can evolve independently.
 *
 * Phase 4 (EXPERIMENTAL semantic turn control - see worker.py's
 * PocAgentSession): three additions, all additive/backward-compatible -
 * an older engine build simply never reads them and keeps today's
 * browser-authoritative behavior.
 *   - "agent_ready" gains `semanticTurnControl`: true only when the
 *     backend's Smart Turn HOLD/END decision - not browser
 *     SpeechRecognition - is authoritative for this session's student
 *     turn completion (see handleAgentControl/semanticTurnControlActive).
 *   - "semantic_turn_started": the server decided (Smart Turn END) that a
 *     real patient turn is now processing - the ONE new event this phase
 *     adds; everything after this (speaking_started/speaking_ended/failed)
 *     reuses the EXISTING patient_turn_status protocol unchanged, keyed by
 *     the SAME clientTurnId (see handleSemanticTurnStarted).
 *   - "semantic_fallback": one-way runtime downgrade - the semantic
 *     pipeline became unhealthy server-side, browser SpeechRecognition
 *     resumes being authoritative for the rest of the session.
 *   - "turn_ack" gains `semanticIgnored`: true only when this ack covers a
 *     browser-originated (non-manual-override) packet the agent received
 *     but will NOT process because semantic control is authoritative for
 *     this session - see worker.py's _send_turn_ack/_on_data and
 *     handleTurnAck below. Turn-ID sync fix: without this, the engine had
 *     no way to distinguish "the agent will process this" from "the agent
 *     merely received this," and would claim "thinking"/arm the processing
 *     watchdog for an id that could never resolve, permanently missing the
 *     REAL semantic_turn_started that follows for a different id.
 */
interface AgentControlPayload {
  type?: "agent_ready" | "turn_ack" | "semantic_turn_started" | "semantic_fallback";
  clientTurnId?: string;
  semanticTurnControl?: boolean;
  semanticIgnored?: boolean;
  reason?: string;
}

/** Phase G transcript-sync events (Realtime engine only). Every event carries
 * the backend generation `epoch` so a stale/out-of-order event from a
 * superseded generation is dropped (see handleTranscriptSync). */
interface TranscriptSyncPayload {
  type?: "student_transcript" | "patient_text_ready" | "patient_text_final";
  clientTurnId?: string;
  epoch?: number;
  patientTurnId?: string;
  text?: string;
  reason?: string;
}

export interface PatientTextMeta {
  clientTurnId?: string;
  patientTurnId?: string;
  final: boolean;
  reason?: string;
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
  /** Phase G (Realtime engine only): backend-APPROVED patient text for a turn,
   * delivered before/at speech start (`final:false`) and reconciled after
   * completion/interruption (`final:true`, with `reason`). OPTIONAL so every
   * existing caller/test is unaffected; a legacy session never fires it. */
  onPatientText?: (
    text: string,
    meta: PatientTextMeta,
  ) => void;
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
  /** Phase G: highest transcript-sync generation epoch seen. A transcript_sync
   * event with a LOWER epoch is a straggler from a superseded generation and
   * is dropped (see handleTranscriptSync). Starts below any real epoch (0).
   * Reset per connection in start() so a NEW Realtime worker session (which
   * begins again at epoch 0/1) is never rejected by a stale high watermark
   * from a previous Start/Stop/Resume cycle. */
  private latestSyncEpoch = -1;
  /** P0-2: the clientTurnId of the current AUTHORITATIVE Realtime voice turn,
   * learned from a transcript_sync `patient_text_ready` (server-authoritative,
   * id `realtime-<session>-<n>`). Lets handleTurnStatus correlate speaking_*
   * events for a Realtime turn WITHOUT a browser-created SpeechRecognition
   * clientTurnId (which never exists for a spoken turn). Null in legacy mode
   * (no transcript_sync arrives), so legacy turn-ID correlation is untouched. */
  private realtimeActiveTurn: {
    clientTurnId: string;
    patientTurnId: string;
    epoch: number;
  } | null = null;
  /** Phase 4 (EXPERIMENTAL): learned from agent_ready's additive
   * `semanticTurnControl` field, then a ONE-WAY flag for the lifetime of
   * this engine instance - a later "semantic_fallback" message can flip it
   * true->false (never back), mirroring the backend's own one-way
   * PocAgentSession._semantic_control_active. While true, a browser
   * SpeechRecognition FINAL is diagnostic-only (see startRecognition's
   * onFinal) - it never calls sendText()/moves the UI to "thinking"; the
   * server drives that via "semantic_turn_started" instead (see
   * handleSemanticTurnStarted). Reset to false in end(). */
  private semanticTurnControlActive = false;
  /** Wall-clock time the current turn's text was FIRST sent to the agent -
   * used only to compute duration_ms for diagnostics (real-device latency
   * validation), never persisted or sent anywhere but the telemetry ping. */
  private turnSentAt: number | null = null;

  private agentReadyWatchdog: number | null = null;
  private deliveryWatchdog: number | null = null;
  private thinkingWatchdog: number | null = null;
  private speakingWatchdog: number | null = null;
  private micTimeoutId: number | null = null;
  /** Phase D2: bounded wait for the worker's "interrupted" ack after
   * interruptPatient() sends interrupt_patient - see armInterruptWatchdog. */
  private interruptWatchdog: number | null = null;

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

  /** Phase C3: the current voice connection's server-generated id (see
   * LiveKitTokenResponse.connectionId) - telemetry-only, threaded through
   * relevant logVoiceEvent() calls so a "stuck restart" report can be
   * correlated to exactly one Start attempt in logs. Never used for any
   * protocol/state decision. Reset to null in end(). */
  private connectionId: string | null = null;

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
    logVoiceEvent("livekit_engine_error", {
      reason, engineState: this.state, connectionId: this.connectionId ?? undefined,
    });
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

  private clearInterruptWatchdog(): void {
    if (this.interruptWatchdog !== null) {
      window.clearTimeout(this.interruptWatchdog);
      this.interruptWatchdog = null;
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
    this.semanticTurnControlActive = false;
    // P0-2/session reset: transcript-sync epoch + authoritative Realtime turn
    // are scoped to THIS connection. A fresh worker session starts at a low
    // epoch, so the watermark must not survive from a previous session.
    this.latestSyncEpoch = -1;
    this.realtimeActiveTurn = null;
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
    this.connectionId = tokenInfo.connectionId ?? null;
    logVoiceEvent("livekit_voice_connection_created", {
      connectionId: this.connectionId ?? undefined, engineState: this.state,
    });
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
        return;
      }
      if (topic === TRANSCRIPT_SYNC_TOPIC) {
        this.handleTranscriptSync(payload);
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
    logVoiceEvent("livekit_room_connected", { connectionId: this.connectionId ?? undefined });
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
      // Phase 4: recorded once, at the same point agent_ready itself is
      // recorded - this is the value startRecognition()'s onFinal reads for
      // every subsequent recognizer cycle this session.
      this.semanticTurnControlActive = parsed.semanticTurnControl === true;
      logVoiceEvent("livekit_agent_ready_received", {
        startupGeneration: generation, connectionId: this.connectionId ?? undefined,
        semanticTurnControlActive: this.semanticTurnControlActive,
      });
      this.maybeEnterListening(generation);
      return;
    }
    if (parsed.type === "turn_ack") {
      this.handleTurnAck(parsed.clientTurnId, parsed.semanticIgnored === true);
      return;
    }
    if (parsed.type === "semantic_turn_started") {
      this.handleSemanticTurnStarted(parsed.clientTurnId);
      return;
    }
    if (parsed.type === "semantic_fallback") {
      // Step 11: one-way - browser SpeechRecognition resumes being
      // authoritative for the rest of this session (never flips back true).
      if (!this.semanticTurnControlActive) return; // already off - no-op
      this.semanticTurnControlActive = false;
      logVoiceEvent("livekit_semantic_fallback_received", {
        engineState: this.state, reason: parsed.reason ?? "unknown",
      });
    }
  }

  /** Phase 4 (Step 8): the server decided (Smart Turn END) that a real
   * patient turn is now processing - the counterpart to sendText() for a
   * SERVER-originated turn (no prior browser publish, so no delivery/ack
   * phase - this goes straight to "processing"). Everything downstream
   * (speaking_started/speaking_ended/failed) reuses handleTurnStatus
   * unchanged, correlated by this SAME clientTurnId. Ignored while not
   * "listening" (already mid-turn, or stale/duplicate) - mirrors sendText's
   * own guard so a late/duplicate semantic_turn_started can never clobber
   * an already-in-flight turn. */
  private handleSemanticTurnStarted(clientTurnId: string | undefined): void {
    if (!clientTurnId || this.state !== "listening") return;
    logVoiceEvent("livekit_semantic_turn_started_received", {
      correlationId: clientTurnId, engineState: this.state,
    });
    // No delivery phase for a server-originated turn - go straight to
    // "processing", matching handleTurnAck's own effect on these fields.
    this.pendingDeliveryTurnId = null;
    this.deliveryRetryCount = 0;
    this.pendingProcessingTurnId = clientTurnId;
    this.turnSentAt = Date.now();
    this.setState("thinking");
    this.armThinkingWatchdog(clientTurnId);
    // Stop the currently-listening recognizer explicitly - it would end on
    // its own shortly anyway (continuous=false), but its onEnd's own
    // restart-while-listening check would otherwise race against the
    // state flip above happening on the SAME tick.
    this.stopRecognition();
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
        logVoiceEvent("livekit_mic_ready", {
          engineState: this.state, durationMs: elapsedMs, startupGeneration: generation,
          connectionId: this.connectionId ?? undefined,
        });
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

  /** Turn-ID sync fix: `semanticIgnored` means the agent received this
   * browser-originated packet but will NEVER process it or send any further
   * message for this clientTurnId (semantic control is authoritative for
   * this session - see worker.py's _on_data/_send_turn_ack). Claiming
   * "thinking"/arming the processing watchdog for such an id would wait
   * forever for a patient_turn_status that can never arrive, and - worse -
   * leaves `state` stuck at "thinking" so the REAL semantic_turn_started
   * that follows (a different clientTurnId) gets silently dropped by
   * handleSemanticTurnStarted's own state==="listening" guard. Returning to
   * "listening" here (and resuming recognition, for captions) is what lets
   * that follow-up message be adopted cleanly. */
  private handleTurnAck(clientTurnId: string | undefined, semanticIgnored: boolean): void {
    if (!clientTurnId || clientTurnId !== this.pendingDeliveryTurnId) return; // stale/foreign ack
    this.clearDeliveryWatchdog();
    this.pendingDeliveryTurnId = null;
    this.deliveryRetryCount = 0;
    if (semanticIgnored) {
      logVoiceEvent("livekit_turn_ack_semantic_ignored", { correlationId: clientTurnId, engineState: this.state });
      this.pendingProcessingTurnId = null;
      this.turnSentAt = null;
      if (this.state === "thinking") {
        this.setState("listening");
        this.startRecognition();
      }
      return;
    }
    logVoiceEvent("livekit_turn_ack_received", { correlationId: clientTurnId, engineState: this.state });
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
      onInterim: (text) => {
        if (this.state !== "listening") return; // stale recognizer instance - see onFinal below
        this.callbacks.onStudentTranscript(text, false);
      },
      onFinal: (text) => {
        if (this.state !== "listening") return; // already moved on (e.g. a semantic turn just started)
        this.callbacks.onStudentTranscript(text, true);
        // Turn-ID sync fix: the semantic-control suppression now lives
        // centrally in sendText() itself (source: "speech_browser"), not
        // here - this guarantees ANY caller passing that source is
        // protected, not just this one call site. No manual recognizer
        // restart needed either way: Web Speech's continuous=false means
        // onEnd fires right after every final regardless, and it already
        // restarts whenever state is still "listening" - exactly the case
        // here, since a suppressed call never touches state.
        void this.sendText(text, { source: "speech_browser" });
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
   * recognizer's onFinal (source: "speech_browser"), and PUBLICLY by a
   * caller wanting to submit typed text through the exact same path
   * (InterviewPage's typed chat input while LiveKit mode is active - see
   * useLiveKitInterviewVoice.ts's submitExternal, source: "manual_typed").
   * A no-op while not "listening" (already waiting for the agent/thinking/
   * speaking/etc.) - the same guard the recognizer path relies on; this is
   * also what guarantees a turn can never be sent while WAITING_FOR_AGENT.
   * See interruptPatient() below for the separate SPEAKING-only barge-in
   * path - sending typed/spoken text is never itself how a patient turn is
   * interrupted.
   *
   * Turn-ID sync fix: `options.source` defaults to "manual_typed" - the
   * NEVER-suppressed source - so every pre-existing caller that doesn't pass
   * it (tests, any future external caller) keeps today's unconditional-send
   * behavior with zero changes required there. Only a caller that
   * deliberately opts into "speech_browser" (currently just onFinal above)
   * gets the new suppression: while semantic turn control is active, a
   * "speech_browser" source is dropped here - centrally, not just in
   * onFinal - matching worker.py's own source-aware ignore logic (_on_data):
   * a browser SpeechRecognition final is never authoritative once the
   * server's Deepgram + Smart Turn pipeline governs turn completion for this
   * session. "manual_typed" is a deliberate student action (the Send
   * button), not a race-prone recognizer guess, and is NEVER suppressed
   * here - the backend's TurnSource.MANUAL_OVERRIDE exempts it from its own
   * ignore-path too, so typed Send keeps working exactly as before whether
   * or not semantic control is on for this session. */
  async sendText(
    text: string,
    options: { source: "speech_browser" | "manual_typed" } = { source: "manual_typed" },
  ): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed || !this.room || this.state !== "listening") return;
    if (options.source === "speech_browser" && this.semanticTurnControlActive) {
      // The server's Deepgram + Smart Turn pipeline is authoritative here -
      // do NOT publish (that would move the UI to "thinking" for a turn the
      // backend will just ignore). Captions already happened in onFinal
      // before this call, independent of this guard.
      logVoiceEvent("livekit_browser_final_ignored_semantic_control", { engineState: this.state });
      return;
    }
    const clientTurnId = newClientTurnId();
    this.pendingDeliveryTurnId = clientTurnId;
    this.pendingProcessingTurnId = null;
    this.deliveryRetryCount = 0;
    this.turnSentAt = Date.now();
    this.setState("thinking");
    await this.publishStudentText(clientTurnId, trimmed, options.source);
  }

  /** Publishes (or re-publishes, on an automatic retry) the student_text
   * packet, targeted directly at the patient agent's identity (Phase C: no
   * more blind broadcast), and arms the delivery/ACK watchdog. Called both
   * by sendText() (first attempt) and by the delivery watchdog itself (retry
   * attempts) - always with the SAME clientTurnId AND source. `source` rides
   * along in the payload so worker.py's _on_data can apply its own
   * source-aware semantic-ignore/manual-override logic - see sendText's
   * docstring. */
  private async publishStudentText(
    clientTurnId: string,
    text: string,
    source: "speech_browser" | "manual_typed",
  ): Promise<void> {
    if (!this.room) return;
    logVoiceEvent("livekit_turn_publish_started", { correlationId: clientTurnId, engineState: this.state });
    this.armDeliveryWatchdog(clientTurnId, text, source);
    const payload = new TextEncoder().encode(JSON.stringify({ text, clientTurnId, source }));
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
  private armDeliveryWatchdog(
    clientTurnId: string,
    text: string,
    source: "speech_browser" | "manual_typed",
  ): void {
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
        void this.publishStudentText(clientTurnId, text, source);
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

  /**
   * Phase D2: true SPEAKING-only interruption ("barge-in") - stops the
   * patient's CURRENT turn mid-playback, on the SAME room/connectionId/
   * worker job (never ends the room, never mints a new token - that
   * distinction is what separates this from stopConversation()/end()).
   *
   * Deliberately a no-op outside "speaking": the worker's OpenAI/ElevenLabs
   * calls run in a thread pool it cannot forcibly stop (see worker.py's
   * PocAgentSession docstring) - offering "interrupt" during THINKING would
   * only fake a cancellation while the provider call keeps running and
   * billing/consuming time in the background. Once audio is actually
   * publishing (SPEAKING), cancelling is genuinely effective: the worker
   * stops publishing further frames and clears anything already queued.
   *
   * Never awaited by callers (mirrors stopConversation()'s fire-and-forget
   * shape) - the bounded interrupt-ack watchdog below is what guarantees
   * this always resolves back to LISTENING, whether or not the worker's
   * acknowledgment ever arrives.
   */
  interruptPatient(): void {
    if (this.state !== "speaking") return;
    const clientTurnId = this.pendingProcessingTurnId;
    if (!clientTurnId || !this.room) return;
    this.clearSpeakingWatchdog();
    this.setState("interrupting");
    logVoiceEvent("livekit_interrupt_requested", { correlationId: clientTurnId, engineState: this.state });
    const payload = new TextEncoder().encode(
      JSON.stringify({ type: "interrupt_patient", clientTurnId }),
    );
    this.room.localParticipant
      .publishData(payload, { reliable: true, topic: AGENT_CONTROL_TOPIC, destinationIdentities: [AGENT_IDENTITY] })
      .catch((err: unknown) => {
        // The local SDK couldn't even send it - logged for diagnostics only;
        // the armed watchdog below still recovers to LISTENING on its own
        // bounded timeout rather than needing a separate failure path here.
        logVoiceEvent("livekit_interrupt_failed", {
          correlationId: clientTurnId, engineState: this.state, reason: "publish_failed", ...describeError(err),
        });
      });
    this.armInterruptWatchdog(clientTurnId);
  }

  /** Bounded wait for the worker's "interrupted" ack (see handleTurnStatus).
   * On timeout: never strand the student in INTERRUPTING and never tear down
   * the room/worker just because one acknowledgment was lost - fall back to
   * LISTENING with a fresh recognizer, exactly like any other bounded
   * recovery path in this engine. */
  private armInterruptWatchdog(clientTurnId: string): void {
    this.clearInterruptWatchdog();
    this.interruptWatchdog = window.setTimeout(() => {
      this.interruptWatchdog = null;
      if (this.state !== "interrupting") return; // already resolved (ack, natural completion, Stop/reset)
      logVoiceEvent("livekit_interrupt_failed", {
        correlationId: clientTurnId, engineState: this.state, reason: "ack_timeout",
      });
      this.pendingProcessingTurnId = null;
      this.turnSentAt = null;
      this.setState("listening");
      this.startRecognition();
    }, INTERRUPT_ACK_TIMEOUT_MS);
  }

  /** Phase G (Realtime engine only): render the backend-authoritative student
   * and patient text promptly, dropping any straggler from a superseded
   * generation via the monotonic epoch. Never touches the legacy
   * recognizer/turn-state machinery - it only forwards to display callbacks. */
  private handleTranscriptSync(payload: Uint8Array): void {
    let parsed: TranscriptSyncPayload;
    try {
      parsed = JSON.parse(new TextDecoder().decode(payload)) as TranscriptSyncPayload;
    } catch {
      logVoiceEvent("livekit_transcript_sync_ignored", { reason: "parse_error" });
      return;
    }
    if (
      parsed.type !== "student_transcript" &&
      parsed.type !== "patient_text_ready" &&
      parsed.type !== "patient_text_final"
    ) {
      logVoiceEvent("livekit_transcript_sync_ignored", { reason: "unsupported_type" });
      return;
    }
    if (!parsed.clientTurnId) {
      logVoiceEvent("livekit_transcript_sync_ignored", { reason: "missing_client_turn_id" });
      return;
    }
    if (parsed.type !== "student_transcript" && !parsed.patientTurnId) {
      logVoiceEvent("livekit_transcript_sync_ignored", {
        reason: "missing_patient_turn_id",
        correlationId: parsed.clientTurnId,
      });
      return;
    }
    const epoch = parsed.epoch;
    if (typeof epoch !== "number" || !Number.isSafeInteger(epoch) || epoch < 0) {
      logVoiceEvent("livekit_transcript_sync_ignored", { reason: "invalid_epoch" });
      return;
    }
    if (epoch < this.latestSyncEpoch) {
      // Straggler from an invalidated generation - stale patient/student text
      // must never overwrite the current turn.
      logVoiceEvent("livekit_transcript_sync_stale_dropped", {
        correlationId: parsed.clientTurnId,
      });
      return;
    }
    this.latestSyncEpoch = epoch;
    const text = parsed.text ?? "";
    if (parsed.type === "student_transcript") {
      this.callbacks.onStudentTranscript(text, true);
      return;
    }
    if (parsed.type === "patient_text_ready") {
      // P0-2: this is the authoritative id for the Realtime voice turn whose
      // speaking_started/ended will follow on patient_turn_status. Recording it
      // here is what lets handleTurnStatus accept those events (the browser
      // never created this id).
      this.realtimeActiveTurn = {
        clientTurnId: parsed.clientTurnId,
        patientTurnId: parsed.patientTurnId!,
        epoch,
      };
      // Realtime turns do not have a browser-created delivery/ack phase. Adopt
      // the backend identity as the current processing identity so the normal
      // speaking watchdog and manual-interrupt path remain fully functional.
      this.pendingProcessingTurnId = parsed.clientTurnId;
      this.callbacks.onPatientText?.(text, {
        clientTurnId: parsed.clientTurnId,
        patientTurnId: parsed.patientTurnId,
        final: false,
      });
      return;
    }
    if (parsed.type === "patient_text_final") {
      this.callbacks.onPatientText?.(text, {
        clientTurnId: parsed.clientTurnId,
        patientTurnId: parsed.patientTurnId,
        final: true,
        reason: parsed.reason,
      });
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
    const turnId = parsed.clientTurnId;
    const matchesProcessing = !!turnId && turnId === this.pendingProcessingTurnId;
    // A patient_turn_status for a turn we're still awaiting the ACK for is
    // still a legitimate, strong signal the agent received it - treat it as
    // an implicit delivery confirmation (see below) rather than ignoring it
    // purely because the ack itself was lost in transit.
    const matchesDelivery = !!turnId && turnId === this.pendingDeliveryTurnId;
    // P0-2: a server-authoritative Realtime voice turn (id learned from
    // transcript_sync patient_text_ready). Only ever non-null in Realtime mode,
    // so this NEVER weakens the legacy browser-owned clientTurnId correlation.
    const matchesRealtime = !!turnId && turnId === this.realtimeActiveTurn?.clientTurnId;
    if (!turnId || (!matchesProcessing && !matchesDelivery && !matchesRealtime)) {
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
      this.clearInterruptWatchdog();
      const totalTurnMs = this.turnSentAt ? Date.now() - this.turnSentAt : undefined;
      logVoiceEvent("livekit_patient_audio_completed", {
        correlationId: turnId,
        durationMs: totalTurnMs,
      });
      this.turnSentAt = null;
      this.turnCount += 1;
      this.pendingProcessingTurnId = null;
      this.realtimeActiveTurn = null;  // P0-2: turn done; ignore any late duplicate status
      this.callbacks.onTurnCompleted(this.turnCount);
      this.setState("listening");
      // Resume listening with a FRESH recognizer - this was the missing
      // call: nothing else restarts recognition after a turn completes (see
      // the turn-2-never-recognized bug this fixes).
      this.startRecognition();
      return;
    }
    if (parsed.status === "interrupted") {
      // Phase D2: the worker's explicit ack that our interrupt_patient
      // request was honored (see worker.py's _on_interrupt_patient). Reuses
      // the SAME turn-terminal cleanup as speaking_ended (a text turn
      // already exists in the transcript regardless of how much audio
      // played - see generate_and_persist_turn, called before any audio
      // publish begins) rather than inventing separate bookkeeping.
      // Race-safe by construction: if speaking_ended instead won the race
      // against our interrupt, pendingProcessingTurnId is already null by
      // the time this arrives, so the earlier turnId-mismatch check above
      // already rejected it before reaching here (requirement 17.A).
      this.clearThinkingWatchdog("interrupted");
      this.clearSpeakingWatchdog();
      this.clearInterruptWatchdog();
      logVoiceEvent("livekit_interrupt_completed", { correlationId: turnId, engineState: this.state });
      this.turnSentAt = null;
      this.turnCount += 1;
      this.pendingProcessingTurnId = null;
      this.realtimeActiveTurn = null;  // P0-2: interrupted turn resolved
      this.callbacks.onTurnCompleted(this.turnCount);
      this.setState("listening");
      this.startRecognition();
      return;
    }
    if (parsed.status === "failed") {
      this.clearThinkingWatchdog("turn_failed");
      this.clearSpeakingWatchdog();
      this.clearInterruptWatchdog();
      logVoiceEvent("livekit_patient_audio_failed", { correlationId: turnId, reason: "agent_failed" });
      this.pendingProcessingTurnId = null;
      this.realtimeActiveTurn = null;  // P0-2: failed turn resolved
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
    if (this.connectionId) {
      // Only fires if a connection was actually created (Start reached the
      // token-fetch stage) - a no-op end() on a never-started engine stays
      // silent, matching the existing "nothing to tear down" discipline
      // below (this.room/this.audioEl guards).
      logVoiceEvent("livekit_voice_connection_ended", {
        connectionId: this.connectionId, engineState: this.state,
      });
    }
    // Invalidate any outstanding startup work (Phase C2): a mic attempt or
    // agent-ready watchdog still in flight from this (or an even earlier)
    // start() call must never mutate state after end() - isCurrentGeneration()
    // is what every such continuation checks before acting.
    this.startupGeneration += 1;
    this.micReady = false;
    this.agentReadyReceived = false;
    this.connectionId = null;
    this.clearMicTimeout();
    this.clearAgentReadyWatchdog();
    this.clearDeliveryWatchdog();
    this.clearThinkingWatchdog("engine_end");
    this.clearSpeakingWatchdog();
    this.clearInterruptWatchdog();
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
