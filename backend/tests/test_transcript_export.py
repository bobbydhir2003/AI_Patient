"""Admin transcript downloads: per-session PDF and filtered ZIP export."""
import base64
import csv
import glob
import io
import os
import re
import tempfile
import zipfile
import zlib
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from app.models import AssessmentRun, ConversationTurn, InterviewSession, Student
from tests.conftest import make_client
from tests.test_auth import auth_header, login_token, make_admin

CHICAGO = "America/Chicago"


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _admin(client, engine):
    make_admin(engine, "admin@school.edu", "adminpass1")
    return auth_header(login_token(client, "admin@school.edu", "adminpass1"))


def _student(engine, name, number="S1"):
    db = _factory(engine)()
    try:
        s = Student(name=name, student_number=number, email=f"{number}@x.edu")
        db.add(s)
        db.commit()
        return s.id
    finally:
        db.close()


def _session(engine, student_id, case_id, started, turns, *, status="completed",
             practice=False, level=None, minutes=12):
    db = _factory(engine)()
    try:
        s = InterviewSession(
            student_id=student_id, case_id=case_id, status=status, locked=True,
            is_practice=practice, started_at=started,
            completed_at=started + timedelta(minutes=minutes) if status == "completed" else None,
        )
        db.add(s)
        db.flush()
        for i, (role, text) in enumerate(turns):
            db.add(ConversationTurn(
                session_id=s.id, turn_index=i, role=role, content=text,
                created_at=started + timedelta(seconds=10 * i),
            ))
        if level:
            db.add(AssessmentRun(session_id=s.id, case_id=case_id, status="COMPLETE", overall_level=level))
        db.commit()
        return s.id
    finally:
        db.close()


def _pdf_text(data: bytes) -> str:
    """Decode reportlab's content streams (ASCII85 + Flate) to searchable text."""
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    out = []
    for m in re.finditer(rb"/Filter\s*\[?([^\]>]*)\]?.*?>>\s*stream\r?\n(.*?)endstream", data, re.S):
        filters, raw = m.group(1), m.group(2).strip()
        try:
            if b"ASCII85Decode" in filters:
                raw = base64.a85decode(raw[:-2] if raw.endswith(b"~>") else raw)
            if b"FlateDecode" in filters:
                raw = zlib.decompress(raw)
        except Exception:
            continue
        out.append(raw.decode("latin-1"))
    # Join the literal strings drawn by Tj/TJ operators, unescaping \( \) \\.
    text = " ".join(re.findall(r"\(((?:\\.|[^\\)])*)\)", "\n".join(out)))
    text = re.sub(r"\\([0-7]{3})", lambda m: bytes([int(m.group(1), 8)]).decode("cp1252"), text)
    return re.sub(r"\\(.)", r"\1", text)


