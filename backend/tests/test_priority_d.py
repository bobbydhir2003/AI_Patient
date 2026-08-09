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


def test_invalid_role_is_rejected(engine):
    """Only student/admin are assignable. super_admin (or any other value) is
    rejected by request validation before it can reach the database."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        stud = _account(engine, "s2@school.edu", role="student", status="ACTIVE")
        ah = _admin_headers(c, engine)  # normal admin
        r = c.post(f"/api/admin/users/{stud}/role", json={"role": "super_admin"}, headers=ah)
        assert r.status_code == 422  # schema pattern ^(student|admin)$ rejects it
        # and the student's role is unchanged
        assert c.get("/api/admin/users", headers=ah).json()
        assert next(u for u in c.get("/api/admin/users", headers=ah).json()
                    if u["email"] == "s2@school.edu")["role"] == "student"


def test_any_admin_can_promote_and_demote(engine):
    """Every admin has full powers: promote a student to admin and demote an
    admin back to student."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine, email="actor@school.edu")
        target = _account(engine, "a3@school.edu", role="student", status="ACTIVE")
        r = c.post(f"/api/admin/users/{target}/role", json={"role": "admin"}, headers=ah)
        assert r.status_code == 200 and r.json()["role"] == "admin"
        r2 = c.post(f"/api/admin/users/{target}/role", json={"role": "student"}, headers=ah)
        assert r2.status_code == 200 and r2.json()["role"] == "student"


def test_cannot_change_own_role(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        _account(engine, "self@school.edu", role="admin", status="ACTIVE", password="adminpass1")
        ah = bearer(login_token(c, "self@school.edu", "adminpass1"))
        me_id = _find_user_id(c, ah, "self@school.edu")
        r = c.post(f"/api/admin/users/{me_id}/role", json={"role": "student"}, headers=ah)
        assert r.status_code == 403  # self role change blocked (self-lockout protection)


def test_last_admin_protected(engine):
    """The final ACTIVE administrator cannot be disabled or demoted away, so the
    system can never be left without an administrator. Verified at the service
    layer because the endpoint actor must themselves be an active admin (which
    would otherwise always count as a remaining admin)."""
    from app.core.exceptions import ForbiddenError
    from app.services import user_admin_service as uas

    db = sessionmaker(bind=engine)()
    try:
        # Only ONE active admin ('sole'); the actor is an admin whose account is
        # DISABLED, so it does not count toward the active-admin total.
        sole_id = _account(engine, "sole@school.edu", role="admin", status="ACTIVE")
        actor_id = _account(engine, "actor@school.edu", role="admin", status="DISABLED")
        sole = db.get(User, sole_id)
        actor = db.get(User, actor_id)

        # Disabling the last active admin is blocked.
        try:
            uas.disable(db, actor, sole.id)
            assert False, "expected ForbiddenError"
        except ForbiddenError:
            pass
        # Demoting the last active admin to student is blocked.
        try:
            uas.change_role(db, actor, sole.id, "student")
            assert False, "expected ForbiddenError"
        except ForbiddenError:
            pass

        # A SECOND active admin removes the protection.
        _account(engine, "second@school.edu", role="admin", status="ACTIVE")
        uas.disable(db, actor, sole.id)  # now allowed
        assert db.get(User, sole.id).account_status == "DISABLED"
    finally:
        db.close()


# ============================ SECURITY ============================
def test_student_cannot_manage_users(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        from tests.conftest import register_student
        h = bearer(register_student(c, email="stud_sec@school.edu")["accessToken"])
        assert c.get("/api/admin/users", headers=h).status_code == 403
        assert c.get("/api/admin/users").status_code == 401  # anon
        # no user-role change without admin
        assert c.post("/api/admin/users/x/role", json={"role": "admin"}, headers=h).status_code == 403


def test_admin_can_create_practice_session_excluded_from_analytics(engine):
    """Admins/professors CAN run the simulator (require_simulator_access), but
    their sessions are practice ("admin_test") and never counted in student
    analytics. Unauthenticated callers are still rejected."""
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        ah = _admin_headers(c, engine, email="rbac_admin@school.edu")
        # Admin can now create a simulator session (practice profile provisioned).
        r = c.post("/api/sessions", json={"studentName": "x", "caseId": "camden"}, headers=ah)
        assert r.status_code == 201
        # ...but it does NOT pollute the academic dashboard (practice excluded).
        dash = c.get("/api/admin/dashboard", headers=ah).json()
        assert dash["totalSessions"] == 0
        assert dash["totalStudents"] == 0
        # ...and the admin session list is empty too.
        sessions = c.get("/api/admin/sessions", headers=ah).json()
        assert sessions["total"] == 0
        # Unauthenticated simulator access is still refused (401).
        assert c.post("/api/sessions", json={"studentName": "x", "caseId": "camden"}).status_code == 401
