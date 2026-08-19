"""Per-assessment OpenAI call budget + usage accounting (single choke point).

Every LOGICAL assessment generation call goes through an AssessmentCallBudget:

* it counts logical provider calls and refuses to exceed a hard ceiling
  (settings.assessment_max_openai_calls) - a fail-closed guard so a bug or a bad
  provider response can never recreate the old 6-11 calls-per-assessment runaway;
* it records EVERY billed call into the persistent AI usage accounting
  (ai_usage_events) with a `purpose` stage, so the admin dashboard reflects real
  assessment spend instead of silently undercounting it;
* it emits one structured log line per call and accumulates token/cost totals for
  a single `assessment_completed` cost line.

"Logical call" is one call to generate_structured. Provider-side HTTP retries and
truncation retries happen INSIDE the OpenAI client and do not consume the logical
budget (they are still visible via provider_retry telemetry + truncation logs);
this keeps the budget meaningful without letting one logical call silently expand
into many invisible billed requests.
"""
from __future__ import annotations

from app.core import pricing
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services import usage_recorder

logger = get_logger(__name__)


class AssessmentCallBudgetExceeded(Exception):
    """Raised when a logical assessment call would exceed the hard ceiling."""

    def __init__(self, *, max_calls: int, stage: str) -> None:
        self.max_calls = max_calls
        self.stage = stage
        super().__init__(
            f"assessment OpenAI call budget of {max_calls} exceeded at stage '{stage}'"
        )


class AssessmentCallBudget:
    def __init__(
        self,
        client,
        db,
        *,
        session_id: str | None,
        student_id: str | None,
        case_id: str | None,
        model: str,
        max_calls: int,
        assessment_id: str | None = None,
    ) -> None:
        self.client = client
        self.db = db
        self.session_id = session_id
        self.student_id = student_id
        self.case_id = case_id
        self.model = model
        self.max_calls = max(1, int(max_calls))
        self.assessment_id = assessment_id
        # P1: assessment generation is much heavier than patient chat, so every
        # assessment provider call (standard combined/verify/correction AND
        # referral extract/evaluate/review/re-eval - all funnel through this
        # single choke point) uses the assessment-specific timeout. Patient chat
        # never constructs a budget, so its timeout is untouched.
        self.timeout_seconds = get_settings().openai_assessment_timeout_seconds
        # Running totals across the assessment (for the completion cost line).
        self.calls = 0
        self.input_tokens = 0
        self.cached_input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost_usd = 0.0

    @property
    def remaining(self) -> int:
        return self.max_calls - self.calls

    def can_call(self) -> bool:
        return self.calls < self.max_calls

    def generate_structured(
        self,
        messages: list[dict],
        schema: dict,
        schema_name: str,
        *,
        stage: str = "assessment_generate",
        max_output_tokens: int | None = None,
        allow_truncation_retry: bool = False,
    ) -> dict:
        if self.calls >= self.max_calls:
            logger.warning(
                "assessment_call_budget_exceeded session_id=%s assessment_id=%s calls=%d "
                "max=%d stage=%s",
                self.session_id, self.assessment_id, self.calls, self.max_calls, stage,
            )
            raise AssessmentCallBudgetExceeded(max_calls=self.max_calls, stage=stage)

        self.calls += 1
        logical_call = self.calls
        logger.info(
            "assessment_openai_call session_id=%s assessment_id=%s stage=%s logical_call=%d",
            self.session_id, self.assessment_id, stage, logical_call,
        )
        usage_out: dict = {}
        data = self.client.generate_structured(
            messages,
            schema,
            schema_name,
            max_output_tokens=max_output_tokens,
            allow_truncation_retry=allow_truncation_retry,
            usage_out=usage_out,
            timeout_seconds=self.timeout_seconds,
        )
        self._account(stage, usage_out)
        return data

    def _account(self, stage: str, usage_out: dict) -> None:
        """Accumulate running totals and persist ONE usage event for this call.

        Best-effort and never raises: usage_recorder swallows its own errors, and
        an empty usage payload (e.g. a mock/interrupted response) is skipped there
        without faking numbers.
        """
        in_tok = int(usage_out.get("input_tokens") or 0)
        out_tok = int(usage_out.get("output_tokens") or 0)
        cached = int(usage_out.get("cached_input_tokens") or 0)
        model = str(usage_out.get("model") or self.model or "")
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.cached_input_tokens += cached
        if in_tok > 0 or out_tok > 0:
            cost, _rates = pricing.estimate_openai_cost(
                input_tokens=max(0, in_tok - cached),
                output_tokens=out_tok,
                cached_input_tokens=cached,
                model=model,
            )
            self.estimated_cost_usd += cost
        usage_recorder.record_openai_usage(
            self.db,
            self.session_id,
            self.student_id,
            self.case_id,
            {**usage_out, "model": model},
            purpose=stage,
        )
