"""Formats the locked transcript for the AI stages and maps labels to turns."""
from dataclasses import dataclass

from app.models import ConversationTurn


@dataclass
class PreparedTranscript:
    text: str
    label_to_turn: dict[str, ConversationTurn]
    student_turn_count: int


def prepare_transcript(turns: list[ConversationTurn]) -> PreparedTranscript:
    lines: list[str] = []
    label_to_turn: dict[str, ConversationTurn] = {}
    student_count = 0
    for turn in turns:
        label = f"turn_{turn.turn_index:02d}"
        label_to_turn[label] = turn
        if turn.role == "student":
            student_count += 1
            speaker = "STUDENT"
        else:
            # Multi-participant cases surface WHO answered (e.g. "PATIENT
            # (Camden's Mother)") so the rubric can assess pediatric,
            # family-centered communication with both participants.
            sid = getattr(turn, "speaker_id", "patient") or "patient"
            label_txt = getattr(turn, "speaker_label", "") or ""
            speaker = f"PATIENT ({label_txt})" if sid not in ("", "patient") and label_txt else "PATIENT"
        lines.append(f"[{label}] {speaker}: {turn.content}")
    return PreparedTranscript(
        text="\n".join(lines), label_to_turn=label_to_turn, student_turn_count=student_count
    )
