"""PAVING digital-chart data: correct per-case values, order, range, isolation.

Values are the authoritative totals read from each patient's completed PAVING
worksheet (page 2 of 730Case{5-8}_PW_*.pdf). No value is invented or defaulted.
"""
from app.core.constants import PAVING_CATEGORIES
from app.services.case_service import get_case

ORDER = [c["key"] for c in PAVING_CATEGORIES]

EXPECTED = {
    "camden": {"physical_activity": 22, "attitude": 21, "variety": 17, "investigations": 16,
               "nutrition": 14, "goals": 11, "stress_management": 11, "time_outs": 23,
               "energy": 20, "purpose": 20, "sleep": 25, "social_connections": 25},
    "carly": {"physical_activity": 21, "attitude": 19, "variety": 18, "investigations": 16,
              "nutrition": 16, "goals": 9, "stress_management": 15, "time_outs": 15,
              "energy": 16, "purpose": 19, "sleep": 13, "social_connections": 20},
    "sofia": {"physical_activity": 24, "attitude": 23, "variety": 12, "investigations": 9,
              "nutrition": 17, "goals": 5, "stress_management": 10, "time_outs": 17,
              "energy": 21, "purpose": 21, "sleep": 25, "social_connections": 25},
    "jayden": {"physical_activity": 21, "attitude": 22, "variety": 15, "investigations": 11,
               "nutrition": 19, "goals": 17, "stress_management": 16, "time_outs": 18,
               "energy": 20, "purpose": 22, "sleep": 20, "social_connections": 21},
}


def _profile(case_id):
    return get_case(case_id).model_dump(by_alias=True)["pavingProfile"]


def test_each_case_uses_its_own_worksheet_values():
    for case_id, expected in EXPECTED.items():
        p = _profile(case_id)
        got = {c["key"]: c["value"] for c in p["categories"]}
        assert got == expected, f"{case_id} paving values differ from source worksheet"


def test_category_order_matches_wheel_order():
    for case_id in EXPECTED:
        keys = [c["key"] for c in _profile(case_id)["categories"]]
        assert keys == ORDER


def test_values_within_official_range_and_no_review_needed():
    for case_id in EXPECTED:
        p = _profile(case_id)
        assert p["needsReview"] == []  # all totals were legible
        for c in p["categories"]:
            assert c["value"] is not None
            assert 0 <= c["value"] <= 25
            assert c["maxValue"] == 25


def test_no_case_shares_another_cases_profile():
    vecs = {cid: tuple(c["value"] for c in _profile(cid)["categories"]) for cid in EXPECTED}
    assert len(set(vecs.values())) == 4  # all four are distinct


def test_label_colors_are_shared_and_not_single_color():
    p = _profile("camden")
    colors = {c["labelColor"] for c in p["categories"]}
    assert len(colors) > 1  # category labels are not all one color
    # colors come from the shared system, identical across patients
    assert (
        [c["labelColor"] for c in _profile("camden")["categories"]]
        == [c["labelColor"] for c in _profile("jayden")["categories"]]
    )


def test_source_file_recorded_per_case():
    assert _profile("camden")["sourceFile"] == "730Case5_PW_Camden.pdf"
    assert _profile("sofia")["sourceFile"] == "730Case7_PW_Sofia.pdf"
    assert _profile("camden")["sourcePage"] == 2


def test_referral_cases_have_no_paving_profile():
    assert get_case("referral_case_01").model_dump(by_alias=True)["pavingProfile"] is None
