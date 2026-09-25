"""Super Admin tier: role dependencies, the Super Admin portal login, User
Accounts protections for super_admin accounts, and the bootstrap command.

Per-feature authorization (survey resets, system, traffic, load tests, AI usage,
runtime config) is also covered in each feature's own test module."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.security import verify_password
from app.models import AuditLog, Student, User
from tests.conftest import FakeOpenAIClient, bearer, make_client
from tests.test_auth import login_token, make_admin, make_super_admin, register

SUPER_ENDPOINTS = [
    "/api/admin/survey-resets",
    "/api/admin/system/overview",
    "/api/admin/system/traffic/overview",
    "/api/admin/system/load-tests/config",
    "/api/admin/usage/summary",
    "/api/admin/runtime/ai-configuration",
]
ADMIN_ENDPOINTS = ["/api/admin/dashboard", "/api/admin/users", "/api/admin/student-data"]


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def c(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as client:
        yield client


def _sa(c, engine, email="root@school.edu"):
    uid = make_super_admin(engine, email=email)
    return uid, bearer(login_token(c, email, "superpass1"))


def _adm(c, engine, email="plain@school.edu"):
    uid = make_admin(engine, email=email)
    return uid, bearer(login_token(c, email, "adminpass1"))


def _get(engine, model, id_):
    db = _factory(engine)()
    try:
        return db.get(model, id_)
    finally:
        db.close()


def _set_role(engine, user_id, role):
    db = _factory(engine)()
    try:
        db.get(User, user_id).role = role
        db.commit()
    finally:
        db.close()


# ============================================================ authorization matrix
def test_status_matrix(c, engine):
    register(c, email="stud@school.edu", password="studpass1", number="S1")
    student = bearer(login_token(c, "stud@school.edu", "studpass1"))
    _, admin = _adm(c, engine)
    _, sup = _sa(c, engine)
    for path in SUPER_ENDPOINTS:
        assert c.get(path).status_code == 401, path
        assert c.get(path, headers=student).status_code == 403, path
        r = c.get(path, headers=admin)
        assert r.status_code == 403 and r.json()["error"]["message"] == "Super Admin access required.", path
        assert c.get(path, headers=sup).status_code == 200, path
    for path in ADMIN_ENDPOINTS:
        assert c.get(path, headers=admin).status_code == 200, path
        assert c.get(path, headers=sup).status_code == 200, path  # super admin is also an admin
        assert c.get(path, headers=student).status_code == 403, path
    assert c.get("/api/admin/no-such-route", headers=sup).status_code == 404


def test_role_is_read_from_the_database_not_the_token(c, engine):
    """A token minted while the account was a normal admin gains super access
    only once the STORED role changes, and loses it again on demotion: the JWT
    role claim is never trusted."""
    uid, admin = _adm(c, engine)
    assert c.get("/api/admin/system/overview", headers=admin).status_code == 403
    _set_role(engine, uid, "super_admin")
    assert c.get("/api/admin/system/overview", headers=admin).status_code == 200
    assert c.get("/api/auth/me", headers=admin).json()["role"] == "super_admin"
    _set_role(engine, uid, "admin")
    assert c.get("/api/admin/system/overview", headers=admin).status_code == 403


def test_super_admin_can_use_the_simulator(c, engine):
    _, sup = _sa(c, engine)
    r = c.post("/api/sessions", json={"studentName": "Root", "caseId": "camden"}, headers=sup)
    assert r.status_code == 201, r.text


# ================================================================== portal login
PORTAL = "/api/auth/superadmin/login"


def _portal(c, email, password):
    return c.post(PORTAL, json={"email": email, "password": password})


def test_superadmin_login_issues_token_only_to_super_admins(c, engine):
    make_super_admin(engine, email="root@school.edu")
    r = _portal(c, "root@school.edu", "superpass1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == "super_admin"
    assert c.get("/api/admin/system/overview", headers=bearer(body["accessToken"])).status_code == 200


def test_superadmin_login_failures_are_indistinguishable(c, engine):
    """Nonexistent user, wrong password, student, normal admin, and a disabled
    super admin ALL get the byte-identical generic 401 and no token, so the
    portal never reveals that credentials were valid for another role."""
    make_super_admin(engine, email="root@school.edu")
    make_admin(engine, email="plain@school.edu")
    register(c, email="stud@school.edu", password="studpass1", number="S1")
    off = make_super_admin(engine, email="off@school.edu")
    db = _factory(engine)()
    try:
        u = db.get(User, off)
        u.account_status, u.is_active = "DISABLED", False
        db.commit()
    finally:
        db.close()

    attempts = {
        "nonexistent": _portal(c, "ghost@school.edu", "whatever-pass"),
        "wrong_password": _portal(c, "root@school.edu", "wrong-password"),
        "student": _portal(c, "stud@school.edu", "studpass1"),
        "normal_admin": _portal(c, "plain@school.edu", "adminpass1"),
        "disabled_super_admin": _portal(c, "off@school.edu", "superpass1"),
    }
    reference = attempts["nonexistent"]
    assert reference.status_code == 401
    for case, r in attempts.items():
        assert r.status_code == 401, case
        assert r.json() == reference.json(), case
        assert "accessToken" not in r.json(), case
        assert "Super Admin" not in r.text, case
    assert reference.json()["error"]["message"] == "Incorrect email or password."
    # ...and it is exactly what the normal login says for a wrong password.
    normal = c.post("/api/auth/login", json={"email": "ghost@school.edu", "password": "x-wrong"})
    assert normal.status_code == 401 and normal.json() == reference.json()


def test_superadmin_login_denial_does_not_touch_last_login(c, engine):
    uid = make_admin(engine, email="plain@school.edu")
    _portal(c, "plain@school.edu", "adminpass1")
    assert _get(engine, User, uid).last_login_at is None


def test_normal_admin_can_still_use_the_normal_login(c, engine):
    make_admin(engine, email="plain@school.edu")
    _portal(c, "plain@school.edu", "adminpass1")
    r = c.post("/api/auth/login", json={"email": "plain@school.edu", "password": "adminpass1"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"


def test_super_admin_may_also_use_the_normal_login(c, engine):
    make_super_admin(engine, email="root@school.edu")
    r = c.post("/api/auth/login", json={"email": "root@school.edu", "password": "superpass1"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "super_admin"


# ============================================================ event visibility
def _audit_rows(engine, rows):
    from app.repositories.audit_repository import AuditRepository

    db = _factory(engine)()
    try:
        repo = AuditRepository(db)
        for action, record_type in rows:
            repo.record(
                admin_user_id=None, admin_email="x@school.edu", action_type=action,
                record_type=record_type, record_id="r1", description=f"{action}/{record_type} event",
            )
        db.commit()
    finally:
        db.close()


PRIVILEGED_ROWS = [
    ("credential_replaced", "credential"),
    ("ai_config_updated", "openai_config"),
    ("config_restored", "openai_config"),
    ("load_test_started", "load_test"),
    ("load_test_stopped", "load_test"),
    ("survey_reset", "student"),
    ("survey_reset", "survey_reset_bulk"),
    ("ROLE_CHANGED", "super_admin_user"),
    ("ACCOUNT_DISABLED", "super_admin_user"),
    ("voice_updated", "voice"),
]
ACADEMIC_ROWS = [
    ("ACCOUNT_APPROVED", "user"),
    ("ROLE_CHANGED", "user"),
    ("student_archived", "student"),
    ("session_deleted", "session"),
    ("assessment_deleted", "assessment"),
]


def _actions(resp_items, key):
    return sorted(i[key] for i in resp_items)


def test_activity_log_and_notifications_are_role_aware(c, engine):
    _, admin = _adm(c, engine)
    _, sup = _sa(c, engine)
    _audit_rows(engine, PRIVILEGED_ROWS + ACADEMIC_ROWS)

    a_log = c.get("/api/admin/audit-logs?page_size=100", headers=admin).json()
    s_log = c.get("/api/admin/audit-logs?page_size=100", headers=sup).json()
    assert _actions(a_log["items"], "actionType") == sorted(a for a, _ in ACADEMIC_ROWS)
    assert a_log["total"] == len(ACADEMIC_ROWS)  # totals/paging never hint at hidden rows
    assert s_log["total"] == len(PRIVILEGED_ROWS) + len(ACADEMIC_ROWS)

    a_feed = c.get("/api/admin/notifications", headers=admin).json()["notifications"]
    s_feed = c.get("/api/admin/notifications", headers=sup).json()["notifications"]
    privileged_msgs = {f"{a}/{r} event" for a, r in PRIVILEGED_ROWS}
    assert not privileged_msgs & {n["message"] for n in a_feed}
    assert privileged_msgs <= {n["message"] for n in s_feed}
    assert {f"{a}/{r} event" for a, r in ACADEMIC_ROWS} <= {n["message"] for n in a_feed}


def test_real_privileged_actions_are_hidden_from_admin_but_logged(c, engine, monkeypatch):
    """End to end: actions really taken by a super admin (credential test,
    super-admin account change) are recorded, visible to super admins, and
    never reach a normal admin; the admin's own account actions still do."""
    _, admin = _adm(c, engine)
    _, sup = _sa(c, engine)
    other_sup = make_super_admin(engine, email="root2@school.edu")
    target = make_admin(engine, email="t@school.edu")
    assert c.post(f"/api/admin/users/{other_sup}/disable", json={}, headers=sup).status_code == 200
    assert c.post(f"/api/admin/users/{target}/role", json={"role": "student"}, headers=admin).status_code == 200

    a_items = c.get("/api/admin/audit-logs?page_size=100", headers=admin).json()["items"]
    s_items = c.get("/api/admin/audit-logs?page_size=100", headers=sup).json()["items"]
    assert [i["recordId"] for i in a_items] == [target]
    assert {other_sup, target} <= {i["recordId"] for i in s_items}
    # Nothing was deleted: the privileged row exists in the table.
    db = _factory(engine)()
    try:
        assert db.execute(select(AuditLog).where(AuditLog.record_id == other_sup)).scalar_one()
    finally:
        db.close()


