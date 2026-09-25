"""Render one interview session transcript as a PDF (admin export).

Pure rendering: callers pass an already-loaded TranscriptDocument, so this
module never touches the database. Uses reportlab's Platypus flow layout, which
wraps text, splits long messages across pages and never truncates content.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import BinaryIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_ACCENT = colors.HexColor("#D9272E")  # UNMC red, matches the admin UI accent
_STUDENT = colors.HexColor("#1F4E79")
_MUTED = colors.HexColor("#6B7280")
_TEXT = colors.HexColor("#111827")
_RULE = colors.HexColor("#D1D5DB")


@dataclass
class TranscriptMessage:
    role: str  # student | patient | anything else is shown as a system note
    speaker_label: str
    content: str
    created_at: datetime | None


@dataclass
class TranscriptDocument:
    session_id: str
    student_name: str
    student_ref: str
    student_number: str
    case_label: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration: str
    messages: list[TranscriptMessage] = field(default_factory=list)


# Latin letters that have no Unicode decomposition to an ASCII base.
_TRANSLIT = str.maketrans({
    "Ł": "L", "ł": "l", "Ø": "O", "ø": "o", "Đ": "D", "đ": "d", "ß": "ss",
    "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe", "Þ": "Th", "þ": "th", "ı": "i",
})


def ascii_fold(value: str) -> str:
    """Best-effort ASCII form of a string (accents and ligatures removed)."""
    decomposed = unicodedata.normalize("NFKD", value.translate(_TRANSLIT))
    return decomposed.encode("ascii", "ignore").decode("ascii")


def _pdf_text(value: str) -> str:
    """Escape for Paragraph markup and keep only glyphs the built-in fonts carry.

    The standard PDF fonts use WinAnsi (cp1252), which covers accented Latin
    names, curly quotes, dashes and ellipses. Anything outside it is decomposed
    to its ASCII base where possible (e.g. "ł" -> "l"), otherwise shown as "?",
    so no character is silently dropped or rendered as a broken glyph."""
    out: list[str] = []
    for ch in value:
        if ch in "\n\t":
            out.append(ch)
            continue
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            out.append(ascii_fold(ch) or "?")
    text = escape("".join(out))
    return text.replace("\t", "    ").replace("\n", "<br/>")


def _local(dt: datetime | None, tz: tzinfo) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # stored as UTC; SQLite returns naive values
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _fmt_time(dt: datetime, *, seconds: bool = True, zone: bool = False) -> str:
    fmt = "%I:%M:%S %p" if seconds else "%I:%M %p"
    text = dt.strftime(fmt).lstrip("0")
    return f"{text} {dt.strftime('%Z')}".strip() if zone else text


def _fmt_long_date(dt: datetime) -> str:
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def speaker_name(msg: TranscriptMessage, case_label: str) -> str:
    """Mirror the admin TranscriptView: student turns are "Student"; patient
    turns use the stored speaker label (multi-participant cases) and otherwise
    the case's patient name."""
    if msg.role == "student":
        return "STUDENT"
    if msg.role == "patient":
        label = (msg.speaker_label or "").strip()
        if not label or label.lower() == "patient":
            label = case_label or "Patient"
        return label.upper()
    return "SYSTEM / NOTE"


class _NumberedCanvas(rl_canvas.Canvas):
    """Two-pass canvas so every page footer can say "Page X of Y"."""

    footer_left = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages: list[dict] = []

    def showPage(self):  # noqa: N802 - reportlab API
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total: int) -> None:
        width, _ = self._pagesize
        self.saveState()
        self.setStrokeColor(_RULE)
        self.setLineWidth(0.5)
        self.line(0.75 * inch, 0.62 * inch, width - 0.75 * inch, 0.62 * inch)
        self.setFont("Helvetica", 7.5)
        self.setFillColor(_MUTED)
        self.drawString(0.75 * inch, 0.45 * inch, self.footer_left)
        self.drawRightString(width - 0.75 * inch, 0.45 * inch, f"Page {self._pageNumber} of {total}")
        self.restoreState()


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "brand": ParagraphStyle("brand", fontName="Helvetica-Bold", fontSize=15, leading=18, textColor=_ACCENT),
        "product": ParagraphStyle("product", fontName="Helvetica", fontSize=10.5, leading=13, textColor=_TEXT),
        "title": ParagraphStyle(
            "title", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=_TEXT,
            spaceBefore=14, spaceAfter=8,
        ),
        "section": ParagraphStyle(
            "section", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=_TEXT,
            spaceBefore=4, spaceAfter=10,
        ),
        "meta_label": ParagraphStyle("meta_label", fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=_MUTED),
        "meta_value": ParagraphStyle("meta_value", fontName="Helvetica", fontSize=9.5, leading=12, textColor=_TEXT),
        "time": ParagraphStyle(
            "time", fontName="Helvetica", fontSize=8, leading=10, textColor=_MUTED, keepWithNext=1,
        ),
        "speaker_student": ParagraphStyle(
            "speaker_student", fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=_STUDENT,
            keepWithNext=1,
        ),
        "speaker_patient": ParagraphStyle(
            "speaker_patient", fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=_ACCENT,
            keepWithNext=1,
        ),
        "speaker_other": ParagraphStyle(
            "speaker_other", fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=_MUTED,
            keepWithNext=1,
        ),
        "body": ParagraphStyle(
            "body", fontName="Helvetica", fontSize=10, leading=14, textColor=_TEXT, alignment=TA_LEFT,
            leftIndent=10, spaceAfter=11, splitLongWords=1,
        ),
        "empty": ParagraphStyle("empty", fontName="Helvetica-Oblique", fontSize=10, leading=14, textColor=_MUTED),
    }


