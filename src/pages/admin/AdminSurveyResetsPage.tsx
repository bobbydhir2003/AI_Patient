import { useEffect, useId, useState, type ComponentType, type SVGProps } from "react";
import { useAuth } from "../../state/AuthContext";
import {
  bulkResetSurveys,
  fetchSurveyResets,
  resetStudentSurvey,
  type SurveyResetList,
  type SurveyResetRow,
  type SurveyResetScope,
  type SurveyStageStatus,
} from "../../services/authApi";
import { ApiError } from "../../services/api";
import { ConfirmModal, EmptyState, ErrorState, LoadingState, useToast } from "../../portal/ui";
import { fmtDateShort, fmtDateTime } from "../../portal/format";
import {
  IconAlert,
  IconCheckCircle,
  IconClock,
  IconRefresh,
  IconSearch,
  IconStudents,
} from "../../components/admin/icons";
import styles from "./AdminSurveyResets.module.css";

type Icon = ComponentType<SVGProps<SVGSVGElement>>;

const PAGE_SIZES = [10, 25, 50] as const;

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: "all", label: "All Survey Status" },
  { value: "completed", label: "Pre + Post completed" },
  { value: "in_progress", label: "In progress" },
  { value: "not_started", label: "Not started" },
  { value: "reset_before", label: "Reset before" },
];

/** Bulk scopes, in the order shown at the top of the page. */
const BULK: { scope: SurveyResetScope; title: string; sub: string; confirmTitle: string; count: keyof SurveyResetList["summary"] }[] = [
  {
    scope: "both",
    title: "Reset All Surveys",
    sub: "Reset pre and post surveys for all students",
    confirmTitle: "Reset all surveys?",
    count: "bothResettable",
  },
  {
    scope: "pre",
    title: "Reset All Pre Surveys",
    sub: "Reset only pre-surveys for all students",
    confirmTitle: "Reset all pre-surveys?",
    count: "preResettable",
  },
  {
    scope: "post",
    title: "Reset All Post Surveys",
    sub: "Reset only post-surveys for all students",
    confirmTitle: "Reset all post-surveys?",
    count: "postResettable",
  },
];

const SCOPE_NOUN: Record<SurveyResetScope, string> = {
  pre: "the pre-survey",
  post: "the post-survey",
  both: "both the pre- and post-survey",
};

/** Per-student confirm copy. Names come from the row the admin clicked. */
const ROW_COPY: Record<SurveyResetScope, { title: (n: string) => string; label: string }> = {
  pre: { title: (n) => `Reset pre-survey for ${n}?`, label: "Reset Pre Survey" },
  post: { title: (n) => `Reset post-survey for ${n}?`, label: "Reset Post Survey" },
  both: { title: (n) => `Reset both surveys for ${n}?`, label: "Reset Both Surveys" },
};

// Status pills carry an icon + text, never colour alone.
const STAGE_PILL: Record<SurveyStageStatus, { cls: string; label: string; icon: Icon }> = {
  completed: { cls: "pt-badge-green", label: "Completed", icon: IconCheckCircle },
  pending: { cls: "pt-badge-amber", label: "Pending", icon: IconClock },
  failed: { cls: "pt-badge-red", label: "Sync failed", icon: IconAlert },
  not_started: { cls: "pt-badge-gray", label: "Not Started", icon: IconClock },
};

function StagePill({ status, at }: { status: SurveyStageStatus; at: string | null }) {
  const p = STAGE_PILL[status] ?? STAGE_PILL.not_started;
  const Ico = p.icon;
  return (
    <span
      className={`pt-badge ${p.cls} ${styles.pill}`}
      title={status === "completed" && at ? `Completed ${fmtDateTime(at)}` : status === "failed" ? "Submitted, but the REDCap import failed. The student can retry." : undefined}
    >
      <Ico width={13} height={13} />
      {p.label}
    </span>
  );
}

function initials(name: string): string {
  const parts = (name || "?").trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "?";
}

type Pending =
  | { kind: "row"; scope: SurveyResetScope; row: SurveyResetRow }
  | { kind: "bulk"; scope: SurveyResetScope };

/**
 * Admin "Survey Resets": survey completion state for every REAL student (practice
 * profiles excluded server-side) with per-student and bulk resets.
 *
 * Every reset goes through a confirmation dialog; bulk resets additionally require
 * typing RESET. Resets never delete survey history: earlier REDCap answers stay
 * under their original record and the retake is filed under a new one.
 */
