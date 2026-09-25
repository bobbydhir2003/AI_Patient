"""Admin transcript downloads: one session as PDF, or a filtered set as a ZIP.

Every export is session based - a student who completed the same case three
times gets three separate PDFs. The session set mirrors the admin Transcripts
page (GET /admin/sessions): practice sessions are excluded, all statuses are
included, newest first. Transcript text is read straight from
conversation_turns in turn order and is never logged.
"""
from __future__ import annotations

import csv
import io
import os
import re
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.constants import CASE_IDS
from app.core.exceptions import AppError, SessionNotFoundError
from app.core.logging import get_logger
from app.models import AssessmentRun, ConversationTurn, InterviewSession, Student
from app.patient_engine.case_loader import load_all_cases
from app.services.transcript_pdf import (
    TranscriptDocument,
    TranscriptMessage,
    ascii_fold,
    render_transcript_pdf,
)

logger = get_logger(__name__)

_TEMP_PREFIX = "ptai_transcripts_"
# A finished export is deleted right after it is sent; this only catches files
# orphaned by a client disconnect or a crashed worker.
_STALE_EXPORT_SECONDS = 3600

# Sessions are processed in chunks so memory stays bounded however many
# transcripts match: only one chunk's turns are loaded at a time.
_CHUNK = 100

_INDEX_COLUMNS = [
    "Student Name",
    "Student ID",
    "Student Number",
    "Case",
    "Session ID",
    "Session Date",
    "Start Time",
    "End Time",
    "Duration",
    "Messages",
    "Status",
    "Assessment Level",
    "Transcript Filename",
]


class NoTranscriptsError(AppError):
    status_code = 404
    code = "no_transcripts_found"

    def __init__(self) -> None:
        super().__init__("No transcripts match the current filters.")


@dataclass(frozen=True)
class ExportFilters:
    case_id: str = ""
    on_date: date | None = None
    search: str = ""


@dataclass
class _SessionRow:
    id: str
    student_id: str
    student_name: str
    student_number: str
    case_id: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None


# ------------------------------------------------------------------ helpers
def resolve_timezone(name: str | None) -> tzinfo:
    """The admin's browser time zone (IANA name), so PDF times match what the
    Transcripts page shows. Unknown or missing names fall back to UTC."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc


def case_label(case_id: str) -> str:
    case = load_all_cases().get(case_id)
    if case is not None and case.display_name:
        return case.display_name
    return case_id.replace("_", " ").title()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_duration(started: datetime | None, completed: datetime | None) -> str:
    """Same rules as the admin UI's fmtDuration."""
    if started is None or completed is None:
        return ""
    seconds = int((_aware(completed) - _aware(started)).total_seconds())
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    return f"{m // 60}h {m % 60}m"


def safe_filename_part(value: str, *, fallback: str = "Unknown", max_len: int = 60) -> str:
    """ASCII-only, filesystem/zip safe: letters, digits and hyphens joined by
    underscores. Strips path separators, dots and control characters, so no
    value can escape its folder inside the archive."""
    ascii_text = ascii_fold(value or "")
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "_", ascii_text).strip("_-")
    cleaned = re.sub(r"_+", "_", cleaned)[:max_len].strip("_-")
    return cleaned or fallback


def transcript_filename(row: _SessionRow, tz: tzinfo) -> str:
    started = _aware(row.started_at)
    parts = [safe_filename_part(case_label(row.case_id)), safe_filename_part(row.student_name)]
    if started is not None:
        local = started.astimezone(tz)
        parts.append(local.strftime("%Y-%m-%d_%I-%M-%p"))
    return "_".join(parts) + ".pdf"