def render_transcript_pdf(
    out: BinaryIO, doc: TranscriptDocument, *, tz: tzinfo, generated_at: datetime
) -> None:
    """Write a complete, paginated transcript PDF for one session to `out`."""
    st = _styles()
    started = _local(doc.started_at, tz)
    completed = _local(doc.completed_at, tz)

    meta: list[tuple[str, str]] = [
        ("Student", doc.student_name or "Unknown student"),
        ("Student ID", doc.student_ref),
    ]
    if doc.student_number:
        meta.append(("Student Number", doc.student_number))
    meta += [("Case", doc.case_label), ("Session ID", doc.session_id)]
    if started:
        meta.append(("Session Date", _fmt_long_date(started)))
        meta.append(("Start Time", _fmt_time(started, seconds=False, zone=True)))
    if completed:
        meta.append(("End Time", _fmt_time(completed, seconds=False, zone=True)))
    if doc.duration:
        meta.append(("Duration", doc.duration))
    meta += [("Status", doc.status.replace("_", " ").title()), ("Messages", str(len(doc.messages)))]

    meta_table = Table(
        [[Paragraph(_pdf_text(k), st["meta_label"]), Paragraph(_pdf_text(v), st["meta_value"])] for k, v in meta],
        colWidths=[1.35 * inch, None],
        hAlign="LEFT",
    )
    meta_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))

    story: list = [
        Paragraph("UNMC | iEXCEL", st["brand"]),
        Paragraph("PT AI Patient Simulator", st["product"]),
        Paragraph("SESSION TRANSCRIPT", st["title"]),
        meta_table,
        Spacer(1, 10),
        HRFlowable(width="100%", thickness=0.8, color=_RULE, spaceBefore=2, spaceAfter=12),
        Paragraph("CONVERSATION TRANSCRIPT", st["section"]),
    ]

    if not doc.messages:
        story.append(Paragraph("This session has no saved conversation.", st["empty"]))
    start_date = started.date() if started else None
    for msg in doc.messages:
        when = _local(msg.created_at, tz)
        if when is not None:
            # Show the date too when a turn falls on a different calendar day.
            stamp = _fmt_time(when)
            if start_date and when.date() != start_date:
                stamp = f"{when.strftime('%b')} {when.day}, {when.year} {stamp}"
            story.append(Paragraph(_pdf_text(stamp), st["time"]))
        style = {"student": "speaker_student", "patient": "speaker_patient"}.get(msg.role, "speaker_other")
        story.append(Paragraph(_pdf_text(speaker_name(msg, doc.case_label)), st[style]))
        story.append(Paragraph(_pdf_text(msg.content or ""), st["body"]))

    story.append(HRFlowable(width="100%", thickness=0.8, color=_RULE, spaceBefore=6, spaceAfter=0))

    gen_local = _local(generated_at, tz) or generated_at
    footer = (
        f"PT AI Patient Simulator  |  Generated: {_fmt_long_date(gen_local)} "
        f"{_fmt_time(gen_local, seconds=False, zone=True)}"
    )
    canvas_cls = type("_TranscriptCanvas", (_NumberedCanvas,), {"footer_left": footer})

    pdf = SimpleDocTemplate(
        out,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.85 * inch,
        title=f"Session Transcript - {doc.case_label} - {doc.student_name}",
        author="PT AI Patient Simulator",
        subject="Session transcript",
        creator="PT AI Patient Simulator",
    )
    pdf.build(story, canvasmaker=canvas_cls)