def test_audit_repository_hides_privileged_rows_by_default(engine):
    from app.repositories.audit_repository import AuditRepository

    _audit_rows(engine, PRIVILEGED_ROWS + ACADEMIC_ROWS)
    db = _factory(engine)()
    try:
        _, default_total = AuditRepository(db).list(limit=100, offset=0)
        _, full_total = AuditRepository(db).list(limit=100, offset=0, include_privileged=True)
    finally:
        db.close()
    assert default_total == len(ACADEMIC_ROWS)
    assert full_total == len(PRIVILEGED_ROWS) + len(ACADEMIC_ROWS)


# ================================================================ user accounts
def test_nobody_can_assign_super_admin_through_the_api(c, engine):
    admin_id, admin = _adm(c, engine)
    _, sup = _sa(c, engine)
    target = make_admin(engine, email="t@school.edu")
    for headers in (admin, sup):
        assert c.post(f"/api/admin/users/{target}/role", json={"role": "super_admin"}, headers=headers).status_code == 422
    # Self-promotion via body manipulation is refused too.
    assert c.post(f"/api/admin/users/{admin_id}/role", json={"role": "super_admin"}, headers=admin).status_code == 422
    assert _get(engine, User, target).role == "admin"
    assert _get(engine, User, admin_id).role == "admin"


