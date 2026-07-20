import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import {
  deleteStudent,
  fetchStudentDetail,
  fetchStudentSessions,
  setStudentStatus,
  type SessionSummary,
  type StudentDetail,
} from "../../services/authApi";
import { ApiError } from "../../services/api";
import {
  ActiveBadge,
  ConfirmModal,
  ErrorState,
  Spinner,
  StatusBadge,
  TypeToConfirmModal,
  useToast,
} from "../../portal/ui";
import { caseLabel, fmtDate, fmtDateTime, fmtDuration } from "../../portal/format";

export function AdminStudentDetailPage() {
  const { studentId } = useParams<{ studentId: string }>();
  const { token, user } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const [student, setStudent] = useState<StudentDetail | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [modal, setModal] = useState<null | "archive" | "reactivate" | "delete">(null);
  const [busy, setBusy] = useState(false);

  function load() {
    if (!token || !studentId) return;
    setError(null);
    Promise.all([fetchStudentDetail(token, studentId), fetchStudentSessions(token, studentId)])
      .then(([d, s]) => {
        setStudent(d);
        setSessions(s);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load this student."));
  }
  useEffect(load, [token, studentId]);

  async function toggleStatus(active: boolean) {
    if (!token || !studentId) return;
    setBusy(true);
    try {
      await setStudentStatus(token, studentId, active);
      toast.success(active ? "Student reactivated." : "Student archived.");
      setModal(null);
      load();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }

  async function permanentDelete() {
    if (!token || !studentId) return;
    setBusy(true);
    try {
      await deleteStudent(token, studentId, "DELETE");
      toast.success("Student and all connected data deleted.");
      navigate("/admin/students");
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Delete failed.");
      setBusy(false);
    }
  }

  if (error) return <ErrorState message={error} onRetry={load} />;
  if (!student || sessions === null) return <Spinner />;

  const isSelf = student.role === "admin" && student.accountEmail === user?.email;

  return (
    <div>
      <span className="pt-back" onClick={() => navigate("/admin/students")}>← Back to students</span>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">{student.name}</h1>
          <p className="pt-muted" style={{ margin: 0 }}>
            {student.email || "no email"} {student.studentNumber ? `· #${student.studentNumber}` : ""}
          </p>
        </div>
        <ActiveBadge active={student.isActive} />
      </div>

      <div className="pt-card pt-section">
        <dl className="pt-kv">
          <dt>Login account</dt>
          <dd>{student.hasAccount ? student.accountEmail : "No account linked"}</dd>
          <dt>Role</dt>
          <dd>{student.role ?? "—"}</dd>
          <dt>Joined</dt>
          <dd>{fmtDate(student.createdAt)}</dd>
          <dt>Last login</dt>
          <dd>{fmtDateTime(student.lastLoginAt)}</dd>
          <dt>Sessions</dt>
          <dd>{student.sessionCount} ({student.completedCount} completed)</dd>
          <dt>Assessments</dt>
          <dd>{student.assessmentCount}</dd>
        </dl>

        <div className="pt-row" style={{ marginTop: 20 }}>
          {student.isActive ? (
            <button className="pt-btn pt-btn-secondary pt-btn-sm" onClick={() => setModal("archive")} disabled={isSelf}>
              Archive account
            </button>
          ) : (
            <button className="pt-btn pt-btn-sm" onClick={() => setModal("reactivate")}>
              Reactivate account
            </button>
          )}
          <button className="pt-btn pt-btn-danger pt-btn-sm" onClick={() => setModal("delete")} disabled={isSelf}>
            Permanently delete
          </button>
          {isSelf && <span className="pt-muted" style={{ fontSize: "0.8rem" }}>You cannot modify your own account.</span>}
        </div>
      </div>

      <h2 className="pt-h2">Sessions</h2>
      {sessions.length === 0 ? (
        <p className="pt-muted">This student has no sessions.</p>
      ) : (
        <div className="pt-table-wrap">
          <table className="pt-table">
            <thead>
              <tr>
                <th>Case</th>
                <th>Status</th>
                <th>Started</th>
                <th>Duration</th>
                <th>Assessment</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr
                  key={s.sessionId}
                  className="clickable"
                  onClick={() => navigate(`/admin/sessions/${s.sessionId}`)}
                >
                  <td>{caseLabel(s.caseId)}</td>
                  <td><StatusBadge status={s.status} /></td>
                  <td>{fmtDateTime(s.startedAt)}</td>
                  <td>{fmtDuration(s.durationSeconds)}</td>
                  <td>{s.hasAssessment ? "Yes" : <span className="pt-muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {modal === "archive" && (
        <ConfirmModal
          title="Archive this student?"
          body="Archiving deactivates the login and hides the student from active lists. Their data is preserved and this can be reversed."
          confirmLabel="Archive"
          busy={busy}
          onConfirm={() => toggleStatus(false)}
          onCancel={() => setModal(null)}
        />
      )}
      {modal === "reactivate" && (
        <ConfirmModal
          title="Reactivate this student?"
          body="The student will be able to sign in again."
          confirmLabel="Reactivate"
          busy={busy}
          onConfirm={() => toggleStatus(true)}
          onCancel={() => setModal(null)}
        />
      )}
      {modal === "delete" && (
        <TypeToConfirmModal
          title="Permanently delete this student?"
          body={
            <>
              This will <strong>permanently delete</strong> {student.name}, their login account,
              all {student.sessionCount} session(s), every transcript message, and all connected
              assessments and evidence. This cannot be undone. Consider archiving instead.
            </>
          }
          busy={busy}
          onConfirm={permanentDelete}
          onCancel={() => setModal(null)}
        />
      )}
    </div>
  );
}
