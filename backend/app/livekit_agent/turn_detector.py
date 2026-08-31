"""Phase 3 (EXPERIMENTAL, OBSERVATIONAL ONLY): a small, replaceable semantic
turn-detector abstraction, plus a vendored Smart Turn v3.2 implementation.

This module has NO dependency on livekit/FastAPI/the DB - it operates purely
on numpy audio + plain strings, so it can be swapped or unit-tested in
isolation (see SemanticTurnDetector). worker.py's _CandidateTurnCoordinator
is the ONLY caller; a HOLD/END decision produced here never reaches
PocAgentSession's turn-driving state (no reference to it exists in this
module at all) - see worker.py's own Phase 3 notes for how the decision is
merely logged.

------------------------------------------------------------------------
Why this vendors the Smart Turn v3.2 MODEL but does NOT depend on the
pipecat-ai PACKAGE (a decision confirmed with the project owner before
implementing - see the Phase 3 conversation):

Pipecat Smart Turn (https://huggingface.co/pipecat-ai/smart-turn-v3,
BSD-2-Clause) is a Whisper-Tiny-encoder + linear-classifier ONNX model
(8M params) that classifies whether a trailing ~8s window of 16kHz mono
audio represents a COMPLETE or INCOMPLETE conversational turn, returning a
genuine sigmoid probability - not a text/transcript-based model. The
`pipecat-ai` PyPI package (verified at 0.0.108) bundles this exact ONNX
file at pipecat/audio/turn/smart_turn/data/smart-turn-v3.2-cpu.onnx and
exposes it through pipecat.audio.turn.smart_turn.local_smart_turn_v3.
LocalSmartTurnAnalyzerV3 (a genuinely standalone class - append_audio()/
analyze_end_of_turn(), no Pipecat pipeline/frame adoption required).

However, merely IMPORTING anything under the `pipecat` package executes
pipecat/__init__.py, which on Python < 3.12 (this project's runtime is
3.10) unconditionally does `asyncio.wait_for = wait_for2.wait_for` - a
GLOBAL, process-wide monkeypatch with different task-cancellation-race
semantics than the stdlib (confirmed by reading wait_for2's own source: it
is a deliberate reimplementation, not a transparent shim). Grepping the
actually-installed livekit-agents==1.3.5 confirms `asyncio.wait_for` is
used directly in its OWN job-process supervision code
(ipc/supervised_proc.py, ipc/job_thread_executor.py - the code underpinning
this worker's JobExecutorType.PROCESS isolation model) and its job-
assignment-timeout logic (agents/worker.py), plus livekit.rtc's own
internals. Installing pipecat-ai would therefore silently change timeout/
cancellation behavior for load-bearing LiveKit internals in this exact
worker process, for the lifetime of every job. pipecat-ai also requires
openai>=1.74 (unconditionally, not an extra) and an unpinned
transformers<6 range that resolves to a brand-new major version (5.16.1
observed) - both inconsistent with this project's deliberate pinning
discipline elsewhere (see requirements.txt's livekit-agents/opentelemetry
comments).

None of that risk lives in the MODEL itself. SmartTurnDetector below
therefore re-implements ONLY the ~40-line, framework-agnostic inference
recipe (documented and empirically verified against the real bundled ONNX
file: WhisperFeatureExtractor(chunk_length=8) -> pad/truncate to exactly
8s*16kHz samples, keeping the END when truncating -> onnxruntime session
run with input key "input_features" -> the output tensor, despite being
named "logits", is already a bounded-in-[0,1] sigmoid probability - taken
directly with NO extra activation applied, matching pipecat's own
(unstated) behavior, confirmed by running real inference against
synthetic input and observing every output in [0, 1]). onnxruntime and
transformers (pinned exact versions - see requirements.txt) were verified
to do no comparable monkeypatching and to require no network access or
deep-learning framework (torch/tensorflow) at runtime for this feature
extractor.
------------------------------------------------------------------------
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


class TurnDecision(str, Enum):
    HOLD = "hold"
    END = "end"


@dataclass(frozen=True)
class TurnContext:
    """Everything a detector needs to evaluate ONE candidate-turn boundary.
    Assembled fresh by the caller (worker.py's _CandidateTurnCoordinator)
    for each evaluation - detectors are stateless with respect to the
    conversation and never reach back into any session/turn state.

    `transcript` is the EXPERIMENTAL accumulated candidate-turn text (never
    the real transcript, never derived from/written into conversation
    history - see worker.py). A text-only detector implementation (e.g. a
    future LiveKit Turn Detector swap-in) would simply ignore `audio`."""

    audio: "np.ndarray"  # float32 mono PCM in [-1, 1] at audio_sample_rate
    audio_sample_rate: int
    transcript: str
    segment_count: int
    pause_ms: float | None
    speech_duration_ms: float | None
    total_turn_duration_ms: float | None


@dataclass(frozen=True)
class TurnDetectorResult:
    decision: TurnDecision
    # Only set when the underlying model genuinely provides a probability -
    # never fabricated for a detector that doesn't expose one (Step 12).
    probability: float | None
    inference_ms: float
    detector: str


class BargeInDecision(str, Enum):
    """Phase 5A (EXPERIMENTAL): classifies transcript heard WHILE the
    patient is speaking - a separate, smaller-scoped question than
    TurnDecision (which asks "has the STUDENT finished their turn"). This
    asks "should the PATIENT keep talking or yield the floor". Deliberately
    NOT a bigger state machine - three values are enough (worker.py's
    _CandidateTurnCoordinator is the only caller)."""

    ACKNOWLEDGEMENT = "acknowledgement"
    TRUE_BARGE_IN = "true_barge_in"
    UNDECIDED = "undecided"


# Backchannel/filler words that must NEVER interrupt the patient (Step 8) -
# checked both as a whole-phrase match (multi-word fillers like "mm hmm")
# and per-word (so "okay right" or "yeah yeah" still classifies as pure
# acknowledgement even though neither phrase is listed verbatim).
_ACK_PHRASES = frozenset({
    "yeah", "yep", "yes", "yup", "mm hmm", "mhm", "mmhmm", "uh huh", "uhhuh",
    "okay", "ok", "right", "got it", "gotcha", "sure", "i see", "alright",
})
_ACK_WORDS = frozenset({
    "yeah", "yep", "yes", "yup", "mhm", "mmhmm", "uhhuh", "okay", "ok",
    "right", "gotcha", "sure", "alright",
})

# Deliberate-interruption signals (Step 5) - checked as word-exact/phrase
# matches (never raw substring, so "wait" never false-matches inside
# "waiting" - see _contains_phrase) against the FULL accumulated
# during-patient-speech transcript. A single match is enough even for a
# one-word utterance ("Wait." / "Stop.") - these are unambiguous by
# themselves, unlike a generic long clause (see _MIN_SUBSTANTIVE_WORDS).
_BARGE_IN_TRIGGER_PHRASES = frozenset({
    "wait", "stop", "hold on", "hang on", "actually", "excuse me",
    "sorry", "before that", "one second", "one sec", "no no",
})
_QUESTION_TRIGGER_WORDS = frozenset({
    "what", "why", "how", "where", "when", "who", "which", "can", "could", "would",
})
# A clause this long that is NEITHER pure acknowledgement NOR a recognized
# trigger is still confidently substantive (not noise) - conservative
# enough to avoid a 1-2 word noisy STT fragment ever counting (Step 6).
_MIN_SUBSTANTIVE_WORDS = 4


def normalize_barge_in_text(text: str) -> str:
    """Lowercase, hyphens -> spaces (so "mm-hmm"/"uh-huh" match the
    space-joined phrase list above), strip all other punctuation, collapse
    whitespace. Pure string manipulation - no model, no I/O."""
    import re

    lowered = text.lower().replace("-", " ")
    cleaned = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return " ".join(cleaned.split())


def _contains_phrase(words: list[str], phrase: str) -> bool:
    """True iff `phrase` (one or more words) appears as a CONTIGUOUS,
    word-exact subsequence of `words` - never a raw substring-of-string
    match, which would false-positive "wait" inside "waiting"."""
    phrase_words = phrase.split()
    if len(phrase_words) == 1:
        return phrase_words[0] in words
    n = len(phrase_words)
    return any(words[i : i + n] == phrase_words for i in range(len(words) - n + 1))


def classify_barge_in(text: str) -> BargeInDecision:
    """Phase 5A (Step 5): deterministic, conservative classifier - NOT
    another model call. Order matters: a directive/question trigger wins
    even inside an otherwise-ack-looking utterance ("yeah, but what do you
    mean?" -> TRUE_BARGE_IN, not ACKNOWLEDGEMENT, because "what" is
    present) - see worker.py's _CandidateTurnCoordinator, which re-runs
    this on the FULL accumulated during-patient-speech buffer on every new
    STT final, so a provisional "yeah" naturally upgrades to
    TRUE_BARGE_IN once real continuation arrives (Step 9), without this
    function itself needing to track history."""
    normalized = normalize_barge_in_text(text)
    if not normalized:
        return BargeInDecision.UNDECIDED
    words = normalized.split()

    if any(_contains_phrase(words, trigger) for trigger in _BARGE_IN_TRIGGER_PHRASES):
        return BargeInDecision.TRUE_BARGE_IN
    if any(w in _QUESTION_TRIGGER_WORDS for w in words):
        return BargeInDecision.TRUE_BARGE_IN

    if normalized in _ACK_PHRASES or all(w in _ACK_WORDS for w in words):
        return BargeInDecision.ACKNOWLEDGEMENT

    if len(words) >= _MIN_SUBSTANTIVE_WORDS:
        return BargeInDecision.TRUE_BARGE_IN

    # Short, unrecognized fragment (noisy STT partial, a lone filler sound
    # like "uh"/"hm", or the start of a longer utterance) - wait for more
    # rather than guessing either way (Step 6/9).
    return BargeInDecision.UNDECIDED


class SemanticTurnDetector(ABC):
    """Minimal, replaceable interface (Step 3). Swapping Smart Turn for
    LiveKit's text-based Turn Detector, another model, or a test stub means
    writing one new subclass of this - nothing in worker.py's audio
    pipeline/wiring changes."""

    @abstractmethod
    async def evaluate(self, context: TurnContext) -> TurnDetectorResult: ...

    async def aclose(self) -> None:
        """Optional: release model/session resources. Default no-op."""
        return None


class SmartTurnDetector(SemanticTurnDetector):
    """Vendored Smart Turn v3.2 (see module docstring for why this avoids
    the pipecat-ai package). CPU-only via onnxruntime; no network access at
    inference time; safe to construct/run inside this worker process."""

    MODEL_SAMPLE_RATE = 16000
    WINDOW_SECONDS = 8
    # Verified against the real bundled ONNX model: for a plausible speech
    # window the sigmoid output clusters near 1.0 for "no more to say" and
    # would be expected to drop for a clearly-unfinished utterance (real
    # PT-interview speech validation is the project owner's manual Phase 3
    # test, not something this smoke-level check can substitute for).
    COMPLETE_THRESHOLD = 0.5
    DETECTOR_ID = "smart-turn-v3.2-cpu"

    def __init__(self, *, model_path: str, cpu_count: int = 1) -> None:
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor

        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = max(1, cpu_count)
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(model_path, sess_options=so)
        self._feature_extractor = WhisperFeatureExtractor(chunk_length=self.WINDOW_SECONDS)

    async def evaluate(self, context: TurnContext) -> TurnDetectorResult:
        import asyncio

        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        probability = await loop.run_in_executor(
            None, self._predict, context.audio, context.audio_sample_rate,
        )
        inference_ms = (time.monotonic() - t0) * 1000
        decision = TurnDecision.END if probability > self.COMPLETE_THRESHOLD else TurnDecision.HOLD
        return TurnDetectorResult(
            decision=decision, probability=probability, inference_ms=inference_ms, detector=self.DETECTOR_ID,
        )

    def _predict(self, audio: "np.ndarray", sample_rate: int) -> float:
        """Faithfully reproduces LocalSmartTurnAnalyzerV3._predict_endpoint's
        preprocessing recipe (see module docstring) - pad/truncate to
        exactly WINDOW_SECONDS*MODEL_SAMPLE_RATE samples (keeping the END
        when truncating, zero-padding at the START when shorter), extract
        Whisper log-mel features, run the ONNX session, return the raw
        output value as the probability (empirically confirmed already
        bounded in [0, 1] - see module docstring)."""
        import numpy as np

        if sample_rate != self.MODEL_SAMPLE_RATE:
            # worker.py already requests _VAD_STT_SAMPLE_RATE (16kHz) frames
            # directly from LiveKit (see _ingest_student_audio) specifically
            # so every consumer - VAD, STT, and this detector - receives
            # audio already at the rate it needs, with no redundant
            # per-consumer resampling. This is a defensive invariant check,
            # not a supported code path - see Step 10 (fail open, log, skip
            # this boundary) at the caller.
            raise ValueError(
                f"SmartTurnDetector requires {self.MODEL_SAMPLE_RATE}Hz audio, got {sample_rate}Hz"
            )

        max_samples = self.WINDOW_SECONDS * self.MODEL_SAMPLE_RATE
        if len(audio) > max_samples:
            audio = audio[-max_samples:]
        elif len(audio) < max_samples:
            audio = np.pad(audio, (max_samples - len(audio), 0), mode="constant", constant_values=0.0)

        inputs = self._feature_extractor(
            audio,
            sampling_rate=self.MODEL_SAMPLE_RATE,
            return_tensors="np",
            padding="max_length",
            max_length=max_samples,
            truncation=True,
            do_normalize=True,
        )
        input_features = inputs.input_features.squeeze(0).astype(np.float32)[np.newaxis, ...]
        outputs = self._session.run(None, {"input_features": input_features})
        return float(outputs[0][0].item())