def test_service_layer_also_refuses_super_admin_assignment(engine):
    """Defense in depth: even bypassing request validation, the service refuses."""
    from app.core.exceptions import ForbiddenError
    from app.services import user_admin_service

    sup_id = make_super_admin(engine, email="root@school.edu")
    target = make_admin(engine, email="t@school.edu")
    db = _factory(engine)()
    try:
        with pytest.raises(ForbiddenError):
            user_admin_service.change_role(db, db.get(User, sup_id), target, "super_admin")
    finally:
        db.close()


def test_admin_cannot_touch_a_super_admin_account(c, engine):
    _, admin = _adm(c, engine)
    sup_id, _ = _sa(c, engine)
    _sa(c, engine, email="root2@school.edu")  # so "last super admin" is never the reason
    for action, body in (
        ("role", {"role": "admin"}),
        ("role", {"role": "student"}),
        ("disable", {}),
        ("reject", {}),
        ("enable", None),
        ("approve", None),
    ):
        kwargs = {"json": body} if body is not None else {}
        r = c.post(f"/api/admin/users/{sup_id}/{action}", headers=admin, **kwargs)
        assert r.status_code == 403, (action, r.status_code, r.text)
    for path, body in (
        ("bulk-reject", {"userIds": [sup_id]}),
        ("bulk-delete", {"userIds": [sup_id], "confirm": "DELETE"}),
        ("bulk-approve", {"userIds": [sup_id]}),
    ):
        r = c.post(f"/api/admin/users/{path}", json=body, headers=admin)
        assert r.status_code == 200, r.text
        assert r.json()["succeeded"] == [], path
    after = _get(engine, User, sup_id)
    assert after is not None and after.role == "super_admin" and after.account_status == "ACTIVE"
    reasons = {s["reason"] for s in c.post(
        "/api/admin/users/bulk-reject", json={"userIds": [sup_id]}, headers=admin
    ).json()["skipped"]}
    assert reasons == {"protected_super_admin"}


