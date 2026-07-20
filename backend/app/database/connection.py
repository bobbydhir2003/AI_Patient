"""Engine / session factory and FastAPI dependency."""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        kwargs: dict = {"pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False}}
        _engine = create_engine(settings.database_url, **kwargs)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return _engine


def get_session_factory() -> sessionmaker:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


def get_db_factory():
    """FastAPI dependency returning a SESSION FACTORY (not a session).

    Used by streaming endpoints that must open several SHORT-lived sessions
    over the lifetime of one streamed response instead of holding a single
    session/transaction open for the whole stream. Tests override this
    dependency with a factory bound to the test engine.
    """
    return get_session_factory()


def reset_engine() -> None:
    """Used by tests to rebuild the engine after settings change."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