def _csv_cell(value: str) -> str:
    # Student names are user-supplied; neutralise spreadsheet formula injection.
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# ------------------------------------------------------------------ queries
def _filtered_stmt(filters: ExportFilters, tz: tzinfo):
    stmt = (
        select(
            InterviewSession.id,
            InterviewSession.student_id,
            Student.name,
            Student.student_number,
            InterviewSession.case_id,
            InterviewSession.status,
            InterviewSession.started_at,
            InterviewSession.completed_at,
        )
        .join(Student, Student.id == InterviewSession.student_id, isouter=True)
        .where(InterviewSession.is_practice.is_(False))
    )
    if filters.case_id:
        stmt = stmt.where(InterviewSession.case_id == filters.case_id)
    if filters.on_date is not None:
        # The date is a calendar day in the admin's time zone (as displayed).
        start = datetime.combine(filters.on_date, time.min, tzinfo=tz)
        end = datetime.combine(filters.on_date + timedelta(days=1), time.min, tzinfo=tz)
        stmt = stmt.where(
            InterviewSession.started_at >= start.astimezone(timezone.utc),
            InterviewSession.started_at < end.astimezone(timezone.utc),
        )
    q = filters.search.strip().lower()
    if q:
        # Same semantics as the page's search box: student name or case name.
        matching_cases = [
            cid for cid in CASE_IDS
            if q in case_label(cid).lower() or q in cid.replace("_", " ").lower()
        ]
        clauses = [Student.name.icontains(q, autoescape=True)]
        if matching_cases:
            clauses.append(InterviewSession.case_id.in_(matching_cases))
        stmt = stmt.where(or_(*clauses))
    return stmt


def count_transcripts(db: Session, filters: ExportFilters, tz: tzinfo) -> int:
    stmt = _filtered_stmt(filters, tz)
    return int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())


def _session_rows(db: Session, filters: ExportFilters, tz: tzinfo) -> list[_SessionRow]:
    stmt = _filtered_stmt(filters, tz).order_by(InterviewSession.started_at.desc(), InterviewSession.id)
    return [
        _SessionRow(
            id=r[0], student_id=r[1], student_name=r[2] or "", student_number=r[3] or "",
            case_id=r[4], status=r[5], started_at=r[6], completed_at=r[7],
        )
        for r in db.execute(stmt).all()
    ]


def _turns_for(db: Session, session_ids: list[str]) -> dict[str, list[TranscriptMessage]]:
    grouped: dict[str, list[TranscriptMessage]] = defaultdict(list)
    rows = db.execute(
        select(
            ConversationTurn.session_id,
            ConversationTurn.role,
            ConversationTurn.speaker_label,
            ConversationTurn.content,
            ConversationTurn.created_at,
        )
        .where(ConversationTurn.session_id.in_(session_ids))
        .order_by(ConversationTurn.session_id, ConversationTurn.turn_index)
    ).all()
    for sid, role, label, content, created in rows:
        grouped[sid].append(TranscriptMessage(role=role, speaker_label=label or "", content=content, created_at=created))
    return grouped


def _latest_levels(db: Session, session_ids: list[str]) -> dict[str, str]:
    """Latest assessment run's overall level per session (same rule the admin
    UI uses). Sessions without a completed level are simply absent."""
    latest: dict[str, tuple[datetime, str | None]] = {}
    rows = db.execute(
        select(AssessmentRun.session_id, AssessmentRun.created_at, AssessmentRun.overall_level)
        .where(AssessmentRun.session_id.in_(session_ids))
    ).all()
    for sid, created, level in rows:
        created = _aware(created)
        if sid not in latest or created > latest[sid][0]:
            latest[sid] = (created, level)
    return {sid: level for sid, (_, level) in latest.items() if level}


def _document(row: _SessionRow, messages: list[TranscriptMessage]) -> TranscriptDocument:
    return TranscriptDocument(
        session_id=row.id,
        student_name=row.student_name,
        student_ref=row.student_id[:8],
        student_number=row.student_number,
        case_label=case_label(row.case_id),
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration=_fmt_duration(row.started_at, row.completed_at),
        messages=messages,
    )


# ------------------------------------------------------------------ single PDF
def build_session_pdf(db: Session, session_id: str, tz: tzinfo) -> tuple[bytes, str]:
    """Return (pdf_bytes, filename) for exactly one session."""
    r = db.execute(
        select(
            InterviewSession.id, InterviewSession.student_id, Student.name, Student.student_number,
            InterviewSession.case_id, InterviewSession.status, InterviewSession.started_at,
            InterviewSession.completed_at,
        )
        .join(Student, Student.id == InterviewSession.student_id, isouter=True)
        .where(InterviewSession.id == session_id)
    ).first()
    if r is None:
        raise SessionNotFoundError(session_id)
    row = _SessionRow(
        id=r[0], student_id=r[1], student_name=r[2] or "", student_number=r[3] or "",
        case_id=r[4], status=r[5], started_at=r[6], completed_at=r[7],
    )
    buf = io.BytesIO()
    render_transcript_pdf(
        buf, _document(row, _turns_for(db, [row.id]).get(row.id, [])),
        tz=tz, generated_at=datetime.now(timezone.utc),
    )
    return buf.getvalue(), transcript_filename(row, tz)