def test_admin_cannot_archive_or_delete_student_profile_of_a_super_admin(c, engine):
    """A super admin promoted from a student keeps a Student profile; archiving
    or deleting that profile would disable/delete the privileged login."""
    _, admin = _adm(c, engine)
    db = _factory(engine)()
    try:
        s = Student(name="Promoted", student_number="P1", email="p@school.edu")
        db.add(s)
        db.flush()
        from app.core.security import hash_password
        u = User(email="p@school.edu", password_hash=hash_password("superpass1"), full_name="P",
                 role="super_admin", student_id=s.id, is_active=True)
        db.add(u)
        db.commit()
        sid, uid = s.id, u.id
    finally:
        db.close()
    assert c.patch(f"/api/admin/students/{sid}/status", json={"isActive": False}, headers=admin).status_code == 403
    assert c.request("DELETE", f"/api/admin/students/{sid}", json={"confirm": "DELETE"}, headers=admin).status_code == 403
    assert _get(engine, User, uid) is not None and _get(engine, User, uid).is_active is True


def test_admin_still_manages_normal_accounts(c, engine):
    _, admin = _adm(c, engine)
    target = make_admin(engine, email="other@school.edu")
    assert c.post(f"/api/admin/users/{target}/role", json={"role": "student"}, headers=admin).status_code == 200
    assert c.post(f"/api/admin/users/{target}/role", json={"role": "admin"}, headers=admin).status_code == 200
    assert c.post(f"/api/admin/users/{target}/disable", json={}, headers=admin).status_code == 200


