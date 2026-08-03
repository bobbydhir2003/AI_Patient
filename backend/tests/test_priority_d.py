"""Priority D tests: account approval lifecycle, role management + protections,
audit, and admin-user RBAC."""
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.models import User
from tests.conftest import FakeOpenAIClient, bearer, make_client
from tests.test_auth import auth_header, login_token


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


def _register_pending(client, email, password="studpass1", number="P1"):
    r = client.post("/api/auth/register", json={"fullName": "P", "email": email, "password": password, "studentNumber": number})
    assert r.status_code == 201 and r.json()["status"] == "pending"


def _admin_headers(client, engine, email="admin@school.edu", role="admin"):
    _account(engine, email, role=role, status="ACTIVE", password="adminpass1")
    return bearer(login_token(client, email, "adminpass1"))


def _find_user_id(client, headers, email):
    users = client.get("/api/admin/users", headers=headers).json()
    return next(u["id"] for u in users if u["email"] == email)


# ============================ ACCOUNT FLOW ============================
def test_approve_flow_then_login(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        _register_pending(c, "new@school.edu")
        # pending cannot login
        assert c.post("/api/auth/login", json={"email": "new@school.edu", "password": "studpass1"}).status_code == 403
        ah = _admin_headers(c, engine)
        uid = _find_user_id(c, ah, "new@school.edu")
        appr = c.post(f"/api/admin/users/{uid}/approve", headers=ah)
        assert appr.status_code == 200 and appr.json()["accountStatus"] == "ACTIVE"
        # approved can login
        assert c.post("/api/auth/login", json={"email": "new@school.edu", "password": "studpass1"}).status_code == 200


def test_reject_then_cannot_login(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        _register_pending(c, "rej@school.edu")
        ah = _admin_headers(c, engine)
        uid = _find_user_id(c, ah, "rej@school.edu")
        r = c.post(f"/api/admin/users/{uid}/reject", json={"note": "not a student"}, headers=ah)
        assert r.json()["accountStatus"] == "REJECTED"
        assert r.json()["reviewedBy"] == "admin@school.edu" and r.json()["reviewNote"] == "not a student"
        login = c.post("/api/auth/login", json={"email": "rej@school.edu", "password": "studpass1"})
        assert login.status_code == 403 and login.json()["error"]["code"] == "account_rejected"


def test_disable_and_enable(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        target = _account(engine, "act@school.edu", role="student", status="ACTIVE", password="studpass1")
        ah = _admin_headers(c, engine)
        c.post(f"/api/admin/users/{target}/disable", json={"note": "policy"}, headers=ah)
        d = c.post("/api/auth/login", json={"email": "act@school.edu", "password": "studpass1"})
        assert d.status_code == 403 and d.json()["error"]["code"] == "account_disabled"
        c.post(f"/api/admin/users/{target}/enable", headers=ah)
        assert c.post("/api/auth/login", json={"email": "act@school.edu", "password": "studpass1"}).status_code == 200


def test_existing_active_admin_still_works(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine, email="stillworks@school.edu")
        assert c.get("/api/admin/dashboard", headers=ah).status_code == 200


# ============================ ROLE MANAGEMENT ============================
def test_admin_can_promote_student_to_admin_and_audit(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        stud = _account(engine, "s@school.edu", role="student", status="ACTIVE")
        ah = _admin_headers(c, engine)
        r = c.post(f"/api/admin/users/{stud}/role", json={"role": "admin"}, headers=ah)
        assert r.status_code == 200 and r.json()["role"] == "admin"
        # audited
        logs = c.get("/api/admin/audit-logs", headers=ah).json()["items"]
        assert any(l["actionType"] == "ROLE_CHANGED" for l in logs)


def test_normal_admin_cannot_grant_super_admin(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        stud = _account(engine, "s2@school.edu", role="student", status="ACTIVE")
        ah = _admin_headers(c, engine)  # normal admin
        r = c.post(f"/api/admin/users/{stud}/role", json={"role": "super_admin"}, headers=ah)
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


def test_super_admin_can_promote_and_demote_admin(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        sah = _admin_headers(c, engine, email="super@school.edu", role="super_admin")
        admin_id = _account(engine, "a3@school.edu", role="admin", status="ACTIVE")
        r = c.post(f"/api/admin/users/{admin_id}/role", json={"role": "super_admin"}, headers=sah)
        assert r.json()["role"] == "super_admin"
        r2 = c.post(f"/api/admin/users/{admin_id}/role", json={"role": "admin"}, headers=sah)
        assert r2.json()["role"] == "admin"


def test_cannot_change_own_role(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        _account(engine, "self@school.edu", role="admin", status="ACTIVE", password="adminpass1")
        ah = bearer(login_token(c, "self@school.edu", "adminpass1"))
        me_id = _find_user_id(c, ah, "self@school.edu")
        r = c.post(f"/api/admin/users/{me_id}/role", json={"role": "student"}, headers=ah)
        assert r.status_code == 403  # self role change blocked (self-lockout protection)


def test_last_super_admin_protected(engine):
    """The final active super admin cannot be disabled, rejected or demoted away."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine, email="adminx@school.edu")  # a normal admin actor
        only_super = _account(engine, "only@school.edu", role="super_admin", status="ACTIVE")
        # disable the ONLY active super admin -> blocked
        assert c.post(f"/api/admin/users/{only_super}/disable", json={}, headers=ah).status_code == 403
        # a SECOND active super admin makes disabling the first allowed again
        second = _account(engine, "second_super@school.edu", role="super_admin", status="ACTIVE")
        assert c.post(f"/api/admin/users/{only_super}/disable", json={}, headers=ah).status_code == 200
        # now 'second' is the only active super admin -> protected again
        assert c.post(f"/api/admin/users/{second}/disable", json={}, headers=ah).status_code == 403


# ============================ SECURITY ============================
def test_student_cannot_manage_users(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        from tests.conftest import register_student
        h = bearer(register_student(c, email="stud_sec@school.edu")["accessToken"])
        assert c.get("/api/admin/users", headers=h).status_code == 403
        assert c.get("/api/admin/users").status_code == 401  # anon
        # no user-role change without admin
        assert c.post("/api/admin/users/x/role", json={"role": "admin"}, headers=h).status_code == 403


def test_require_student_still_enforced_for_admin(engine):
    """Admins still cannot create student interview sessions (RBAC preserved)."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine, email="rbac_admin@school.edu")
        r = c.post("/api/sessions", json={"studentName": "x", "caseId": "camden"}, headers=ah)
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
