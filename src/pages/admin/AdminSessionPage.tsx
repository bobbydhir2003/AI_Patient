import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import {
  archiveSession,
  deleteAssessment,
  deleteMessage,
  deleteSession,
  fetchAdminAssessment,
  fetchAdminSession,
  fetchAdminTranscript,
  type SessionSummary,
  type TranscriptMessage,
} from "../../services/authApi";
import type { Assessment } from "../../types/assessment";
import { ApiError } from "../../services/api";
import {
  ConfirmModal,
  ErrorState,
  Spinner,
  StatusBadge,
  TypeToConfirmModal,
  useToast,
} from "../../portal/ui";
import { TranscriptView } from "../../portal/TranscriptView";
import { AssessmentPanel } from "../../portal/AssessmentPanel";
import { caseLabel, fmtDateTime, fmtDuration } from "../../portal/format";

type Modal =
  | null
  | { kind: "archiveSession" }
  | { kind: "deleteSession" }
  | { kind: "deleteAssessment"; id: string }
  | { kind: "deleteMessage"; id: string };

export function AdminSessionPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const { token } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<"transcript" | "assessment">(
    searchParams.get("tab") === "assessment" ? "assessment" : "transcript",
  );
  const [session, setSession] = useState<SessionSummary | null>(null);
  const [transcript, setTranscript] = useState<TranscriptMessage[] | null>(null);
  const [assessment, setAssessment] = useState<Assessment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [modal, setModal] = useState<Modal>(null);
  const [busy, setBusy] = useState(false);

  function load() {
    if (!token || !sessionId) return;
    setError(null);
    Promise.all([
      fetchAdminSession(token, sessionId),
      fetchAdminTranscript(token, sessionId),
      fetchAdminAssessment(token, sessionId).catch(() => null),
    ])
      .then(([s, t, a]) => {
        setSession(s);
        setTranscript(t);
        setAssessment(a);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load this session."));
  }
  useEffect(load, [token, sessionId]);

  async function run(action: () => Promise<unknown>, ok: string, after: "reload" | "back") {
    setBusy(true);
    try {
      await action();
      toast.success(ok);
      setModal(null);
      if (after === "back") navigate(-1);
      else load();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }

  if (error) return <ErrorState message={error} onRetry={load} />;
  if (!session || transcript === null) return <Spinner />;

  return (
    <div>
      <span className="pt-back" onClick={() => navigate(-1)}>← Back</span>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">{caseLabel(session.caseId)} · {session.studentName}</h1>
          <p className="pt-muted" style={{ margin: 0 }}>
            {fmtDateTime(session.startedAt)} · {fmtDuration(session.durationSeconds)} ·{" "}
            {session.studentTurnCount} questions
          </p>
        </div>
        <StatusBadge status={session.status} />
      </div>

      <div className="pt-row" style={{ marginBottom: 16 }}>
        {session.status !== "archived" && (
          <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => setModal({ kind: "archiveSession" })}>
            Archive session
          </button>
        )}
        {assessment && (
          <button
            className="pt-btn pt-btn-secondary pt-btn-sm"
            onClick={() => setModal({ kind: "deleteAssessment", id: assessment.assessmentId })}
          >
            Delete assessment
          </button>
        )}
        <button className="pt-btn pt-btn-danger pt-btn-sm" onClick={() => setModal({ kind: "deleteSession" })}>
          Delete session
        </button>
      </div>

      <div className="pt-row" style={{ marginBottom: 16 }}>
        <button
          className={`pt-btn pt-btn-sm ${tab === "transcript" ? "" : "pt-btn-secondary"}`}
          onClick={() => setTab("transcript")}
        >
          Transcript
        </button>
        <button
          className={`pt-btn pt-btn-sm ${tab === "assessment" ? "" : "pt-btn-secondary"}`}
          onClick={() => setTab("assessment")}
        >
          AI assessment
        </button>
      </div>

      {tab === "transcript" && (
        <div className="pt-card">
          <TranscriptView
            messages={transcript}
            renderAction={(m) => (
              <button
                className="pt-btn pt-btn-secondary pt-btn-sm"
                onClick={() => setModal({ kind: "deleteMessage", id: m.id })}
              >
                Delete message
              </button>
            )}
          />
        </div>
      )}
      {tab === "assessment" && (
        <AssessmentPanel
          sessionId={sessionId}
          sessionStatus={session.status}
          assessment={assessment}
          variant="admin"
        />
      )}

      {modal?.kind === "archiveSession" && (
        <ConfirmModal
          title="Archive this session?"
          body="The session is locked and hidden from active lists. Its transcript and assessment are preserved."
          confirmLabel="Archive"
          busy={busy}
          onConfirm={() => run(() => archiveSession(token!, session.sessionId), "Session archived.", "reload")}
          onCancel={() => setModal(null)}
        />
      )}
      {modal?.kind === "deleteAssessment" && (
        <ConfirmModal
          title="Delete this assessment?"
          body="The AI assessment, its domain results and evidence will be removed. The transcript is kept."
          confirmLabel="Delete assessment"
          danger
          busy={busy}
          onConfirm={() => run(() => deleteAssessment(token!, modal.id), "Assessment deleted.", "reload")}
          onCancel={() => setModal(null)}
        />
      )}
      {modal?.kind === "deleteMessage" && (
        <ConfirmModal
          title="Delete this transcript message?"
          body="Only do this when necessary. Any assessment evidence anchored to this message is also removed."
          confirmLabel="Delete message"
          danger
          busy={busy}
          onConfirm={() => run(() => deleteMessage(token!, modal.id), "Message deleted.", "reload")}
          onCancel={() => setModal(null)}
        />
      )}
      {modal?.kind === "deleteSession" && (
        <TypeToConfirmModal
          title="Permanently delete this session?"
          body={
            <>
              This will <strong>permanently delete</strong> the session, all {session.turnCount}{" "}
              transcript messages, and any assessment and evidence connected to it. This cannot be
              undone. Consider archiving instead.
            </>
          }
          busy={busy}
          onConfirm={() => run(() => deleteSession(token!, session.sessionId), "Session deleted.", "back")}
          onCancel={() => setModal(null)}
        />
      )}
    </div>
  );
}