def test_super_admin_manages_super_admins_but_never_the_last_one(c, engine):
    sup_id, sup = _sa(c, engine)
    other = make_super_admin(engine, email="root2@school.edu")
    # Another super admin can be demoted (and it is audited)...
    r = c.post(f"/api/admin/users/{other}/role", json={"role": "admin"}, headers=sup)
    assert r.status_code == 200 and r.json()["role"] == "admin"
    # ...but self-changes and removing the last active super admin are refused.
    assert c.post(f"/api/admin/users/{sup_id}/role", json={"role": "admin"}, headers=sup).status_code == 403
    assert c.post(f"/api/admin/users/{sup_id}/disable", json={}, headers=sup).status_code == 403
    db = _factory(engine)()
    try:
        audit = db.execute(select(AuditLog).where(AuditLog.record_id == other)).scalars().all()
        assert any(a.action_type == "ROLE_CHANGED" and "super_admin -> admin" in a.description for a in audit)
    finally:
        db.close()


def test_last_super_admin_guard_in_service(engine):
    from app.core.exceptions import ForbiddenError
    from app.services import user_admin_service

    only = make_super_admin(engine, email="root@school.edu")
    other_sup = make_super_admin(engine, email="root2@school.edu")
    db = _factory(engine)()
    try:
        actor = db.get(User, other_sup)
        # Disable one of two: allowed.
        user_admin_service.disable(db, actor, only)
        # Now `actor` is the last active super admin: another super admin
        # cannot exist to remove it, and it cannot remove itself.
        with pytest.raises(ForbiddenError):
            user_admin_service.disable(db, actor, other_sup)
        res = user_admin_service.bulk_delete(db, actor, [other_sup], "DELETE")
        assert res["succeeded"] == []
    finally:
        db.close()


# ================================================================== bootstrap
PW = "a-very-long-test-pass"


@pytest.fixture()
def bootstrap(engine, monkeypatch):
    """create_super_admin against the test DB with a scripted hidden prompt.
    Records every prompt so tests can prove it asked twice."""
    import scripts.create_super_admin as cmd

    prompts: list[str] = []
    answers = {"values": [PW, PW]}

    def fake_getpass(prompt=""):
        prompts.append(prompt)
        return answers["values"].pop(0)

    monkeypatch.setattr(cmd, "get_session_factory", lambda: _factory(engine))
    monkeypatch.setattr(cmd, "_interactive", lambda: True)
    monkeypatch.setattr(cmd.getpass, "getpass", fake_getpass)
    cmd.prompts = prompts
    cmd.answers = answers
    return cmd


def _user_by_email(engine, email):
    db = _factory(engine)()
    try:
        return db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    finally:
        db.close()


def test_bootstrap_dedicated_account_prompts_twice_and_can_log_in(c, engine, bootstrap, capsys):
    rc = bootstrap.main(["--email", "superadmin@school.edu", "--full-name", "Super Administrator"])
    assert rc == 0
    assert len(bootstrap.prompts) == 2  # password + confirmation, hidden
    out = capsys.readouterr()
    assert PW not in out.out and PW not in out.err  # never printed
    u = _user_by_email(engine, "superadmin@school.edu")
    assert (u.role, u.account_status, u.is_active, u.full_name) == (
        "super_admin", "ACTIVE", True, "Super Administrator",
    )
    assert u.password_hash != PW and verify_password(PW, u.password_hash)
    assert _portal(c, "superadmin@school.edu", PW).status_code == 200
    db = _factory(engine)()
    try:
        [row] = db.execute(select(AuditLog).where(AuditLog.record_id == u.id)).scalars().all()
        assert row.admin_email == "system:create_super_admin"
        assert row.record_type == "super_admin_user"  # super-admin-only visibility
        assert PW not in row.description
    finally:
        db.close()


def test_bootstrap_has_no_password_flag_or_env_var(engine, bootstrap, monkeypatch):
    monkeypatch.setenv("SUPER_ADMIN_PASSWORD", "from-env-should-be-ignored")
    with pytest.raises(SystemExit):
        bootstrap.main(["--email", "superadmin@school.edu", "--password", "x" * 20])
    assert bootstrap.main(["--email", "superadmin@school.edu"]) == 0
    u = _user_by_email(engine, "superadmin@school.edu")
    assert verify_password(PW, u.password_hash)  # the prompted one, not the env value


