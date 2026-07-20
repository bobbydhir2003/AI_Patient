"""Static scan: production frontend code must contain no mock-conversation paths.

Fails if application code (src/ outside src/test/) references mock responses,
canned patient replies, or silently-swallowed API errors.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src"

FORBIDDEN_TOKENS = (
    "mockResponses",
    "getMockResponse",
    "demoResponses",
    "sampleMessages",
    "fakeMessages",
    "fallbackResponses",
    "simulatePatientResponse",
    "randomPatientReply",
    "mockAssessment",
    "buildMockAssessment",
    "mockCases",
)

# Any import that reaches into the test fixtures from production code.
FIXTURE_IMPORT = re.compile(r"""from\s+["'].*test/fixtures""")


def _production_files():
    for path in SRC.rglob("*"):
        if path.suffix not in (".ts", ".tsx"):
            continue
        if "test" in path.relative_to(SRC).parts[:1]:
            continue  # src/test/** is allowed to contain fixtures
        yield path


def test_src_exists():
    assert SRC.is_dir(), f"frontend src not found at {SRC}"


def test_no_mock_tokens_in_production_code():
    offenders = []
    for path in _production_files():
        content = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            if token in content:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, f"Mock-conversation references found in production code: {offenders}"


def test_no_fixture_imports_in_production_code():
    offenders = [
        path.name
        for path in _production_files()
        if FIXTURE_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"Production code imports test fixtures: {offenders}"


def test_no_silent_empty_catch_around_patient_flow():
    """The interview page must not swallow API errors silently."""
    interview = SRC / "pages" / "InterviewPage.tsx"
    content = interview.read_text(encoding="utf-8")
    assert ".catch(() => {})" not in content
    assert "getMockResponse" not in content
    # every catch path must at least log the error
    assert "console.error" in content
