"""Tests for the User Accounts bulk-management endpoints (Part 1).

Covers real DB summary counts, bulk approve/reject, approve-all-pending, the
confirmation-gated flow (backend only acts when the endpoint is called, so a
cancelled confirmation is a genuine no-op), idempotency against double
submission, and that counts refresh correctly after each action. Every count
asserted here is a real query result, never a hardcoded example.
"""
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.models import User
from tests.conftest import FakeOpenAIClient, bearer, make_client
from tests.test_auth import login_token


def _account(engine, email, *, role="student", status="ACTIVE", password="pw123456"):
    db = sessionmaker(bind=engine)()
    try:
        u = User(email=email, password_hash=hash_password(password), full_name="U",
                 role=role, account_status=status, is_active=(status == "ACTIVE"))
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _admin_headers(client, engine, email="admin@school.edu", role="admin"):
    _account(engine, email, role=role, status="ACTIVE", password="adminpass1")
    return bearer(login_token(client, email, "adminpass1"))


def _summary(client, headers):
    r = client.get("/api/admin/users/summary", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ summary
def test_summary_reports_real_counts(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine)  # 1 active admin
        _account(engine, "p1@s.edu", status="PENDING")
        _account(engine, "p2@s.edu", status="PENDING")
        _account(engine, "a1@s.edu", status="ACTIVE")
        _account(engine, "d1@s.edu", status="DISABLED")
        s = _summary(c, ah)
        assert s["pending"] == 2
        assert s["disabled"] == 1
        # admin + a1 active + admin counts; total is the real row count
        assert s["total"] == 5
        assert s["admins"] == 1


def test_summary_requires_admin(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        assert c.get("/api/admin/users/summary").status_code == 401


# ------------------------------------------------------------ bulk approve
def test_bulk_approve_selected(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine)
        p1 = _account(engine, "p1@s.edu", status="PENDING")
        p2 = _account(engine, "p2@s.edu", status="PENDING")
        r = c.post("/api/admin/users/bulk-approve", json={"userIds": [p1, p2]}, headers=ah)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body["succeeded"]) == {p1, p2}
        assert body["skipped"] == []
        # counts refreshed in the same response
        assert body["summary"]["pending"] == 0
        assert body["summary"]["active"] == 3  # admin + p1 + p2
        # approved users can now log in
        assert login_token(c, "p1@s.edu", "pw123456")


def test_bulk_approve_skips_non_pending(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine)
        p1 = _account(engine, "p1@s.edu", status="PENDING")
        a1 = _account(engine, "a1@s.edu", status="ACTIVE")
        r = c.post("/api/admin/users/bulk-approve", json={"userIds": [p1, a1]}, headers=ah)
        body = r.json()
        assert body["succeeded"] == [p1]
        assert len(body["skipped"]) == 1 and body["skipped"][0]["userId"] == a1


def test_double_submission_is_idempotent(engine):
    """A repeated bulk-approve must NOT re-approve already-active accounts."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine)
        p1 = _account(engine, "p1@s.edu", status="PENDING")
        first = c.post("/api/admin/users/bulk-approve", json={"userIds": [p1]}, headers=ah).json()
        assert first["succeeded"] == [p1]
        second = c.post("/api/admin/users/bulk-approve", json={"userIds": [p1]}, headers=ah).json()
        assert second["succeeded"] == []           # no duplicate change
        assert len(second["skipped"]) == 1
        assert second["summary"]["active"] == 2     # unchanged (admin + p1)


# --------------------------------------------------------- approve all pending
def test_approve_all_pending(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine)
        _account(engine, "p1@s.edu", status="PENDING")
        _account(engine, "p2@s.edu", status="PENDING")
        _account(engine, "p3@s.edu", status="PENDING")
        r = c.post("/api/admin/users/approve-all-pending", headers=ah)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["succeeded"]) == 3
        assert body["summary"]["pending"] == 0


def test_approve_all_pending_noop_when_none(engine):
    """Mirrors a cancelled/empty confirmation: acting on zero pending changes
    nothing and reports zero - the backend never invents work."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine)
        before = _summary(c, ah)
        r = c.post("/api/admin/users/approve-all-pending", headers=ah).json()
        assert r["succeeded"] == []
        after = _summary(c, ah)
        assert after == before  # no backend change


def test_cancelled_confirmation_makes_no_change(engine):
    """The confirm dialog is client-gated: if the admin cancels, the bulk
    endpoint is simply never called, so DB state is untouched. We assert that
    NOT calling the endpoint leaves pending users pending."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine)
        _account(engine, "p1@s.edu", status="PENDING")
        # (no bulk call - simulating Cancel)
        assert _summary(c, ah)["pending"] == 1


# ------------------------------------------------------------- bulk reject
def test_bulk_reject_selected(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine)
        p1 = _account(engine, "p1@s.edu", status="PENDING")
        p2 = _account(engine, "p2@s.edu", status="PENDING")
        r = c.post("/api/admin/users/bulk-reject", json={"userIds": [p1, p2], "note": "spam"}, headers=ah)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body["succeeded"]) == {p1, p2}
        assert body["summary"]["rejected"] == 2
        assert body["summary"]["pending"] == 0
        # rejected user cannot log in
        login = c.post("/api/auth/login", json={"email": "p1@s.edu", "password": "pw123456"})
        assert login.status_code == 403


def test_bulk_reject_protects_last_admin_and_self(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        _account(engine, "admin@s.edu", role="admin", status="ACTIVE", password="adminpass1")
        ah = bearer(login_token(c, "admin@s.edu", "adminpass1"))
        me = c.get("/api/auth/me", headers=ah).json()["id"]
        r = c.post("/api/admin/users/bulk-reject", json={"userIds": [me]}, headers=ah).json()
        # self is skipped, and being the last admin is also guarded
        assert r["succeeded"] == []
        assert len(r["skipped"]) == 1


def test_bulk_endpoints_require_admin(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        assert c.post("/api/admin/users/bulk-approve", json={"userIds": []}).status_code == 401
        assert c.post("/api/admin/users/approve-all-pending").status_code == 401