def test_bootstrap_requires_interactive_terminal(engine, bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap, "_interactive", lambda: False)
    assert bootstrap.main(["--email", "superadmin@school.edu"]) == 2
    assert _user_by_email(engine, "superadmin@school.edu") is None


def test_bootstrap_mismatch_and_short_password_rejected(engine, bootstrap):
    bootstrap.answers["values"] = [PW, PW + "x"]
    assert bootstrap.main(["--email", "superadmin@school.edu"]) == 2
    bootstrap.answers["values"] = ["short", "short"]
    assert bootstrap.main(["--email", "superadmin@school.edu"]) == 2
    assert _user_by_email(engine, "superadmin@school.edu") is None
    assert bootstrap.main(["--email", "not-an-email"]) == 2


def test_bootstrap_refuses_duplicate_then_promotes_deliberately(engine, bootstrap):
    make_admin(engine, email="superadmin@school.edu")
    assert bootstrap.main(["--email", "superadmin@school.edu"]) == 3
    assert _user_by_email(engine, "superadmin@school.edu").role == "admin"
    assert bootstrap.prompts == []  # refused before asking for a password
    # Explicit promotion keeps the existing password unless --reset-password.
    assert bootstrap.main(["--email", "superadmin@school.edu", "--promote-existing"]) == 0
    u = _user_by_email(engine, "superadmin@school.edu")
    assert u.role == "super_admin" and verify_password("adminpass1", u.password_hash)
    assert bootstrap.prompts == []
    assert bootstrap.main(
        ["--email", "superadmin@school.edu", "--promote-existing", "--reset-password"]
    ) == 0
    assert verify_password(PW, _user_by_email(engine, "superadmin@school.edu").password_hash)


# ============================================================== create_admin
@pytest.fixture()
def create_admin(engine, monkeypatch):
    import scripts.create_admin as cmd

    monkeypatch.setattr(cmd, "get_session_factory", lambda: _factory(engine))
    return cmd


def test_create_admin_refuses_to_touch_a_super_admin(engine, create_admin, capsys):
    sid = make_super_admin(engine, email="superadmin@school.edu")
    before = _get(engine, User, sid)
    rc = create_admin.main(
        ["--email", "superadmin@school.edu", "--password", "new-admin-pass", "--full-name", "X"]
    )
    assert rc == 4
    assert "Refusing to modify a Super Admin account with create_admin." in capsys.readouterr().err
    after = _get(engine, User, sid)
    assert (after.role, after.password_hash, after.account_status, after.is_active, after.full_name) == (
        before.role, before.password_hash, before.account_status, before.is_active, before.full_name,
    )


def test_create_admin_cannot_reactivate_or_demote_last_super_admin(engine, create_admin):
    """Even a disabled super admin (so not the 'last active' one) is refused:
    create_admin never becomes a side door around the super admin protections."""
    sid = make_super_admin(engine, email="superadmin@school.edu")
    db = _factory(engine)()
    try:
        u = db.get(User, sid)
        u.account_status, u.is_active = "DISABLED", False
        db.commit()
    finally:
        db.close()
    assert create_admin.main(["--email", "SuperAdmin@School.edu", "--password", "new-admin-pass"]) == 4
    after = _get(engine, User, sid)
    assert (after.role, after.account_status, after.is_active) == ("super_admin", "DISABLED", False)


def test_create_admin_still_creates_and_updates_normal_admins(engine, create_admin):
    assert create_admin.main(["--email", "new@school.edu", "--password", "adminpass9"]) == 0
    u = _user_by_email(engine, "new@school.edu")
    assert u.role == "admin" and verify_password("adminpass9", u.password_hash)
    assert create_admin.main(["--email", "new@school.edu", "--password", "adminpass10"]) == 0
    assert verify_password("adminpass10", _user_by_email(engine, "new@school.edu").password_hash)
