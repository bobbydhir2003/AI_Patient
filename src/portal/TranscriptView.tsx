import type { TranscriptMessage } from "../services/authApi";

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleString();
}

function speakerLabel(speaker: string): string {
  if (speaker === "student") return "Student";
  if (speaker === "patient") return "AI Patient";
  return "System / Note";
}

/** Read-only chat-style transcript. Messages are shown exactly as stored, in
 * turn order; nothing is regenerated or edited. An optional per-message action
 * (used by the admin panel to delete a message) can be supplied. */
export function TranscriptView({
  messages,
  renderAction,
}: {
  messages: TranscriptMessage[];
  renderAction?: (m: TranscriptMessage) => React.ReactNode;
}) {
  if (messages.length === 0) {
    return <div className="pt-muted">This session has no saved conversation.</div>;
  }
  const ordered = [...messages].sort((a, b) => a.turnIndex - b.turnIndex);
  return (
    <div className="pt-transcript">
      {ordered.map((m) => {
        const cls =
          m.speaker === "student"
            ? "pt-msg-student"
            : m.speaker === "patient"
              ? "pt-msg-patient"
              : "pt-msg-system";
        return (
          <div key={m.id} className={`pt-msg ${cls}`}>
            <span className="pt-msg-meta">
              #{m.turnIndex + 1} · {speakerLabel(m.speaker)} · {fmtTime(m.createdAt)}
            </span>
            {m.content}
            {renderAction && <div style={{ marginTop: 6 }}>{renderAction(m)}</div>}
          </div>
        );
      })}
    </div>
  );
}
