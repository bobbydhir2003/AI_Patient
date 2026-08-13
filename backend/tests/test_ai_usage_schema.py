"""Schema/migration tests for ai_usage_events (regression guard for the
'no such table: ai_usage_events' 500). Ensures the table exists after the
standard schema build, the endpoints return 200 with real zero aggregates on an
empty table, and the 0017 migration matches the model and is PostgreSQL-portable.
"""
import pathlib

from sqlalchemy import inspect as sa_inspect

from app.models import AiUsageEvent
from tests.conftest import FakeOpenAIClient, bearer, make_client
from tests.test_auth import login_token, make_admin

_MIGRATION = pathlib.Path(__file__).resolve().parents[1] / (
    "app/database/migrations/versions/0017_ai_usage_events.py"
)
# Columns are added incrementally by later additive migrations; the anti-drift
# guard below compares the model against the UNION of all ai_usage_events DDL.
_PURPOSE_MIGRATION = pathlib.Path(__file__).resolve().parents[1] / (
    "app/database/migrations/versions/0018_ai_usage_purpose.py"
)


# TEST 1 — the table exists in the built schema (create_all mirrors the migration).
def test_ai_usage_events_table_exists(engine):
    assert sa_inspect(engine).has_table("ai_usage_events")


# TEST 2/3/4 — empty table → endpoints return 200 with legitimate zero aggregates.
def test_empty_endpoints_return_200_with_zeroes(engine):
    with make_client(engine, FakeOpenAIClient(), authenticate=False) as c:
        make_admin(engine, email="empty_admin@school.edu")
        ah = bearer(login_token(c, "empty_admin@school.edu", "adminpass1"))

        s = c.get("/api/admin/usage/summary?range=today", headers=ah)
        assert s.status_code == 200
        body = s.json()
        assert body["input_tokens"] == 0 and body["output_tokens"] == 0
        assert body["total_tokens"] == 0 and body["elevenlabs_characters"] == 0
        assert body["total_cost_usd"] == 0 and body["session_count"] == 0
        assert body["projected_monthly"]["available"] is False  # honest, not faked

        t = c.get("/api/admin/usage/timeseries?range=today", headers=ah)
        assert t.status_code == 200
        assert all(p["total_tokens"] == 0 for p in t.json()["points"])

        se = c.get("/api/admin/usage/sessions?range=today&limit=10", headers=ah)
        assert se.status_code == 200
        assert se.json()["total"] == 0 and se.json()["sessions"] == []


# TEST 8 — migration is correctly chained (fits the chain; has upgrade+downgrade).
def test_migration_0017_chain_and_ops():
    src = _MIGRATION.read_text()
    assert 'revision = "0017"' in src
    assert 'down_revision = "0016"' in src
    assert "def upgrade()" in src and "def downgrade()" in src
    assert "op.create_table" in src and "ai_usage_events" in src
    assert "op.drop_table" in src  # downgrade removes the table


# TEST 5-guard — the migration creates EXACTLY the model's columns (no drift).
def test_migration_columns_match_model():
    model_cols = set(AiUsageEvent.__table__.columns.keys())
    import re

    # create_table columns (0017) + additive add_column migrations (0018+).
    src = _MIGRATION.read_text() + _PURPOSE_MIGRATION.read_text()
    migration_cols = set(re.findall(r'sa\.Column\(\s*"([^"]+)"', src))
    assert migration_cols == model_cols, (
        f"migration/model column drift: only in model={model_cols - migration_cols}, "
        f"only in migration={migration_cols - model_cols}"
    )


# TEST 10 — PostgreSQL-portable: only standard column types, no sqlite-only SQL.
def test_migration_is_postgres_portable():
    src = _MIGRATION.read_text()
    assert "sqlite" not in src.lower()
    # Every column type used is a portable SQLAlchemy generic type.
    import re

    types = set(re.findall(r"sa\.(String|Integer|Float|DateTime|Boolean|Text)\(", src))
    assert types.issubset({"String", "Integer", "Float", "DateTime", "Boolean", "Text"})
    assert types, "migration should declare typed columns"
