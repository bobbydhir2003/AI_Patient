import importlib
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    access,
    admin,
    admin_load_tests,
    admin_runtime,
    admin_system,
    admin_traffic,
    admin_usage,
    admin_users,
    assessments,
    auth,
    cases,
    health,
    interviews,
    queue as queue_api,
    sessions,
    students,
    voice,
)
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging


@asynccontextmanager
async def _lifespan(_: FastAPI):
    settings = get_settings()
    workers_on = settings.background_workers_enabled
    # Startup: begin the live-chart sampler, the assessment queue worker, and
    # this process's Redis worker-presence heartbeat (see core/worker_registry).
    if workers_on:
        from app.core.assessment_worker import get_assessment_worker
        from app.core.telemetry import get_telemetry
        from app.core.worker_registry import get_heartbeat

        get_telemetry().start_sampler(
            settings.telemetry_sample_interval_seconds, settings.active_user_window_seconds
        )
        if settings.assessment_queue_enabled:
            get_assessment_worker().start()
        get_heartbeat().start()  # no-op when Redis is not configured
    yield
    # Shutdown: stop background workers and release the ElevenLabs keep-alive pool.
    from app.voice.elevenlabs_client import close_http_client

    try:
        if workers_on:
            from app.core.assessment_worker import get_assessment_worker
            from app.core.telemetry import get_telemetry
            from app.core.worker_registry import get_heartbeat

            get_assessment_worker().stop()
            get_telemetry().stop_sampler()
            get_heartbeat().stop()  # deletes this worker's Redis record immediately
    finally:
        close_http_client()


def _install_telemetry_middleware(_app: FastAPI) -> None:
    from app.core.security import decode_access_token
    from app.core.telemetry import get_telemetry

    @_app.middleware("http")
    async def _telemetry_mw(request: Request, call_next):
        tele = get_telemetry()
        # Attribute activity to the authenticated user (best-effort, no DB hit).
        auth_header = request.headers.get("Authorization") or request.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            try:
                sub = decode_access_token(auth_header.split(" ", 1)[1].strip()).get("sub")
                if sub:
                    tele.live.touch_user(sub)
            except Exception:
                pass
        start = time.monotonic()
        tele.http_in_flight.inc()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            tele.http_in_flight.dec()
            tele.record_http(status_code, (time.monotonic() - start) * 1000.0)


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
    _install_telemetry_middleware(_app)
    register_exception_handlers(_app)

    _app.include_router(health.router, prefix="/api")
    _app.include_router(auth.router, prefix="/api")
    _app.include_router(students.router, prefix="/api")
    _app.include_router(admin.router, prefix="/api")
    _app.include_router(cases.router, prefix="/api")
    _app.include_router(admin_system.router, prefix="/api")
    _app.include_router(admin_runtime.router, prefix="/api")
    _app.include_router(admin_traffic.router, prefix="/api")
    _app.include_router(admin_usage.router, prefix="/api")
    _app.include_router(admin_load_tests.router, prefix="/api")
    _app.include_router(admin_users.router, prefix="/api")
    _app.include_router(access.public_router, prefix="/api")
    _app.include_router(access.admin_router, prefix="/api")
    _app.include_router(sessions.router, prefix="/api")
    _app.include_router(interviews.router, prefix="/api")
    _app.include_router(queue_api.router, prefix="/api")
    _app.include_router(assessments.router, prefix="/api")
    _app.include_router(voice.router, prefix="/api")

    if settings.auto_create_tables:
        from app.database.base import Base
        from app.database.connection import get_engine
        importlib.import_module("app.models")  # register models without binding 'app'

        Base.metadata.create_all(bind=get_engine())

    return _app


app = create_app()
