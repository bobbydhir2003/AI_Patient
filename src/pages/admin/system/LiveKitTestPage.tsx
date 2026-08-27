/**
 * Phase 1 LiveKit POC test page - admin/test-only, NOT part of the production
 * interview workflow (see /interview/:caseId, InterviewPage.tsx, which is
 * completely untouched by this experiment).
 *
 * Exercises ONE path end-to-end: join a LiveKit room, speak, and receive the
 * SAME OpenAI + ElevenLabs patient-generation pipeline's audio back over a
 * single persistent WebRTC track - no MediaSource, no Blob download, no
 * browser speechSynthesis fallback anywhere in this page. See
 * src/services/livekit/livekitPocEngine.ts for the full design notes.
 */
import { useCallback, useRef, useState } from "react";
import { useAuth } from "../../../state/AuthContext";
import { ApiError, createSession, type ApiSession } from "../../../services/api";
import { ErrorState } from "../../../portal/ui";
import {
  LiveKitPocEngine,
  type PocDiagnostics,
  type PocState,
} from "../../../services/livekit/livekitPocEngine";

const POC_CASE_ID = "carly";

const STATE_LABELS: Record<PocState, string> = {
  idle: "Idle",
  connecting: "Connecting…",
  waiting_for_agent: "Waiting for patient agent…",
  listening: "Listening",
  thinking: "Thinking…",
  speaking: "Speaking",
  interrupting: "Interrupting…",
  reconnecting: "Reconnecting…",
  error: "Error",
  ended: "Ended",
};

const INITIAL_DIAGNOSTICS: PocDiagnostics = {
  roomConnected: false,
  micPublished: false,
  patientTrackSubscribed: false,
  agentConnected: false,
};

function DiagnosticRow({ label, ok }: { label: string; ok: boolean }) {
  return (
    <div className="pt-row" style={{ justifyContent: "space-between", padding: "4px 0" }}>
      <span className="pt-muted">{label}</span>
      <span className={`pt-badge ${ok ? "pt-badge-green" : "pt-badge-gray"}`}>
        {ok ? "Yes" : "No"}
      </span>
    </div>
  );
}

export function LiveKitTestPage() {
  const { user } = useAuth();
  const [session, setSession] = useState<ApiSession | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pocState, setPocState] = useState<PocState>("idle");
  const [turnCount, setTurnCount] = useState(0);
  const [diagnostics, setDiagnostics] = useState<PocDiagnostics>(INITIAL_DIAGNOSTICS);
  const [transcript, setTranscript] = useState<string>("");
  const [roomName, setRoomName] = useState<string | null>(null);
  const engineRef = useRef<LiveKitPocEngine | null>(null);

  const handleStart = useCallback(async () => {
    if (!user) return;
    setStarting(true);
    setError(null);
    setTranscript("");
    setDiagnostics(INITIAL_DIAGNOSTICS);
    setTurnCount(0);
    setRoomName(null);
    try {
      // A fresh POC-only InterviewSession, owned by the current admin/test
      // account via the SAME production POST /sessions endpoint the real
      // student flow uses (ownership derived server-side from the bearer
      // token - see backend/app/api/sessions.py). Never touches a real
      // student's session or transcript.
      const newSession = await createSession(user.fullName, user.id, POC_CASE_ID);
      setSession(newSession);

      const engine = new LiveKitPocEngine({
        onStateChange: setPocState,
        onStudentTranscript: (text, isFinal) => {
          if (isFinal) setTranscript((prev) => (prev ? `${prev}\n` : "") + `You: ${text}`);
        },
        onError: (message) => setError(message),
        onTurnCompleted: (count) => setTurnCount(count),
        onDiagnostics: setDiagnostics,
        onRoomName: setRoomName,
      });
      engineRef.current = engine;
      await engine.start(newSession.sessionId);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not start the LiveKit POC session.");
    } finally {
      setStarting(false);
    }
  }, [user]);

  const handleEnd = useCallback(async () => {
    await engineRef.current?.end();
    engineRef.current = null;
  }, []);

  const canStart = !session || pocState === "ended" || pocState === "error";
  const canEnd = !!session && pocState !== "idle" && pocState !== "ended";

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1" style={{ margin: 0 }}>LiveKit Voice POC</h1>
          <p className="pt-page-sub">
            Phase 1 proof of concept only. Does not replace or affect the production interview page.
          </p>
        </div>
      </div>

      <div className="pt-card" style={{ maxWidth: 640, padding: "var(--space-4)" }}>
        <p className="pt-muted" style={{ marginTop: 0 }}>
          Case: <strong>{POC_CASE_ID}</strong>
          {session && (
            <>
              {" · "}Session: <span style={{ fontFamily: "monospace" }}>{session.sessionId}</span>
              {roomName && (
                <>
                  {" · "}Room: <span style={{ fontFamily: "monospace" }}>{roomName}</span>
                </>
              )}
            </>
          )}
        </p>
        {session && roomName && (
          <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: 0 }}>
            Copy the room name above and pass it to the agent worker's <code>--room</code> flag
            (with <code>--session-id {session.sessionId}</code>) in a separate terminal.
          </p>
        )}

        <div className="pt-row" style={{ gap: 8, marginBottom: "var(--space-4)" }}>
          <button
            className="pt-btn pt-btn-primary"
            onClick={handleStart}
            disabled={!canStart || starting}
          >
            {starting ? "Starting…" : "Start Interview"}
          </button>
          <button className="pt-btn pt-btn-secondary" onClick={handleEnd} disabled={!canEnd}>
            End Interview
          </button>
        </div>

        <div className="pt-row" style={{ justifyContent: "space-between", padding: "8px 0", borderTop: "1px solid var(--color-border)" }}>
          <span>Current state</span>
          <span className="pt-badge pt-badge-blue">{STATE_LABELS[pocState]}</span>
        </div>
        <div className="pt-row" style={{ justifyContent: "space-between", padding: "4px 0" }}>
          <span className="pt-muted">Patient turns completed</span>
          <span>{turnCount}</span>
        </div>

        <h2 className="pt-h2" style={{ fontSize: "0.95rem", marginTop: "var(--space-4)" }}>
          Diagnostics
        </h2>
        <DiagnosticRow label="Room connected" ok={diagnostics.roomConnected} />
        <DiagnosticRow label="Microphone published" ok={diagnostics.micPublished} />
        <DiagnosticRow label="Patient audio track subscribed" ok={diagnostics.patientTrackSubscribed} />
        <DiagnosticRow label="Agent connected" ok={diagnostics.agentConnected} />

        {error && <ErrorState message={error} />}

        {transcript && (
          <>
            <h2 className="pt-h2" style={{ fontSize: "0.95rem", marginTop: "var(--space-4)" }}>
              Recognized speech (student side only)
            </h2>
            <pre
              style={{
                whiteSpace: "pre-wrap",
                fontFamily: "inherit",
                fontSize: "0.85rem",
                background: "var(--color-bg-elevated)",
                padding: "var(--space-2)",
                borderRadius: 6,
              }}
            >
              {transcript}
            </pre>
          </>
        )}

        <p className="pt-muted" style={{ fontSize: "0.78rem", marginTop: "var(--space-4)" }}>
          Requires the standalone agent worker running separately
          (<code>python -m app.livekit_agent.worker --room ptai-poc-&lt;session_id&gt; --session-id &lt;session_id&gt; --case-id {POC_CASE_ID}</code>)
          and LiveKit Cloud credentials configured on the backend. Patient audio never falls back to
          browser text-to-speech in this path - a failure here surfaces as an explicit error above.
        </p>
      </div>
    </div>
  );
}
