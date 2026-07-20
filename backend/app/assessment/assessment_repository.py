import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssessmentDomainResult, AssessmentEvidence, AssessmentRun


class AssessmentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_run(self, **kwargs) -> AssessmentRun:
        run = AssessmentRun(**kwargs)
        self.db.add(run)
        self.db.flush()
        return run

    def get(self, assessment_id: str) -> AssessmentRun | None:
        return self.db.get(AssessmentRun, assessment_id)

    def latest_for_session(self, session_id: str) -> AssessmentRun | None:
        stmt = (
            select(AssessmentRun)
            .where(AssessmentRun.session_id == session_id)
            .order_by(AssessmentRun.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def add_domain_result(
        self, run: AssessmentRun, *, rubric_domain: str, performance_level: str,
        summary: str, narrative: str, strengths: list[str], areas_for_growth: list[str],
    ) -> AssessmentDomainResult:
        result = AssessmentDomainResult(
            assessment_run_id=run.id,
            rubric_domain=rubric_domain,
            performance_level=performance_level,
            summary=summary,
            narrative=narrative,
            strengths=json.dumps(strengths),
            areas_for_growth=json.dumps(areas_for_growth),
        )
        self.db.add(result)
        self.db.flush()
        return result

    def add_evidence(self, domain_result: AssessmentDomainResult, **kwargs) -> AssessmentEvidence:
        evidence = AssessmentEvidence(domain_result_id=domain_result.id, **kwargs)
        self.db.add(evidence)
        self.db.flush()
        return evidence
