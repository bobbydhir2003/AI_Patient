import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../state/AuthContext";
import { fetchStudents, type Paginated, type StudentListItem } from "../../services/authApi";
import { ApiError } from "../../services/api";
import { ActiveBadge, EmptyState, ErrorState, Spinner } from "../../portal/ui";
import { fmtDate } from "../../portal/format";

export function AdminStudentsPage() {
  const { token } = useAuth();
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [sort, setSort] = useState("newest");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Paginated<StudentListItem> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pageSize = 20;

  useEffect(() => {
    if (!token) return;
    setError(null);
    setData(null);
    const handle = setTimeout(() => {
      fetchStudents(token, { search, status, sort, page, pageSize })
        .then(setData)
        .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load students."));
    }, 250); // debounce search typing
    return () => clearTimeout(handle);
  }, [token, search, status, sort, page]);

  // Reset to page 1 whenever filters change.
  useEffect(() => setPage(1), [search, status, sort]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.pageSize)) : 1;

  return (
    <div>
      <div className="pt-page-header">
        <h1 className="pt-h1">Students</h1>
      </div>

      <div className="pt-toolbar">
        <input
          className="pt-input"
          placeholder="Search name, email, or number…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select className="pt-select" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="all">All statuses</option>
          <option value="active">Active only</option>
          <option value="inactive">Inactive only</option>
        </select>
        <select className="pt-select" value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
          <option value="name">Name (A–Z)</option>
        </select>
      </div>

      {error && <ErrorState message={error} />}
      {!error && data === null && <Spinner label="Loading students…" />}
      {!error && data !== null && data.items.length === 0 && (
        <EmptyState title="No students found" hint="Try adjusting your search or filters." />
      )}
      {!error && data !== null && data.items.length > 0 && (
        <>
          <div className="pt-table-wrap">
            <table className="pt-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Number</th>
                  <th>Status</th>
                  <th>Sessions</th>
                  <th>Completed</th>
                  <th>Joined</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((s) => (
                  <tr
                    key={s.id}
                    className="clickable"
                    onClick={() => navigate(`/admin/students/${s.id}`)}
                  >
                    <td>{s.name}</td>
                    <td>{s.email || <span className="pt-muted">—</span>}</td>
                    <td>{s.studentNumber || <span className="pt-muted">—</span>}</td>
                    <td><ActiveBadge active={s.isActive} /></td>
                    <td>{s.sessionCount}</td>
                    <td>{s.completedCount}</td>
                    <td>{fmtDate(s.createdAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pt-pagination">
            <span className="pt-muted">
              {data.total} student{data.total === 1 ? "" : "s"} · page {data.page} of {totalPages}
            </span>
            <button
              className="pt-btn pt-btn-secondary pt-btn-sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
            >
              Previous
            </button>
            <button
              className="pt-btn pt-btn-secondary pt-btn-sm"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </button>
          </div>
        </>
      )}
    </div>
  );
}
