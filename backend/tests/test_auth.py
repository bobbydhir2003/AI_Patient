"""Authentication, RBAC and student-ownership tests."""
from sqlalchemy.orm import sessionmaker

from app.core.constants import USER_ROLE_ADMIN
from app.core.security import hash_password
from app.models import ConversationTurn, InterviewSession, Student, User


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def make_admin(engine, email="admin@school.edu", password="adminpass1"):
    db = _factory(engine)()
    try:
        user = User(
            email=email,
            password_hash=hash_password(password),
            full_name="Admin",
            role=USER_ROLE_ADMIN,
            is_active=True,
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def register(client, email="stud@school.edu", password="studpass1", number="S1", approve=True):
    """Register a student. Registration now creates a PENDING account; by default
    this helper also approves it (so tests that then log in keep working). Pass
    approve=False to observe the raw pending registration."""
    resp = client.post(
        "/api/auth/register",
        json={"fullName": "Stud Ent", "email": email, "password": password, "studentNumber": number},
    )
    if approve and resp.status_code == 201:
        engine = getattr(client, "_test_engine", None)
        if engine is not None:
            from sqlalchemy.orm import sessionmaker
            from app.models import User
            db = sessionmaker(bind=engine)()
            try:
                u = db.query(User).filter(User.email == email.strip().lower()).first()
                if u is not None:
                    u.account_status = "ACTIVE"
                    u.is_active = True
                    db.commit()
            finally:
                db.close()
    return resp


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def login_token(client, email, password):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["accessToken"]


# --------------------------------------------------------------------- register
def test_student_registration_creates_pending_account(client, engine):
    from sqlalchemy.orm import sessionmaker
    from app.models import User

    r = register(client, email="pend@school.edu", approve=False)
    assert r.status_code == 201, r.text
    body = r.json()
    # D2: no auto-login; a pending status message is returned (no token).
    assert body["status"] == "pending"
    assert "approval" in body["message"].lower()
    assert "accessToken" not in body
    # Account exists as PENDING, role student, linked to a Student profile.
    db = sessionmaker(bind=engine)()
    try:
        u = db.query(User).filter(User.email == "pend@school.edu").first()
        assert u is not None and u.account_status == "PENDING" and u.role == "student"
        assert u.is_active is False and u.student_id
    finally:
        db.close()


def test_pending_account_cannot_login(client):
    register(client, email="pending2@school.edu", approve=False)
    r = client.post("/api/auth/login", json={"email": "pending2@school.edu", "password": "studpass1"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "account_pending"


def test_duplicate_email_rejected(client):
    register(client, email="dup@school.edu")
    r = register(client, email="DUP@school.edu")  # case-insensitive
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_already_registered"


def test_password_is_hashed_not_plaintext(client, engine):
    register(client, email="hash@school.edu", password="supersecret1")
    db = _factory(engine)()
    try:
        user = db.query(User).filter(User.email == "hash@school.edu").one()
        assert user.password_hash != "supersecret1"
        assert user.password_hash.startswith("$2")  # bcrypt
    finally:
        db.close()


# --------------------------------------------------------------------- login
def test_student_login_succeeds(client):
    register(client, email="login@school.edu", password="rightpass1")
    token = login_token(client, "login@school.edu", "rightpass1")
    me = client.get("/api/auth/me", headers=auth_header(token))
    assert me.status_code == 200
    assert me.json()["email"] == "login@school.edu"


def test_admin_login_succeeds(client, engine):
    make_admin(engine, "boss@school.edu", "bosspass1")
    token = login_token(client, "boss@school.edu", "bosspass1")
    me = client.get("/api/auth/me", headers=auth_header(token))
    assert me.json()["role"] == "admin"


def test_invalid_login_is_generic(client):
    register(client, email="real@school.edu", password="correct1a")
    wrong_pw = client.post("/api/auth/login", json={"email": "real@school.edu", "password": "nope"})
    unknown = client.post("/api/auth/login", json={"email": "ghost@school.edu", "password": "nope1234"})
    assert wrong_pw.status_code == 401
    assert unknown.status_code == 401
    # Identical generic error - never reveals whether the email exists.
    assert wrong_pw.json()["error"]["code"] == "invalid_credentials"
    assert unknown.json()["error"]["message"] == wrong_pw.json()["error"]["message"]


def test_logout_requires_auth_and_succeeds(client):
    register(client, email="out@school.edu")
    token = login_token(client, "out@school.edu", "studpass1")
    assert client.post("/api/auth/logout").status_code == 401
    ok = client.post("/api/auth/logout", headers=auth_header(token))
    assert ok.status_code == 200 and ok.json()["success"] is True


# --------------------------------------------------------------------- RBAC
def test_student_cannot_access_admin_routes(client):
    register(client, email="s2@school.edu")
    token = login_token(client, "s2@school.edu", "studpass1")
    r = client.get("/api/admin/dashboard", headers=auth_header(token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_admin_routes_require_authentication(client):
    assert client.get("/api/admin/dashboard").status_code == 401
    assert client.get("/api/admin/students").status_code == 401


def test_invalid_token_rejected(client):
    r = client.get("/api/auth/me", headers=auth_header("not-a-real-token"))
    assert r.status_code == 401


# ----------------------------------------------------------- student ownership
def _seed_session(engine, student_id, case_id="carly"):
    db = _factory(engine)()
    try:
        s = InterviewSession(student_id=student_id, case_id=case_id, status="completed", locked=True)
        db.add(s)
        db.flush()
        db.add(ConversationTurn(session_id=s.id, turn_index=0, role="student", content="Hi"))
        db.add(ConversationTurn(session_id=s.id, turn_index=1, role="patient", content="Hello"))
        db.commit()
        return s.id
    finally:
        db.close()


def test_student_cannot_access_another_students_session(client, engine):
    # Student A
    register(client, email="a@school.edu", password="passa1234", number="A1")
    token_a = login_token(client, "a@school.edu", "passa1234")
    # Student B with a session
    register(client, email="b@school.edu", password="passb1234", number="B1")
    token_b = login_token(client, "b@school.edu", "passb1234")
    student_b_id = client.get("/api/auth/me", headers=auth_header(token_b)).json()["studentId"]
    session_b = _seed_session(engine, student_b_id)

    # A tries to read B's session -> 404 (existence not revealed)
    r = client.get(f"/api/students/me/sessions/{session_b}", headers=auth_header(token_a))
    assert r.status_code == 404

    # A tries B's transcript / assessment -> also blocked
    assert client.get(
        f"/api/students/me/sessions/{session_b}/transcript", headers=auth_header(token_a)
    ).status_code == 404


def test_student_sees_only_their_own_sessions(client, engine):
    register(client, email="own@school.edu", password="passown1", number="O1")
    token = login_token(client, "own@school.edu", "passown1")
    student_id = client.get("/api/auth/me", headers=auth_header(token)).json()["studentId"]
    session_id = _seed_session(engine, student_id)

    listing = client.get("/api/students/me/sessions", headers=auth_header(token))
    assert listing.status_code == 200
    ids = [s["sessionId"] for s in listing.json()]
    assert ids == [session_id]

    detail = client.get(f"/api/students/me/sessions/{session_id}", headers=auth_header(token))
    assert detail.status_code == 200
    transcript = client.get(
        f"/api/students/me/sessions/{session_id}/transcript", headers=auth_header(token)
    )
    assert [m["speaker"] for m in transcript.json()] == ["student", "patient"]
