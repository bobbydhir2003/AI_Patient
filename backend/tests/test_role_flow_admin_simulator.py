"""End-to-end role-flow + admin-simulator-access tests.

Covers the final role design:
  - Student          -> uses the simulator; blocked from all admin APIs.
  - Promoted admin   -> role=admin, is_system_admin=False; CAN use the simulator
                        (sessions flagged practice, excluded from analytics) AND
                        reach the admin dashboard.
  - System admin     -> role=admin, is_system_admin=True.
  - Super admin      -> role=super_admin; admin access preserved.
Backend RBAC (require_admin / require_simulator_access) is the real protection;
these tests exercise it directly.
"""
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.models import User
from tests.conftest import FakeOpenAIClient, bearer, make_client
from tests.test_auth import login_token, register


def _make_user(engine, email, *, role, is_system_admin=False, student_id=None, password="pw123456"):
    db = sessionmaker(bind=engine)()
    try:
        u = User(
            email=email,
            password_hash=hash_password(password),
            full_name="Test User",
            role=role,
            account_status="ACTIVE",
            is_active=True,
            is_system_admin=is_system_admin,
            student_id=student_id,
        )
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _promote_to_admin(engine, email):
    db = sessionmaker(bind=engine)()
    try:
        u = db.query(User).filter(User.email == email.strip().lower()).first()
        u.role = "admin"  # change_role does NOT touch is_system_admin -> stays False
        db.commit()
        return u.id, u.student_id
    finally:
        db.close()


def _seed_and_complete(client, token, case_id="camden"):
    h = bearer(token)
    sid = client.post("/api/sessions", json={"studentName": "Tester", "caseId": case_id}, headers=h)
    assert sid.status_code == 201, sid.text
    session_id = sid.json()["sessionId"]
    client.post(f"/api/interviews/{session_id}/messages",
                json={"text": "Hi, how are you today?", "caseId": case_id}, headers=h)
    done = client.post(f"/api/sessions/{session_id}/complete", headers=h)
    assert done.status_code == 200, done.text
    return session_id


# ------------------------------------------------------------- TEST 1: STUDENT
def test_student_uses_simulator_but_is_blocked_from_admin(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        register(c, email="stud1@school.edu", number="S100")
        tok = login_token(c, "stud1@school.edu", "studpass1")
        me = c.get("/api/auth/me", headers=bearer(tok)).json()
        assert me["role"] == "student" and me["isSystemAdmin"] is False

        # Full simulator flow works.
        _seed_and_complete(c, tok)
        assert c.get("/api/students/me/sessions", headers=bearer(tok)).status_code == 200

        # Admin surface is denied (403), not merely hidden.
        assert c.get("/api/admin/dashboard", headers=bearer(tok)).status_code == 403
        assert c.get("/api/admin/users", headers=bearer(tok)).status_code == 403
        assert c.get("/api/admin/students", headers=bearer(tok)).status_code == 403


# ------------------------------------------------------- TEST 2: PROMOTED ADMIN
def test_promoted_admin_runs_practice_and_reaches_dashboard(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        # A real student (their prior session is a real academic record).
        register(c, email="prof@school.edu", number="P200")
        stud_tok = login_token(c, "prof@school.edu", "studpass1")
        _seed_and_complete(c, stud_tok)  # 1 REAL session for this person

        # Promote to admin (is_system_admin stays False).
        _promote_to_admin(engine, "prof@school.edu")
        adm_tok = login_token(c, "prof@school.edu", "studpass1")
        me = c.get("/api/auth/me", headers=bearer(adm_tok)).json()
        assert me["role"] == "admin" and me["isSystemAdmin"] is False

        # Dashboard reflects the 1 real session BEFORE any admin practice.
        before = c.get("/api/admin/dashboard", headers=bearer(adm_tok)).json()
        assert before["totalSessions"] == 1

        # Admin can now run the full simulator (practice sessions).
        _seed_and_complete(c, adm_tok)
        _seed_and_complete(c, adm_tok)

        # ...but practice sessions never inflate the academic dashboard.
        after = c.get("/api/admin/dashboard", headers=bearer(adm_tok)).json()
        assert after["totalSessions"] == 1, "admin practice must not affect analytics"

        # Admin can reach admin management APIs.
        assert c.get("/api/admin/users", headers=bearer(adm_tok)).status_code == 200
        # ...and still see their OWN sessions via simulator self-service.
        mine = c.get("/api/students/me/sessions", headers=bearer(adm_tok)).json()
        assert len(mine) >= 1


# --------------------------------------------------------- TEST 3: SYSTEM ADMIN
def test_system_admin_flag_and_admin_access(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        _make_user(engine, "sysadmin@school.edu", role="admin", is_system_admin=True)
        tok = login_token(c, "sysadmin@school.edu", "pw123456")
        me = c.get("/api/auth/me", headers=bearer(tok)).json()
        assert me["role"] == "admin" and me["isSystemAdmin"] is True
        assert c.get("/api/admin/dashboard", headers=bearer(tok)).status_code == 200


# ---------------------------------------------------------- TEST 4: SUPER ADMIN
def test_super_admin_access_preserved(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        _make_user(engine, "root@school.edu", role="super_admin")
        tok = login_token(c, "root@school.edu", "pw123456")
        me = c.get("/api/auth/me", headers=bearer(tok)).json()
        assert me["role"] == "super_admin"
        assert c.get("/api/admin/dashboard", headers=bearer(tok)).status_code == 200
        # super_admin also has simulator access.
        assert c.post("/api/sessions", json={"studentName": "Tester", "caseId": "camden"}, headers=bearer(tok)).status_code == 201


# ----------------------------------------------------------- TEST 5: PERMISSIONS
def test_permission_matrix(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        # Unauthenticated simulator access -> 401.
        assert c.post("/api/sessions", json={"studentName": "Tester", "caseId": "camden"}).status_code == 401
        # Unauthenticated admin API -> 401.
        assert c.get("/api/admin/dashboard").status_code == 401
