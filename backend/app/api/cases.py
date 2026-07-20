from fastapi import APIRouter

from app.schemas.case_schema import CaseCatalogOut, CaseSummary
from app.services import case_service

router = APIRouter(prefix="/cases", tags=["cases"])


@router.get("", response_model=CaseCatalogOut)
def case_catalog() -> CaseCatalogOut:
    """Student-safe case catalog grouped into sections by case category."""
    return case_service.get_case_catalog()


@router.get("/{case_id}", response_model=CaseSummary)
def get_case(case_id: str) -> CaseSummary:
    return case_service.get_case(case_id)
