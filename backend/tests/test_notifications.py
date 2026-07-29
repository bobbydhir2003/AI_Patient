"""Admin notifications are derived from REAL recorded activity - never hardcoded.

Unread count = audit events newer than the admin's last "mark all read".
"""
from sqlalchemy.orm import sessionmaker

from app.models import AuditLog
from tests.test_auth import auth_header, login_token, make_admin


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _add_audit(engine, admin_email, action_type, description, record_type="system", record_id=""):
    db = _factory(engine)()
    try:
        db.add(AuditLog(admin_email=admin_email, action_type=action_type, record_type=record_type,
                        record_id=record_id, description=description))
        db.commit()
    finally:
        db.close()


def _admin(client, engine):
    make_admin(engine, email="notif@school.edu", password="adminpass1")
    return login_token(client, "notif@school.edu", "adminpass1")


def test_notifications_require_admin(client):
    assert client.get("/api/admin/notifications").status_code == 401


def test_empty_feed_has_no_unread(client, engine):
    tok = _admin(client, engine)
    body = client.get("/api/admin/notifications", headers=auth_header(tok)).json()
    assert body["notifications"] == []
    assert body["unreadCount"] == 0  # not a hardcoded number


def test_unread_reflects_real_events_and_mapping(client, engine):
    tok = _admin(client, engine)
    _add_audit(engine, "someone@school.edu", "audio_cache_cleared", "Cleared audio cache (2 entries)")
    _add_audit(engine, "someone@school.edu", "voice_previewed", "Previewed patient voice for camden",
               record_type="voice", record_id="camden")
    body = client.get("/api/admin/notifications", headers=auth_header(tok)).json()
    assert body["unreadCount"] == 2  # exactly the two real events
    titles = {n["title"] for n in body["notifications"]}
    assert "Audio cache cleared" in titles
    assert "Patient voice preview used" in titles
    # derived fields present + typed
    n0 = body["notifications"][0]
    assert n0["type"] and n0["createdAt"] and n0["isRead"] is False and n0["link"]


def test_mark_all_read_zeroes_unread_and_new_activity_reappears(client, engine):
    tok = _admin(client, engine)
    _add_audit(engine, "x@school.edu", "ai_config_updated", "Updated OpenAI config: model")
    assert client.get("/api/admin/notifications", headers=auth_header(tok)).json()["unreadCount"] == 1

    r = client.post("/api/admin/notifications/read-all", headers=auth_header(tok))
    assert r.status_code == 200 and r.json()["success"] is True

    after = client.get("/api/admin/notifications", headers=auth_header(tok)).json()
    assert after["unreadCount"] == 0
    assert all(n["isRead"] for n in after["notifications"])

    # A new real event ticks the badge back up.
    _add_audit(engine, "y@school.edu", "credential_replaced", "Replaced openai API key")
    again = client.get("/api/admin/notifications", headers=auth_header(tok)).json()
    assert again["unreadCount"] == 1


def test_student_notification_links_to_student(client, engine):
    tok = _admin(client, engine)
    _add_audit(engine, "x@school.edu", "student_archived", "Archived student",
               record_type="student", record_id="stud123")
    body = client.get("/api/admin/notifications", headers=auth_header(tok)).json()
    n = body["notifications"][0]
    assert n["type"] == "student" and n["link"] == "/admin/students/stud123"
