from app.core.constants import CASE_SECTIONS
from app.patient_engine.case_loader import load_all_cases, load_case
from app.schemas.case_schema import CaseCatalogOut, CaseSectionOut, CaseSummary


def list_cases() -> list[CaseSummary]:
    """Flat student-safe list (used internally and by /api/cases/{id})."""
    return [CaseSummary.from_definition(c) for c in load_all_cases().values()]


def get_case(case_id: str) -> CaseSummary:
    return CaseSummary.from_definition(load_case(case_id))


def get_case_catalog() -> CaseCatalogOut:
    """Student-safe catalog grouped by case_category (NOT by case id)."""
    all_cases = list_cases()
    sections = []
    for section in CASE_SECTIONS:
        cases = [c for c in all_cases if c.case_category == section["id"]]
        if cases:
            sections.append(
                CaseSectionOut(
                    id=section["id"],
                    title=section["title"],
                    description=section["description"],
                    cases=cases,
                )
            )
    return CaseCatalogOut(sections=sections)