# ------------------------------------------------------------------ ZIP
def export_zip_filename(filters: ExportFilters, tz: tzinfo) -> str:
    today = datetime.now(timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
    scope = safe_filename_part(case_label(filters.case_id)) if filters.case_id else "All"
    return f"PTAI_{scope}_Transcripts_{today}.zip"


def _index_csv(rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_INDEX_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _csv_cell(v) for k, v in row.items()})
    # BOM so Excel opens accented student names correctly.
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _sweep_stale_exports() -> None:
    tmp = tempfile.gettempdir()
    cutoff = datetime.now().timestamp() - _STALE_EXPORT_SECONDS
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    for entry in names:
        if entry.startswith(_TEMP_PREFIX) and entry.endswith(".zip"):
            full = os.path.join(tmp, entry)
            try:
                if os.path.getmtime(full) < cutoff:
                    os.unlink(full)
            except OSError:
                pass


def build_export_zip(db: Session, filters: ExportFilters, tz: tzinfo) -> str:
    """Write every matching transcript into a ZIP in the system temp dir and
    return its path. The caller owns the file and must delete it (the API does
    so after the response is sent). Raises NoTranscriptsError for an empty set
    instead of producing an empty archive."""
    sessions = _session_rows(db, filters, tz)
    if not sessions:
        raise NoTranscriptsError()

    _sweep_stale_exports()
    generated_at = datetime.now(timezone.utc)
    fd, path = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=".zip")
    os.close(fd)
    try:
        root_index: list[dict[str, str]] = []
        case_index: dict[str, list[dict[str, str]]] = defaultdict(list)
        used_names: dict[str, set[str]] = defaultdict(set)
        folders: dict[str, str] = {}

        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for i in range(0, len(sessions), _CHUNK):
                chunk = sessions[i:i + _CHUNK]
                ids = [s.id for s in chunk]
                turns = _turns_for(db, ids)
                levels = _latest_levels(db, ids)
                for row in chunk:
                    folder = folders.setdefault(row.case_id, safe_filename_part(case_label(row.case_id)))
                    name = transcript_filename(row, tz)
                    if name in used_names[folder]:
                        # Same student, case and minute: keep both sessions.
                        name = f"{name[:-4]}_{row.id[:8]}.pdf"
                    used_names[folder].add(name)

                    messages = turns.get(row.id, [])
                    buf = io.BytesIO()
                    render_transcript_pdf(buf, _document(row, messages), tz=tz, generated_at=generated_at)
                    zf.writestr(f"{folder}/{name}", buf.getvalue())

                    started = _aware(row.started_at)
                    completed = _aware(row.completed_at)
                    record = {
                        "Student Name": row.student_name,
                        "Student ID": row.student_id[:8],
                        "Student Number": row.student_number,
                        "Case": case_label(row.case_id),
                        "Session ID": row.id,
                        "Session Date": started.astimezone(tz).strftime("%Y-%m-%d") if started else "",
                        "Start Time": started.astimezone(tz).strftime("%Y-%m-%d %I:%M:%S %p %Z") if started else "",
                        "End Time": completed.astimezone(tz).strftime("%Y-%m-%d %I:%M:%S %p %Z") if completed else "",
                        "Duration": _fmt_duration(row.started_at, row.completed_at),
                        "Messages": str(len(messages)),
                        "Status": row.status.replace("_", " ").title(),
                        "Assessment Level": levels.get(row.id, ""),
                    }
                    root_index.append({**record, "Transcript Filename": f"{folder}/{name}"})
                    case_index[folder].append({**record, "Transcript Filename": name})

            zf.writestr("Transcript_Index.csv", _index_csv(root_index))
            for folder, rows in case_index.items():
                zf.writestr(f"{folder}/{folder}_Transcript_Index.csv", _index_csv(rows))
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise

    logger.info("transcript export built: %d sessions, %d case folders", len(sessions), len(folders))
    return path