def _pages(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


CARLY_TURNS = [
    ("student", "Hello Carly, my name is Zach."),
    ("patient", "Hi, nice to meet you."),
    ("student", "Can you tell me what brought you in today?"),
    ("patient", "My knee has been sore since the fall."),
]


def _seed_world(engine):
    zach = _student(engine, "Zach Pandorf", "Z1")
    claudia = _student(engine, "Claudia Wilson", "C1")
    base = datetime(2026, 9, 24, 15, 38, 14, tzinfo=timezone.utc)  # 10:38 AM CDT
    ids = {
        "zach_carly_1": _session(engine, zach, "carly", base, CARLY_TURNS, level="Developing"),
        "zach_carly_2": _session(engine, zach, "carly", base + timedelta(hours=2), CARLY_TURNS[:2]),
        # 9:23 PM CDT on 9/24 == 02:23 UTC on 9/25 (date filter must use local day)
        "claudia_camden": _session(
            engine, claudia, "camden", datetime(2026, 9, 25, 2, 23, 2, tzinfo=timezone.utc),
            [("student", "Hi Camden."), ("patient", "Hey.")],
        ),
        "practice": _session(engine, zach, "carly", base, CARLY_TURNS, practice=True),
    }
    return ids, zach, claudia


def _export(client, headers, **params):
    params.setdefault("tz", CHICAGO)
    return client.get("/api/admin/transcripts/export", headers=headers, params=params)


def _zip(resp):
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.testzip() is None
    return zf


def _csv_rows(zf, name):
    return list(csv.DictReader(io.StringIO(zf.read(name).decode("utf-8-sig"))))


# --------------------------------------------------------------------- single PDF
def test_single_session_pdf_download(client, engine):
    h = _admin(client, engine)
    ids, zach, _ = _seed_world(engine)
    r = client.get(f"/api/admin/transcripts/{ids['zach_carly_1']}/download", headers=h, params={"tz": CHICAGO})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["cache-control"] == "no-store"
    assert 'filename="Carly_Zach_Pandorf_2026-09-24_10-38-AM.pdf"' in r.headers["content-disposition"]
    text = _pdf_text(r.content)
    for expected in ["UNMC | iEXCEL", "SESSION TRANSCRIPT", "Zach Pandorf", zach[:8], ids["zach_carly_1"],
                     "September 24, 2026", "10:38 AM CDT", "12m 0s", "Completed", "CONVERSATION TRANSCRIPT",
                     "STUDENT", "CARLY", "Hello Carly, my name is Zach.", "My knee has been sore since the fall.",
                     "10:38:14 AM", "10:38:44 AM", "Page 1 of 1"]:
        assert expected in text, expected


def test_camden_pdf_and_each_repeat_session_is_separate(client, engine):
    h = _admin(client, engine)
    ids, _, _ = _seed_world(engine)
    cam = client.get(f"/api/admin/transcripts/{ids['claudia_camden']}/download", headers=h, params={"tz": CHICAGO})
    assert "Camden_Claudia_Wilson_2026-09-24_09-23-PM.pdf" in cam.headers["content-disposition"]
    assert "CAMDEN" in _pdf_text(cam.content)

    one = client.get(f"/api/admin/transcripts/{ids['zach_carly_1']}/download", headers=h, params={"tz": CHICAGO})
    two = client.get(f"/api/admin/transcripts/{ids['zach_carly_2']}/download", headers=h, params={"tz": CHICAGO})
    assert one.headers["content-disposition"] != two.headers["content-disposition"]
    assert ids["zach_carly_1"] in _pdf_text(one.content)
    assert ids["zach_carly_2"] in _pdf_text(two.content)
    assert "what brought you in" not in _pdf_text(two.content)  # its own turns only


def test_long_transcript_with_special_characters_is_complete(client, engine):
    h = _admin(client, engine)
    sid = _student(engine, "Zoë O'Brien-Łukasz", "L1")
    turns = []
    for i in range(160):
        role = "student" if i % 2 == 0 else "patient"
        turns.append((role, f"Message {i:03d} <tag> & \"quotes\" — (parens) café 😀 " + "word " * 30))
    turns.append(("patient", "FINAL-MESSAGE-MARKER"))
    sess = _session(engine, sid, "carly", datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc), turns)
    r = client.get(f"/api/admin/transcripts/{sess}/download", headers=h, params={"tz": CHICAGO})
    assert r.status_code == 200
    assert "Carly_Zoe_O_Brien-Lukasz_2026-09-24_12-00-PM.pdf" in r.headers["content-disposition"]
    text = _pdf_text(r.content)
    pages = _pages(r.content)
    assert pages > 5
    assert f"Page {pages} of {pages}" in text
    for i in range(160):
        assert f"Message {i:03d}" in text
    assert "FINAL-MESSAGE-MARKER" in text
    squashed = re.sub(r"\s+", "", text)
    assert '<tag>&"quotes"—(parens)café?' in squashed  # escaped markup, WinAnsi glyphs, emoji -> ?
    assert "Zoë O'Brien-Łukasz" not in text and "Zoë O'Brien-Lukasz" in text
    assert "Messages" in text and "161" in text


