"""Safety tests for the additive AI iEXCEL LTI migration branch (iexcel_lti_01)."""
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.core.config import get_settings
from app.models import (
    ContextCaseAssignment,
    ContextMember,
    CourseContext,
    LtiLoginCode,
    LtiLoginState,
    PlatformEventOutbox,
    PlatformIdentity,
)
from tests.test_survey_migration import _alembic_config

NEW_TABLES = {
    m.__tablename__: m
    for m in (
        ContextCaseAssignment, ContextMember, CourseContext, LtiLoginCode,
        LtiLoginState, PlatformEventOutbox, PlatformIdentity,
    )
}


def test_lti_revision_branches_from_production_survey_revision():
    scripts = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    assert scripts.get_revision("iexcel_lti_01").down_revision == "0020"
    assert set(scripts.get_revision("iexcel_lti_02").down_revision) == {"0021", "iexcel_lti_01"}
    assert scripts.get_heads() == ["iexcel_lti_02"]


def test_targeted_lti_revision_is_additive_and_reversible(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'at-0020.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        # Just the referenced tables, with a legacy row and the 0019 sentinel.
        connection.execute(text("CREATE TABLE users (id VARCHAR(32) PRIMARY KEY, email VARCHAR(255))"))
        connection.execute(text(
            "CREATE TABLE interview_sessions (id VARCHAR(32) PRIMARY KEY, case_id VARCHAR(50))"
        ))
        connection.execute(text("CREATE TABLE patient_voice_settings (id VARCHAR(32) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO users VALUES ('u1', 'legacy@school.test')"))
        connection.execute(text("INSERT INTO interview_sessions VALUES ('s1', 'camden')"))
        connection.execute(text("INSERT INTO patient_voice_settings VALUES ('preserve-me')"))
    try:
        command.stamp(config, "0020")
        command.upgrade(config, "iexcel_lti_01")
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert set(NEW_TABLES) <= tables and "patient_voice_settings" in tables
        for name, model in NEW_TABLES.items():
            assert {c["name"] for c in inspector.get_columns(name)} == {
                c.name for c in model.__table__.columns
            }, name
        columns = {c["name"]: c for c in inspector.get_columns("interview_sessions")}
        assert columns["context_id"]["nullable"] is True
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "iexcel_lti_01"
            assert connection.execute(
                text("SELECT id, case_id, context_id FROM interview_sessions")
            ).one() == ("s1", "camden", None)
            assert connection.execute(text("SELECT email FROM users")).scalar_one() == "legacy@school.test"

        command.downgrade(config, "0020")
        inspector = inspect(engine)
        assert not set(NEW_TABLES) & set(inspector.get_table_names())
        assert "context_id" not in {c["name"] for c in inspector.get_columns("interview_sessions")}
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id FROM interview_sessions")).scalar_one() == "s1"
            assert connection.execute(text("SELECT id FROM patient_voice_settings")).scalar_one() == "preserve-me"
    finally:
        engine.dispose()
        get_settings.cache_clear()
