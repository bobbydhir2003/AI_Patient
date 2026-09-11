"""Focused safety tests for the production survey migration branch."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.core.config import get_settings
from app.models import SurveyReceipt


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(BACKEND_ROOT / "app" / "database" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _prepare_verified_0018_database(database_url: str) -> None:
    """Create only the tables 0020 references plus the production sentinel.

    The database is stamped at the already-verified production revision. This
    avoids exercising unrelated historical migrations and makes any accidental
    execution of destructive 0019 immediately visible.
    """
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE students (id VARCHAR(32) PRIMARY KEY)"))
            connection.execute(
                text("CREATE TABLE interview_sessions (id VARCHAR(32) PRIMARY KEY)")
            )
            connection.execute(
                text("CREATE TABLE patient_voice_settings (id VARCHAR(32) PRIMARY KEY)")
            )
            connection.execute(
                text("INSERT INTO patient_voice_settings (id) VALUES ('preserve-me')")
            )
    finally:
        engine.dispose()


def test_survey_revision_is_an_independent_branch_from_0018():
    scripts = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    survey = scripts.get_revision("0020")
    merge = scripts.get_revision("0021")

    assert survey.down_revision == "0018"
    assert set(merge.down_revision) == {"0019", "0020"}
    assert scripts.get_heads() == ["0021"]


def test_targeted_0020_upgrade_and_downgrade_touch_only_survey_schema(
    tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'production-at-0018.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    _prepare_verified_0018_database(database_url)

    command.stamp(config, "0018")
    command.upgrade(config, "0020")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "survey_receipts" in inspector.get_table_names()
        assert "survey_submissions" not in inspector.get_table_names()
        assert "patient_voice_settings" in inspector.get_table_names()

        database_columns = {column["name"] for column in inspector.get_columns("survey_receipts")}
        model_columns = {column.name for column in SurveyReceipt.__table__.columns}
        assert database_columns == model_columns

        nullable = {
            column["name"]: column["nullable"]
            for column in inspector.get_columns("survey_receipts")
        }
        assert {name for name, value in nullable.items() if value} == {
            "latest_session_id",
            "pre_synced_at",
            "post_synced_at",
        }

        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("survey_receipts")
        }
        assert unique_constraints == {
            "uq_survey_receipts_student_case": ("student_id", "case_id")
        }

        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("survey_receipts")
        }
        assert indexes == {
            "ix_survey_receipts_case_id": ("case_id",),
            "ix_survey_receipts_student_id": ("student_id",),
        }

        foreign_keys = {
            tuple(foreign_key["constrained_columns"]): (
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
            )
            for foreign_key in inspector.get_foreign_keys("survey_receipts")
        }
        assert foreign_keys == {
            ("student_id",): ("students", ("id",)),
            ("latest_session_id",): ("interview_sessions", ("id",)),
        }

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0020"
            assert connection.execute(
                text("SELECT id FROM patient_voice_settings")
            ).scalar_one() == "preserve-me"
    finally:
        engine.dispose()

    command.downgrade(config, "0018")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "survey_receipts" not in inspector.get_table_names()
        assert "patient_voice_settings" in inspector.get_table_names()
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0018"
            assert connection.execute(
                text("SELECT id FROM patient_voice_settings")
            ).scalar_one() == "preserve-me"
    finally:
        engine.dispose()
        get_settings.cache_clear()