def test_single_download_unknown_or_unsafe_session_id(client, engine):
    h = _admin(client, engine)
    assert client.get("/api/admin/transcripts/deadbeef/download", headers=h).status_code == 404
    assert client.get("/api/admin/transcripts/..%2F..%2Fetc/download", headers=h).status_code in (404, 422)


# --------------------------------------------------------------------- ZIP
def test_export_all_zip_structure_and_indexes(client, engine):
    h = _admin(client, engine)
    ids, zach, claudia = _seed_world(engine)
    r = _export(client, h)
    assert re.search(r'filename="PTAI_All_Transcripts_\d{4}-\d{2}-\d{2}\.zip"', r.headers["content-disposition"])
    zf = _zip(r)
    names = set(zf.namelist())
    assert "Transcript_Index.csv" in names
    assert "Carly/Carly_Transcript_Index.csv" in names
    assert "Camden/Camden_Transcript_Index.csv" in names
    pdfs = sorted(n for n in names if n.endswith(".pdf"))
    assert pdfs == sorted([
        "Camden/Camden_Claudia_Wilson_2026-09-24_09-23-PM.pdf",
        "Carly/Carly_Zach_Pandorf_2026-09-24_10-38-AM.pdf",
        "Carly/Carly_Zach_Pandorf_2026-09-24_12-38-PM.pdf",
    ])  # practice session excluded, every real session its own PDF
    for n in pdfs:
        assert _pdf_text(zf.read(n)).count("SESSION TRANSCRIPT") == 1

    rows = _csv_rows(zf, "Transcript_Index.csv")
    assert list(rows[0].keys()) == [
        "Student Name", "Student ID", "Student Number", "Case", "Session ID", "Session Date", "Start Time",
        "End Time", "Duration", "Messages", "Status", "Assessment Level", "Transcript Filename",
    ]
    by_id = {row["Session ID"]: row for row in rows}
    assert set(by_id) == {ids["zach_carly_1"], ids["zach_carly_2"], ids["claudia_camden"]}
    first = by_id[ids["zach_carly_1"]]
    assert first["Student Name"] == "Zach Pandorf"
    assert first["Student ID"] == zach[:8]
    assert first["Messages"] == "4"
    assert first["Assessment Level"] == "Developing"
    assert first["Session Date"] == "2026-09-24"
    assert first["Status"] == "Completed"
    assert first["Transcript Filename"] == "Carly/Carly_Zach_Pandorf_2026-09-24_10-38-AM.pdf"
    assert by_id[ids["zach_carly_2"]]["Assessment Level"] == ""
    assert by_id[ids["claudia_camden"]]["Session Date"] == "2026-09-24"

    carly_rows = _csv_rows(zf, "Carly/Carly_Transcript_Index.csv")
    assert {row["Session ID"] for row in carly_rows} == {ids["zach_carly_1"], ids["zach_carly_2"]}
    assert carly_rows[0]["Transcript Filename"].startswith("Carly_Zach_Pandorf_")


def test_export_case_filter(client, engine):
    h = _admin(client, engine)
    _seed_world(engine)
    r = _export(client, h, case_id="carly")
    assert "PTAI_Carly_Transcripts_" in r.headers["content-disposition"]
    names = _zip(r).namelist()
    assert all(n == "Transcript_Index.csv" or n.startswith("Carly/") for n in names)
    assert len([n for n in names if n.endswith(".pdf")]) == 2

    cam = _zip(_export(client, h, case_id="camden")).namelist()
    assert [n for n in cam if n.endswith(".pdf")] == ["Camden/Camden_Claudia_Wilson_2026-09-24_09-23-PM.pdf"]


def test_export_date_filter_uses_local_calendar_day(client, engine):
    h = _admin(client, engine)
    _seed_world(engine)
    on_24 = [n for n in _zip(_export(client, h, date="2026-09-24")).namelist() if n.endswith(".pdf")]
    assert len(on_24) == 3  # includes the 9:23 PM CDT session stored as 9/25 UTC
    assert _export(client, h, date="2026-09-25").status_code == 404
    utc_25 = _zip(_export(client, h, date="2026-09-25", tz="UTC")).namelist()
    assert [n for n in utc_25 if n.endswith(".pdf")] == ["Camden/Camden_Claudia_Wilson_2026-09-25_02-23-AM.pdf"]


