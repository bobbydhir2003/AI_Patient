import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import {
  fetchMyAssessment,
  fetchMySession,
  fetchMyTranscript,
  type SessionSummary,
  type TranscriptMessage,
} from "../../services/authApi";
import type { Assessment } from "../../types/assessment";
import { ApiError } from "../../services/api";
import { ErrorState, Spinner, StatusBadge } from "../../portal/ui";
import { TranscriptView } from "../../portal/TranscriptView";
import { AssessmentPanel } from "../../portal/AssessmentPanel";
import { caseLabel, fmtDateTime, fmtDuration } from "../../portal/format";

export function StudentSessionPage({ initialTab = "transcript" }: { initialTab?: "transcript" | "assessment" }) {
  const { sessionId } = useParams<{ sessionId: string }>();
  const { token } = useAuth();
  const navigate = useNavigate();
  const [tab, setTab] = useState<"transcript" | "assessment">(initialTab);
  const [session, setSession] = useState<SessionSummary | null>(null);
  const [transcript, setTranscript] = useState<TranscriptMessage[] | null>(null);
  const [assessment, setAssessment] = useState<Assessment | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !sessionId) return;
    setError(null);
    Promise.all([
      fetchMySession(token, sessionId),
      fetchMyTranscript(token, sessionId),
      fetchMyAssessment(token, sessionId).catch(() => null),
    ])
      .then(([s, t, a]) => {
        setSession(s);
        setTranscript(t);
        setAssessment(a);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load this session."));
  }, [token, sessionId]);

  if (error) return <div className="pt-portal"><ErrorState message={error} /></div>;
  if (!session || transcript === null) return <div className="pt-portal"><Spinner /></div>;

  return (
    <div className="pt-portal">
      <span className="pt-back" onClick={() => navigate("/student/dashboard")}>← Back to dashboard</span>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">{caseLabel(session.caseId)} interview</h1>
          <p className="pt-muted" style={{ margin: 0 }}>
            {fmtDateTime(session.startedAt)} · {fmtDuration(session.durationSeconds)} ·{" "}
            {session.studentTurnCount} questions asked
          </p>
        </div>
        <StatusBadge status={session.status} />
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
          <TranscriptView messages={transcript} />
        </div>
      )}
      {tab === "assessment" && (
        <AssessmentPanel
          sessionId={sessionId}
          sessionStatus={session.status}
          assessment={assessment ?? null}
          variant="student"
        />
      )}
    </div>
  );
}
