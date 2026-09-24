import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import {
  fetchStudentDataList,
  type Paginated,
  type StudentDataListItem,
} from "../../services/authApi";
import { ApiError } from "../../services/api";
import { ActiveBadge, EmptyState, ErrorState, LoadingState } from "../../portal/ui";
import { fmtDateShort, fmtDateTime } from "../../portal/format";
import { IconChevronRight, IconSearch } from "../../components/admin/icons";
import { StudentAvatar, StudentDataDetail } from "./StudentDataDetail";
import styles from "./AdminStudentData.module.css";

const PAGE_SIZE = 15;

/**
 * Admin "Student Data": master-detail view of one student's complete activity.
 *
 * /admin/student-data            -> the full student list (no student selected)
 * /admin/student-data/:studentId -> list + that student's detail (on narrow
 *                                   screens the detail replaces the list)
 *
 * The list only loads paged summary rows; a student's full history is fetched
 * only once that student is selected.
 */
export function AdminStudentDataPage() {
  const { studentId } = useParams<{ studentId: string }>();
  const navigate = useNavigate();
  const selected = studentId ?? null;

  return (
    <div>
      <div className="pt-page-header">
        <div>
          <h1 className="pt-h1">Student Data</h1>
          <p className="pt-page-sub">
            View each student's interviews, assessments, assessment viewing, surveys and activity in one place.
          </p>
        </div>
      </div>

      <div className={`${styles.layout} ${selected ? styles.hasSelection : ""}`}>
        <section className={styles.listPane} aria-label="Students">
          <StudentList
            selectedId={selected}
            compact={selected !== null}
            onSelect={(id) => navigate(`/admin/student-data/${id}`)}
          />
        </section>
        {selected && (
          <section className={styles.detailPane} aria-label="Selected student">
            <button
              type="button"
              className={`pt-back ${styles.mobileBack}`}
              onClick={() => navigate("/admin/student-data")}
            >
              ← All students
            </button>
            <StudentDataDetail key={selected} studentId={selected} />
          </section>
        )}
      </div>
    </div>
  );
}

function StudentList({
  selectedId,
  compact,
  onSelect,
}: {
  selectedId: string | null;
  compact: boolean;
  onSelect: (id: string) => void;
}) {
  const { token } = useAuth();
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [sort, setSort] = useState("name");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Paginated<StudentDataListItem> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  // Debounce typing so each keystroke is not a request.
  useEffect(() => {
    const t = window.setTimeout(() => setQuery(search.trim()), 300);
    return () => window.clearTimeout(t);
  }, [search]);
  useEffect(() => setPage(1), [query, status, sort]);

  useEffect(() => {
    if (!token) return;
    let stale = false;
    setError(null);
    setData(null);
    fetchStudentDataList(token, { search: query, status, sort, page, pageSize: PAGE_SIZE })
      .then((d) => !stale && setData(d))
      .catch((e) => !stale && setError(e instanceof ApiError ? e.message : "Could not load students."));
    return () => {
      stale = true;
    };
  }, [token, query, status, sort, page, reload]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1;
  const first = data && data.total > 0 ? (data.page - 1) * data.pageSize + 1 : 0;
  const last = data ? Math.min(data.page * data.pageSize, data.total) : 0;

  return (
    <div className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle}>
          Students {data && <span className="pt-muted">({data.total})</span>}
        </h2>
      </div>
      <div className={styles.listToolbar}>
        <label className={styles.searchBox}>
          <IconSearch width={16} height={16} />
          <input
            className="pt-input"
            placeholder="Search name, student ID or email…"
            aria-label="Search students by name, student ID or email"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <select
          className="pt-select"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label="Filter by account status"
        >
          <option value="all">All statuses</option>
          <option value="active">Active</option>
          <option value="inactive">Inactive</option>
        </select>
        <select
          className="pt-select"
          value={sort}
          onChange={(e) => setSort(e.target.value)}
          aria-label="Sort students"
        >
          <option value="name">Name A–Z</option>
          <option value="recent">Recently active</option>
        </select>
      </div>

      {error && <ErrorState message={error} onRetry={() => setReload((n) => n + 1)} />}
      {!error && data === null && <LoadingState label="Loading students…" />}
      {!error && data !== null && data.items.length === 0 && (
        <EmptyState
          title="No students found"
          hint={query || status !== "all" ? "Try a different search or filter." : undefined}
        />
      )}
      {!error && data !== null && data.items.length > 0 && (
        <>
          <div className={styles.tableScroll}>
            <table className={`pt-table ${styles.studentTable}`}>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  {!compact && <th scope="col">Student ID</th>}
                  {!compact && <th scope="col">Email</th>}
                  {!compact && <th scope="col">Sessions</th>}
                  <th scope="col">Last activity</th>
                  <th scope="col">Status</th>
                  {!compact && <th scope="col" aria-label="Open" />}
                </tr>
              </thead>
              <tbody>
                {data.items.map((s) => (
                  <tr
                    key={s.id}
                    className={`clickable ${s.id === selectedId ? styles.selectedRow : ""}`}
                    onClick={() => onSelect(s.id)}
                    aria-selected={s.id === selectedId}
                  >
                    <td>
                      <button
                        type="button"
                        className={styles.nameButton}
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelect(s.id);
                        }}
                      >
                        <StudentAvatar name={s.name} size="sm" />
                        <span className={styles.nameText}>
                          {s.name}
                          {compact && <span className={styles.subId}>ID {s.studentNumber || "—"}</span>}
                        </span>
                      </button>
                    </td>
                    {!compact && <td className={styles.nowrap}>{s.studentNumber || "—"}</td>}
                    {!compact && <td>{s.email || "—"}</td>}
                    {!compact && (
                      <td className={styles.nowrap}>
                        {s.sessionCount}
                        {s.incompleteCount > 0 && (
                          <span className="pt-muted"> · {s.incompleteCount} incomplete</span>
                        )}
                      </td>
                    )}
                    <td className={styles.nowrap} title={fmtDateTime(s.lastActivityAt)}>
                      {compact ? fmtDateShort(s.lastActivityAt) : fmtDateTime(s.lastActivityAt)}
                    </td>
                    <td>
                      <ActiveBadge active={s.isActive} />
                    </td>
                    {!compact && (
                      <td className={styles.chevronCell}>
                        <IconChevronRight width={16} height={16} />
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className={styles.pager}>
            <span className="pt-muted">
              Showing {first}–{last} of {data.total} student{data.total === 1 ? "" : "s"}
            </span>
            <div className={styles.pagerButtons}>
              <button
                className="pt-btn pt-btn-secondary pt-btn-sm"
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                aria-label="Previous page"
              >
                ‹
              </button>
              <span className={styles.pageLabel}>
                Page {data.page} of {totalPages}
              </span>
              <button
                className="pt-btn pt-btn-secondary pt-btn-sm"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
                aria-label="Next page"
              >
                ›
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
