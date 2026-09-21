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


def test_ownership_branch_0022_from_0020_and_single_head():
    scripts = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    ownership = scripts.get_revision("0022")
    merge = scripts.get_revision("0023")
    assert ownership.down_revision == "0020"
    assert set(merge.down_revision) == {"0021", "0022"}
    # Head is single again after the merge so `alembic upgrade head` is unambiguous.
    assert scripts.get_heads() == ["0023"]


def _insert_receipt(connection, **kw):
    connection.execute(
        text(
            "INSERT INTO survey_receipts (id, student_id, case_id, redcap_record_id, "
            "pre_sync_status, pre_synced_at, post_sync_status, post_synced_at, "
            "overall_status, created_at, updated_at) VALUES (:id, :student_id, :case_id, "
            ":rec, :pre, :pre_at, :post, :post_at, :overall, :created, :updated)"
        ),
        kw,
    )


def test_0022_backfill_chooses_deterministic_owner_without_deleting(tmp_path, monkeypatch):
    """Historical multi-receipt students get ONE deterministic owner and keep
    every receipt: completed beats in-progress (N); a successful Pre beats a
    failed-only attempt (O); failed-only stays NOT_STARTED."""
    database_url = f"sqlite:///{tmp_path / 'ownership-backfill.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = _alembic_config(database_url)
    _prepare_verified_0018_database(database_url)

    command.stamp(config, "0018")
    command.upgrade(config, "0020")  # creates survey_receipts

    engine = create_engine(database_url)
    try:
        with engine.begin() as c:
            for sid in ("stu_completed", "stu_pre", "stu_failed"):
                c.execute(text("INSERT INTO students (id) VALUES (:id)"), {"id": sid})

            # N: two COMPLETED receipts -> earliest by post_synced_at wins (carly).
            _insert_receipt(c, id="r1", student_id="stu_completed", case_id="sofia",
                            rec="rec-sofia", pre="synced", pre_at="2026-01-02T00:00:00",
                            post="synced", post_at="2026-01-05T00:00:00", overall="completed",
                            created="2026-01-02T00:00:00", updated="2026-01-05T00:00:00")
            _insert_receipt(c, id="r2", student_id="stu_completed", case_id="carly",
                            rec="rec-carly", pre="synced", pre_at="2026-01-01T00:00:00",
                            post="synced", post_at="2026-01-03T00:00:00", overall="completed",
                            created="2026-01-01T00:00:00", updated="2026-01-03T00:00:00")

            # O: a failed-only Pre (camden) + a successful Pre (jayden) -> jayden owns.
            _insert_receipt(c, id="r3", student_id="stu_pre", case_id="camden",
                            rec="rec-camden", pre="failed", pre_at=None,
                            post="pending", post_at=None, overall="in_progress",
                            created="2026-02-01T00:00:00", updated="2026-02-01T00:00:00")
            _insert_receipt(c, id="r4", student_id="stu_pre", case_id="jayden",
                            rec="rec-jayden", pre="synced", pre_at="2026-02-02T00:00:00",
                            post="pending", post_at=None, overall="in_progress",
                            created="2026-02-02T00:00:00", updated="2026-02-02T00:00:00")

            # failed-only -> no owner.
            _insert_receipt(c, id="r5", student_id="stu_failed", case_id="carly",
                            rec="rec-fail", pre="failed", pre_at=None,
                            post="pending", post_at=None, overall="in_progress",
                            created="2026-03-01T00:00:00", updated="2026-03-01T00:00:00")

        command.upgrade(config, "0022")

        with engine.connect() as c:
            owners = dict(
                c.execute(text("SELECT id, survey_owner_case_id FROM students")).fetchall()
            )
            completed = dict(
                c.execute(text("SELECT id, survey_completed_at FROM students")).fetchall()
            )
            assert owners["stu_completed"] == "carly"   # N: earliest completed
            assert completed["stu_completed"] is not None
            assert owners["stu_pre"] == "jayden"         # O: successful Pre, not failed camden
            assert completed["stu_pre"] is None
            assert owners["stu_failed"] is None          # failed-only -> not started
            # Non-destructive: every historical receipt is preserved.
            assert c.execute(text("SELECT COUNT(*) FROM survey_receipts")).scalar_one() == 5
    finally:
        engine.dispose()
        get_settings.cache_clear()


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
