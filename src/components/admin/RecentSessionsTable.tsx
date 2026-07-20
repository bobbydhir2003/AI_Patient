import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import {
  archiveSession,
  deleteSession,
  type RecentSessionItem,
} from "../../services/authApi";
import { ApiError } from "../../services/api";
import {
  AssessmentLevelBadge,
  ConfirmModal,
  ConfirmDeleteModal,
  EmptyState,
  StatusBadge,
  useToast,
} from "../../portal/ui";
import { caseLabel, fmtDateTime } from "../../portal/format";
import { IconClipboard, IconEye, IconMore, IconAssessments } from "./icons";

type Modal =
  | null
  | { kind: "archive"; id: string }
  | { kind: "delete"; id: string; turns: string };

export function RecentSessionsTable({
  sessions,
  onChanged,
}: {
  sessions: RecentSessionItem[];
  onChanged: () => void;
}) {
  const navigate = useNavigate();
  const { token } = useAuth();
  const toast = useToast();
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [modal, setModal] = useState<Modal>(null);
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await fn();
      toast.success(ok);
      setModal(null);
      onChanged();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pt-panel" aria-labelledby="recent-sessions-title">
      <div className="pt-panel-head">
        <h2 className="pt-panel-title" id="recent-sessions-title">
          Recent Sessions
        </h2>
        <button className="pt-panel-link" onClick={() => navigate("/admin/sessions")}>
          View All Sessions
        </button>
      </div>

      {sessions.length === 0 ? (
        <EmptyState title="No sessions yet" hint="Recent interview sessions will appear here." />
      ) : (
        <div className="pt-table-wrap">
          <table className="pt-table">
            <thead>
              <tr>
                <th scope="col">Student</th>
                <th scope="col">ID</th>
                <th scope="col">Case</th>
                <th scope="col">Status</th>
                <th scope="col">Assessment</th>
                <th scope="col">Started</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.sessionId}>
                  <td>{s.studentName}</td>
                  <td className="pt-muted">{s.studentNumber || "—"}</td>
                  <td>{caseLabel(s.caseId)}</td>
                  <td>
                    <StatusBadge status={s.status} />
                  </td>
                  <td>
                    {s.hasAssessment ? (
                      <AssessmentLevelBadge level={s.overallLevel} />
                    ) : (
                      <span className="pt-muted">—</span>
                    )}
                  </td>
                  <td>{fmtDateTime(s.startedAt)}</td>
                  <td>
                    <div className="pt-actions-cell">
                      <button
                        className="pt-icon-btn"
                        title="View session"
                        aria-label={`View session for ${s.studentName}`}
                        onClick={() => navigate(`/admin/sessions/${s.sessionId}`)}
                      >
                        <IconEye width={16} height={16} />
                      </button>
                      <button
                        className="pt-icon-btn"
                        title="View transcript"
                        aria-label={`View transcript for ${s.studentName}`}
                        onClick={() => navigate(`/admin/sessions/${s.sessionId}?tab=transcript`)}
                      >
                        <IconClipboard width={16} height={16} />
                      </button>
                      <button
                        className="pt-icon-btn"
                        title="View assessment"
                        aria-label={`View assessment for ${s.studentName}`}
                        disabled={!s.hasAssessment}
                        style={s.hasAssessment ? undefined : { opacity: 0.4, cursor: "not-allowed" }}
                        onClick={() => navigate(`/admin/sessions/${s.sessionId}?tab=assessment`)}
                      >
                        <IconAssessments width={16} height={16} />
                      </button>
                      <button
                        className="pt-icon-btn"
                        title="More actions"
                        aria-label={`More actions for ${s.studentName}`}
                        aria-haspopup="menu"
                        onClick={() => setMenuFor(menuFor === s.sessionId ? null : s.sessionId)}
                      >
                        <IconMore width={16} height={16} />
                      </button>
                      {menuFor === s.sessionId && (
                        <div
                          className="pt-more-menu"
                          role="menu"
                          onMouseLeave={() => setMenuFor(null)}
                        >
                          <button role="menuitem" onClick={() => navigate(`/admin/students/${s.studentId}`)}>
                            View student
                          </button>
                          {s.status !== "archived" && (
                            <button
                              role="menuitem"
                              onClick={() => {
                                setMenuFor(null);
                                setModal({ kind: "archive", id: s.sessionId });
                              }}
                            >
                              Archive session
                            </button>
                          )}
                          <button
                            role="menuitem"
                            className="danger"
                            onClick={() => {
                              setMenuFor(null);
                              setModal({ kind: "delete", id: s.sessionId, turns: s.studentName });
                            }}
                          >
                            Delete session
                          </button>
                        </div>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {modal?.kind === "archive" && (
        <ConfirmModal
          title="Archive this session?"
          body="The session is locked and hidden from active lists. Its transcript and assessment are preserved and this can be reviewed later."
          confirmLabel="Archive"
          busy={busy}
          onConfirm={() => run(() => archiveSession(token!, modal.id), "Session archived.")}
          onCancel={() => setModal(null)}
        />
      )}
      {modal?.kind === "delete" && (
        <ConfirmDeleteModal
          title="Permanently delete this session?"
          body={
            <>
              This will <strong>permanently delete</strong> {modal.turns}'s session, its full
              transcript, and any assessment and evidence connected to it. This cannot be undone.
              Consider archiving instead.
            </>
          }
          busy={busy}
          onConfirm={() => run(() => deleteSession(token!, modal.id), "Session deleted.")}
          onCancel={() => setModal(null)}
        />
      )}
    </section>
  );
}
