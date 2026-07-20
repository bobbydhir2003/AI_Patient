"""Streaming sentence-boundary detection for the low-latency patient pipeline.

A SentenceAccumulator receives incremental text deltas from the OpenAI stream
and emits complete sentences exactly once. It is deliberately conservative: a
terminator only counts as a boundary once the FOLLOWING character has arrived,
so decimals ("3.5"), abbreviations ("Dr."), initials ("J.") and ellipses that
continue a thought never split a sentence apart.

Short detected sentences (below `min_emit_chars`) are held and merged with the
next sentence so TTS never receives tiny choppy fragments like "Yes." - unless
the stream ends there, in which case flush() emits them as the complete answer.
"""
from __future__ import annotations

import re

# Common abbreviations that end with a period but do not end a sentence.
_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "st", "prof", "vs", "etc", "jr", "sr",
    "dept", "approx", "min", "max", "no", "e.g", "i.e", "ave", "rd",
    "ft", "lb", "lbs", "oz", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
}

_TERMINATORS = ".!?…"
_CLOSERS = "\"'”’)"

# Trailing word (letters, possibly dotted like "e.g") before a period.
_TRAILING_WORD_RE = re.compile(r"([A-Za-z]+(?:\.[A-Za-z]+)*)\s*$")

# Default minimum sentence length worth sending to TTS on its own.
MIN_EMIT_CHARS = 20


def _is_abbreviation_or_initial(prefix: str) -> bool:
    match = _TRAILING_WORD_RE.search(prefix)
    if not match:
        return False
    word = match.group(1)
    if len(word) == 1 and word.isupper():
        return True  # an initial like "J."
    lowered = word.lower()
    return lowered in _ABBREVIATIONS or lowered.rstrip(".") in _ABBREVIATIONS


class SentenceAccumulator:
    """Accumulates streamed text and yields complete sentences exactly once."""

    def __init__(self, min_emit_chars: int = MIN_EMIT_CHARS) -> None:
        self._buffer = ""
        self._pending = ""  # short sentence held for merging with the next one
        self._min_emit_chars = max(0, min_emit_chars)
        self.emitted_count = 0

    # ------------------------------------------------------------------
    def feed(self, delta: str) -> list[str]:
        """Add streamed text; return any newly completed sentences (in order)."""
        if not delta:
            return []
        self._buffer += delta
        out: list[str] = []
        while True:
            cut = self._find_boundary(self._buffer)
            if cut is None:
                break
            candidate = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut:].lstrip()
            if not candidate:
                continue
            merged = f"{self._pending} {candidate}".strip() if self._pending else candidate
            if len(merged) < self._min_emit_chars:
                # Too short to speak on its own: merge with the next sentence.
                self._pending = merged
                continue
            self._pending = ""
            self.emitted_count += 1
            out.append(merged)
        return out

    def flush(self) -> str | None:
        """Stream ended: return the final (possibly partial) sentence, if any."""
        rest = f"{self._pending} {self._buffer.strip()}".strip()
        self._buffer = ""
        self._pending = ""
        if not rest:
            return None
        self.emitted_count += 1
        return rest

    # ------------------------------------------------------------------
    def _find_boundary(self, text: str) -> int | None:
        """Return the exclusive end index of the first complete sentence, or
        None if no confirmed boundary exists yet (needs more input)."""
        n = len(text)
        i = 0
        while i < n:
            ch = text[i]

            if ch == "\n":
                if text[:i].strip():
                    return i + 1
                i += 1
                continue

            if ch not in _TERMINATORS:
                i += 1
                continue

            # Consume a run of terminators ("?!", "...", "?!.").
            j = i
            while j + 1 < n and text[j + 1] in _TERMINATORS:
                j += 1
            run = text[i : j + 1]
            is_ellipsis = run.count(".") >= 2 or "…" in run

            # Optional closing quote/paren directly after the terminator run.
            k = j + 1
            while k < n and text[k] in _CLOSERS:
                k += 1
            if k >= n:
                return None  # boundary unconfirmed until the next char arrives

            nxt = text[k]
            if not nxt.isspace():
                # e.g. a decimal (3.5), "e.g.", mid-word punctuation: not a boundary.
                i = j + 1
                continue

            if run == "." and _is_abbreviation_or_initial(text[:i]):
                i = j + 1
                continue

            # Require a clear NEW sentence start after the terminator: an
            # uppercase letter, digit, or opening quote. This keeps quoted
            # questions ('he asked "how bad is it?" and ...') and lowercase
            # continuations inside ONE sentence. Ellipses are stricter: only
            # an uppercase start ends the sentence ("maybe... 3 weeks" flows on).
            m = k
            while m < n and text[m].isspace():
                m += 1
            if m >= n:
                return None  # need the next sentence's first char to decide
            start_ch = text[m]
            if is_ellipsis:
                if not start_ch.isupper():
                    i = j + 1
                    continue
            elif not (start_ch.isupper() or start_ch.isdigit() or start_ch in "\"'“‘("):
                i = j + 1
                continue

            return k  # cut before the whitespace that follows the sentence

        return None