def test_export_search_filter_and_count(client, engine):
    h = _admin(client, engine)
    _seed_world(engine)
    names = _zip(_export(client, h, search="zach")).namelist()
    assert len([n for n in names if n.endswith(".pdf")]) == 2
    names = _zip(_export(client, h, search="camd")).namelist()  # case-name search
    assert [n for n in names if n.endswith(".pdf")] == ["Camden/Camden_Claudia_Wilson_2026-09-24_09-23-PM.pdf"]

    def count(**p):
        p.setdefault("tz", CHICAGO)
        r = client.get("/api/admin/transcripts/export/count", headers=h, params=p)
        assert r.status_code == 200
        return r.json()["count"]

    assert count() == 3
    assert count(case_id="carly") == 2
    assert count(search="claudia") == 1
    assert count(search="100%_") == 0  # LIKE wildcards are escaped
    assert count(case_id="carly", date="2026-09-25") == 0


def test_duplicate_minute_sessions_keep_distinct_files(client, engine):
    h = _admin(client, engine)
    sid = _student(engine, "Mykael Stoddard", "M1")
    t = datetime(2026, 9, 24, 17, 56, 4, tzinfo=timezone.utc)
    a = _session(engine, sid, "carly", t, CARLY_TURNS)
    b = _session(engine, sid, "carly", t + timedelta(seconds=20), CARLY_TURNS)
    pdfs = [n for n in _zip(_export(client, h)).namelist() if n.endswith(".pdf")]
    assert len(pdfs) == 2 and len(set(pdfs)) == 2
    assert any(a[:8] in n or b[:8] in n for n in pdfs)


def test_empty_export_returns_clear_error_and_leaves_no_temp_file(client, engine):
    h = _admin(client, engine)
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "ptai_transcripts_*.zip")))
    r = _export(client, h, case_id="sofia")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "no_transcripts_found"
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "ptai_transcripts_*.zip")))
    assert after == before


def test_export_temp_file_is_removed_after_response(client, engine):
    h = _admin(client, engine)
    _seed_world(engine)
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "ptai_transcripts_*.zip")))
    _zip(_export(client, h))
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "ptai_transcripts_*.zip")))
    assert after == before


def test_csv_formula_injection_is_neutralised(client, engine):
    h = _admin(client, engine)
    sid = _student(engine, "=HYPERLINK(\"http://x\")", "X1")
    _session(engine, sid, "carly", datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc), CARLY_TURNS)
    rows = _csv_rows(_zip(_export(client, h)), "Transcript_Index.csv")
    assert rows[0]["Student Name"].startswith("'=")


def test_invalid_filters_rejected(client, engine):
    h = _admin(client, engine)
    assert _export(client, h, case_id="../etc").status_code == 422
    assert _export(client, h, date="not-a-date").status_code == 422
    assert _export(client, h, tz="Bad Zone;rm").status_code == 422


# --------------------------------------------------------------------- auth
def test_downloads_require_admin(engine, fake_client):
    ids = None
    with make_client(engine, fake_client, authenticate=False) as anon:
        ids, _, _ = _seed_world(engine)
        assert anon.get("/api/admin/transcripts/export").status_code == 401
        assert anon.get("/api/admin/transcripts/export/count").status_code == 401
        assert anon.get(f"/api/admin/transcripts/{ids['zach_carly_1']}/download").status_code == 401

    from tests.conftest import auth_headers

    with make_client(engine, fake_client, authenticate=False) as c:
        student = auth_headers(c, email="stud@school.edu")
        assert c.get("/api/admin/transcripts/export", headers=student).status_code == 403
        assert c.get("/api/admin/transcripts/export/count", headers=student).status_code == 403
        assert c.get(f"/api/admin/transcripts/{ids['zach_carly_1']}/download", headers=student).status_code == 403