export function AdminSurveyResetsPage() {
  const { token } = useAuth();
  const toast = useToast();
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [caseId, setCaseId] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(PAGE_SIZES[0]);
  const [data, setData] = useState<SurveyResetList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);

  // Debounce typing so each keystroke is not a request.
  useEffect(() => {
    const t = window.setTimeout(() => setQuery(search.trim()), 300);
    return () => window.clearTimeout(t);
  }, [search]);
  useEffect(() => setPage(1), [query, status, caseId, pageSize]);

  useEffect(() => {
    if (!token) return;
    let stale = false;
    setError(null);
    fetchSurveyResets(token, { search: query, status, caseId, page, pageSize })
      .then((d) => !stale && setData(d))
      .catch((e) => !stale && setError(e instanceof ApiError ? e.message : "Could not load survey data."));
    return () => {
      stale = true;
    };
  }, [token, query, status, caseId, page, pageSize, reload]);

  const refresh = () => setReload((n) => n + 1);

  async function doReset(p: Pending) {
    if (!token) return;
    setBusy(true);
    try {
      if (p.kind === "row") {
        const res = await resetStudentSurvey(token, p.row.studentId, p.scope);
        toast.success(res.message || "Survey reset.");
      } else {
        const res = await bulkResetSurveys(token, p.scope);
        if (res.failed > 0) toast.error(res.message);
        else toast.success(res.message);
      }
      setPending(null);
      // Re-read from the server: the row and the counters reflect what actually changed.
      refresh();
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : "Survey reset failed.");
    } finally {
      setBusy(false);
    }
  }

  const summary = data?.summary;
  const pct = (n: number) => (summary && summary.totalStudents > 0 ? Math.round((n / summary.totalStudents) * 100) : 0);
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1;
  const first = data && data.total > 0 ? (data.page - 1) * data.pageSize + 1 : 0;
  const last = data ? Math.min(data.page * data.pageSize, data.total) : 0;

  const stats: { label: string; value: number | undefined; hint: string; accent: string; icon: Icon }[] = [
    { label: "Total Students", value: summary?.totalStudents, hint: "All enrolled real students", accent: "gray", icon: IconStudents },
    {
      label: "Pre Surveys Completed",
      value: summary?.preCompleted,
      hint: summary ? `${pct(summary.preCompleted)}% completion rate` : "—",
      accent: "green",
      icon: IconCheckCircle,
    },
    {
      label: "Post Surveys Completed",
      value: summary?.postCompleted,
      hint: summary ? `${pct(summary.postCompleted)}% completion rate` : "—",
      accent: "blue",
      icon: IconCheckCircle,
    },
    {
      label: "Available for Reset",
      value: summary?.availableForReset,
      hint: "Students with a started or completed survey",
      accent: "amber",
      icon: IconRefresh,
    },
  ];

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">Survey Resets</h1>
          <p className="pt-page-sub">
            Reset pre-survey, post-survey, or both for students when they need to retake survey responses.
          </p>
        </div>
      </div>

      <div className={styles.bulkRow}>
        {BULK.map((b, i) => (
          <button
            key={b.scope}
            type="button"
            className={`${styles.bulkCard} ${i === 0 ? styles.bulkPrimary : ""}`}
            onClick={() => setPending({ kind: "bulk", scope: b.scope })}
            disabled={!summary || busy}
          >
            <span className={styles.bulkIcon} aria-hidden="true">
              <IconRefresh width={20} height={20} />
            </span>
            <span className={styles.bulkText}>
              <span className={styles.bulkTitle}>{b.title}</span>
              <span className={styles.bulkSub}>{b.sub}</span>
            </span>
          </button>
        ))}
      </div>

      <div className={styles.banner} role="note">
        <IconAlert width={18} height={18} />
        <span>
          Survey resets allow students to complete the selected survey again. Existing survey history and prior
          REDCap responses are preserved: the retake is saved under a new REDCap record. Sessions, transcripts and
          assessments are not changed.
        </span>
      </div>

      <div className={styles.stats}>
        {stats.map((c) => {
          const Ico = c.icon;
          return (
            <div key={c.label} className={`pt-statcard accent-${c.accent}`}>
              <div className="pt-statcard-top">
                <span className="pt-statcard-lbl">{c.label}</span>
                <span className="pt-statcard-ic" aria-hidden="true">
                  <Ico width={18} height={18} />
                </span>
              </div>
              <div className="pt-statcard-val">{c.value ?? "—"}</div>
              <div className="pt-statcard-hint">{c.hint}</div>
            </div>
          );
        })}
      </div>

      <div className="pt-uac-controls">
        <div className="pt-uac-search">
          <span className="pt-uac-search-ic" aria-hidden="true">
            <IconSearch width={16} height={16} />
          </span>
          <input
            className="pt-input"
            placeholder="Search by name, email, or student ID…"
            aria-label="Search students by name, email, or student ID"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <select className="pt-input pt-uac-role" value={caseId} onChange={(e) => setCaseId(e.target.value)} aria-label="Filter by survey case">
          <option value="">All Cases</option>
          {data?.caseOptions.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
          <option value="none">No survey case</option>
        </select>
        <select className="pt-input pt-uac-role" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by survey status">
          {STATUS_FILTERS.map((s) => (
            <option key={s.value} value={s.value}>{s.label}</option>
          ))}
        </select>
        <div className="pt-uac-controls-right">
          <button type="button" className="pt-btn pt-btn-secondary pt-btn-icon" onClick={refresh} aria-label="Refresh">
            <IconRefresh width={16} height={16} /> Refresh
          </button>
        </div>
      </div>

      {error && <ErrorState message={error} onRetry={refresh} />}
      {!error && data === null && <LoadingState label="Loading survey data…" />}
      {!error && data !== null && (
        <div className="pt-uac-tablecard">
          {data.items.length === 0 ? (
            <EmptyState
              title="No students found"
              hint={query || status !== "all" || caseId ? "Try a different search or filter." : undefined}
            />
          ) : (
            <div className={styles.tableScroll}>
              <table className={`pt-uac-table ${styles.table}`}>
                <thead>
                  <tr>
                    <th scope="col">Student</th>
                    <th scope="col">Email</th>
                    <th scope="col">Student ID</th>
                    <th scope="col">Survey Case</th>
                    <th scope="col">Pre Survey</th>
                    <th scope="col">Post Survey</th>
                    <th scope="col">Last Activity</th>
                    <th scope="col" className={styles.actionsHead}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((s) => (
                    <tr key={s.studentId}>
                      <td>
                        <div className="pt-uac-usercell">
                          <span className="pt-uac-avatar" aria-hidden="true">{initials(s.name)}</span>
                          <span className={styles.nameCol}>
                            <span className="pt-uac-username">{s.name}</span>
                            {(s.resetCount > 0 || !s.isActive) && (
                              <span className="pt-uac-usersub">
                                {!s.isActive && "Archived"}
                                {!s.isActive && s.resetCount > 0 && " · "}
                                {s.resetCount > 0 &&
                                  `Reset ${s.resetCount}× · last ${fmtDateShort(s.lastResetAt)}`}
                              </span>
                            )}
                          </span>
                        </div>
                      </td>
                      <td className="pt-uac-emailcol">{s.email || "—"}</td>
                      <td className={styles.nowrap}>{s.studentNumber || "—"}</td>
                      <td>{s.surveyCaseName ?? <span className="pt-muted">—</span>}</td>
                      <td><StagePill status={s.preStatus} at={s.preCompletedAt} /></td>
                      <td><StagePill status={s.postStatus} at={s.postCompletedAt} /></td>
                      <td className={styles.when}>
                        {s.lastActivityAt ? fmtDateTime(s.lastActivityAt) : <span className="pt-muted">—</span>}
                      </td>
                      <td>
                        <div className={styles.actions}>
                          <button
                            type="button"
                            className="pt-btn pt-btn-secondary pt-btn-sm pt-btn-icon"
                            disabled={!s.canResetPre || busy}
                            title={s.canResetPre ? undefined : "No completed pre-survey to reset"}
                            onClick={() => setPending({ kind: "row", scope: "pre", row: s })}
                          >
                            <IconRefresh width={13} height={13} /> Reset Pre
                          </button>
                          <button
                            type="button"
                            className="pt-btn pt-btn-secondary pt-btn-sm pt-btn-icon"
                            disabled={!s.canResetPost || busy}
                            title={s.canResetPost ? undefined : "No completed post-survey to reset"}
                            onClick={() => setPending({ kind: "row", scope: "post", row: s })}
                          >
                            <IconRefresh width={13} height={13} /> Reset Post
                          </button>
                          <button
                            type="button"
                            className="pt-btn pt-btn-danger pt-btn-sm pt-btn-icon"
                            disabled={!s.canResetBoth || busy}
                            title={s.canResetBoth ? undefined : "This student has no survey to reset"}
                            onClick={() => setPending({ kind: "row", scope: "both", row: s })}
                          >
                            <IconRefresh width={13} height={13} /> Reset Full
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="pt-uac-pagination">
            <span className="pt-muted">
              Showing {first} to {last} of {data.total} student{data.total === 1 ? "" : "s"}
            </span>
            <div className="pt-uac-pag-right">
              <label className="pt-uac-rpp">
                Rows per page:
                <select className="pt-input" value={pageSize} onChange={(e) => setPageSize(Number(e.target.value))}>
                  {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </label>
              <div className="pt-uac-pag-btns">
                <button className="pt-iconbtn pt-iconbtn-ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)} aria-label="Previous page">‹</button>
                {Array.from({ length: totalPages }, (_, i) => i + 1)
                  .filter((p) => p === 1 || p === totalPages || Math.abs(p - page) <= 1)
                  .map((p, idx, arr) => (
                    <span key={p} className={styles.pagItem}>
                      {idx > 0 && arr[idx - 1] !== p - 1 && <span className="pt-muted">…</span>}
                      <button
                        className={`pt-uac-pagnum ${p === page ? "active" : ""}`}
                        aria-current={p === page ? "page" : undefined}
                        onClick={() => setPage(p)}
                      >
                        {p}
                      </button>
                    </span>
                  ))}
                <button className="pt-iconbtn pt-iconbtn-ghost" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)} aria-label="Next page">›</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {pending?.kind === "row" && (
        <ConfirmModal
          title={ROW_COPY[pending.scope].title(pending.row.name)}
          body={
            <>
              This will allow the student to complete {SCOPE_NOUN[pending.scope]} again. Existing survey history
              will be preserved: earlier REDCap answers stay under their original record.
              {pending.scope === "both" && " The student can then take the survey in any case."}
            </>
          }
          confirmLabel={ROW_COPY[pending.scope].label}
          danger
          busy={busy}
          onConfirm={() => doReset(pending)}
          onCancel={() => setPending(null)}
        />
      )}
      {pending?.kind === "bulk" && summary && (
        <BulkConfirmModal
          title={BULK.find((b) => b.scope === pending.scope)!.confirmTitle}
          confirmLabel={BULK.find((b) => b.scope === pending.scope)!.title}
          affected={summary[BULK.find((b) => b.scope === pending.scope)!.count]}
          scope={pending.scope}
          busy={busy}
          onConfirm={() => doReset(pending)}
          onCancel={() => setPending(null)}
        />
      )}
    </div>
  );
}

/** Bulk resets touch every eligible student, so a click is never enough:
 *  the admin must type RESET before the confirm button enables. */
function BulkConfirmModal({
  title,
  confirmLabel,
  affected,
  scope,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string;
  confirmLabel: string;
  affected: number;
  scope: SurveyResetScope;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const ready = text.trim().toUpperCase() === "RESET" && affected > 0;
  const titleId = useId();
  return (
    <div className="pt-modal-backdrop" onClick={busy ? undefined : onCancel}>
      <div
        className="pt-modal danger"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={(e) => e.key === "Escape" && !busy && onCancel()}
      >
        <h3 id={titleId}>{title}</h3>
        <div className="pt-sub">
          <p>
            This will allow all eligible real students to complete {SCOPE_NOUN[scope]} again.{" "}
            <strong>
              {affected} student{affected === 1 ? "" : "s"}
            </strong>{" "}
            currently {affected === 1 ? "has" : "have"} a survey state this reset applies to; everyone else is skipped.
          </p>
          <p>
            Existing REDCap survey history is preserved. Practice and admin-only accounts are never included.
          </p>
        </div>
        <div className="pt-field">
          <label htmlFor={`${titleId}-confirm`}>
            Type <strong>RESET</strong> to confirm
          </label>
          <input
            id={`${titleId}-confirm`}
            className="pt-input"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="RESET"
            autoFocus
            disabled={busy || affected === 0}
          />
        </div>
        <div className="pt-modal-actions">
          <button className="pt-btn pt-btn-secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button className="pt-btn pt-btn-danger" onClick={onConfirm} disabled={!ready || busy}>
            {busy ? "Resetting…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
