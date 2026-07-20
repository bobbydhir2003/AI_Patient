import importlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, assessments, auth, cases, health, interviews, sessions, students, voice
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging


@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    # Shutdown: release the shared keep-alive connection pool to ElevenLabs.
    from app.voice.elevenlabs_client import close_http_client

    close_http_client()


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    _app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=_lifespan)
    _app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Lets the browser read the TTS pause hint on streamed audio responses.
        expose_headers=["X-Pause-Before-Ms"],
    )
    register_exception_handlers(_app)

    _app.include_router(health.router, prefix="/api")
    _app.include_router(auth.router, prefix="/api")
    _app.include_router(students.router, prefix="/api")
    _app.include_router(admin.router, prefix="/api")
    _app.include_router(cases.router, prefix="/api")
    _app.include_router(sessions.router, prefix="/api")
    _app.include_router(interviews.router, prefix="/api")
    _app.include_router(assessments.router, prefix="/api")
    _app.include_router(voice.router, prefix="/api")

    if settings.auto_create_tables:
        from app.database.base import Base
        from app.database.connection import get_engine
        importlib.import_module("app.models")  # register models without binding 'app'

        Base.metadata.create_all(bind=get_engine())

    return _app


app = create_app()
