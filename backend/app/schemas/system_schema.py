"""Student-safe / secret-safe schemas for the technical System Dashboard.

Every value surfaced here comes from a REAL runtime check or real recorded
activity. These models never carry API keys, full voice IDs, connection
strings, or filesystem paths.
"""
from app.schemas.base import CamelModel


class BackendHealthOut(CamelModel):
    status: str  # healthy | degraded | unavailable
    response_time_ms: int | None = None
    version: str = ""
    environment: str = ""
    checked_at: str = ""


class DatabaseHealthOut(CamelModel):
    status: str  # connected | unavailable
    db_type: str = ""  # e.g. sqlite | postgresql
    latency_ms: int | None = None
    migration_version: str | None = None  # None => not available/unknown
    checked_at: str = ""


class RedisHealthOut(CamelModel):
    """Global (fleet-wide) concurrency control backing store. `required`
    reflects whether this environment fails closed without Redis (see
    Settings.redis_required)."""

    status: str  # connected | unavailable | not_configured
    required: bool = False
    latency_ms: int | None = None
    checked_at: str = ""


class ServiceHealthOut(CamelModel):
    """OpenAI. `status` is 'configured' or 'not_configured' - never 'connected'
    unless a real Test Connection has actually run (deferred)."""

    service: str
    configured: bool
    status: str  # configured | not_configured
    model: str = ""
    streaming_enabled: bool | None = None
    last_success_at: str | None = None  # None => never recorded
    last_error: str | None = None
    checked_at: str = ""


class StorageHealthOut(CamelModel):
    status: str  # healthy | warning | unavailable
    used_bytes: int | None = None
    total_bytes: int | None = None
    free_bytes: int | None = None
    percent_used: float | None = None
    checked_at: str = ""


class OpenAIConfigOut(CamelModel):
    configured: bool
    model: str = ""
    streaming_enabled: bool = False
    timeout_seconds: float | None = None
    max_output_tokens: int | None = None
    status: str = ""


class AiConfigurationOut(CamelModel):
    openai: OpenAIConfigOut


class CredentialStatusOut(CamelModel):
    """Status only. The full key is NEVER returned. 'updatedAt'/'updatedBy' are
    null because key-change history is not tracked in this deployment."""

    service: str
    configured: bool
    masked_value: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    status: str  # configured | not_configured


class AlertOut(CamelModel):
    id: str
    severity: str  # info | warning | critical
    service: str
    message: str
    detected_at: str
    state: str = "active"
    count: int = 1


class ActivityOut(CamelModel):
    id: str
    admin: str
    action: str
    target: str = ""
    result: str = "success"
    timestamp: str


class WorkerOut(CamelModel):
    """A single OBSERVED backend worker. Every value is self-reported by that
    worker from its own real telemetry/process state - never fabricated.
    Fields that a worker cannot measure (e.g. memory without psutil) are null."""

    worker_id: str
    pid: int | None = None
    hostname: str = ""
    health: str = "healthy"  # healthy | stale (heartbeat aging) | unavailable
    uptime_seconds: int | None = None
    heartbeat_at: str | None = None
    heartbeat_age_seconds: float | None = None
    requests_total: int | None = None
    requests_per_minute: float | None = None
    http_in_flight: int | None = None
    interview_in_flight: int | None = None
    assessment_in_flight: int | None = None
    memory_mb: float | None = None
    # Per requirements: the app does NOT record a reliable per-worker "current
    # task" label, so this is intentionally null (never an invented value).
    current_task: str | None = None


class WorkerFleetOut(CamelModel):
    """Observed vs configured worker presence.

    monitoring:
      - observed    : Redis reachable; `workers` is the live fleet.
      - local_only  : Redis not configured (dev/single-process); only THIS
                      process is visible - fleet health is NOT claimed.
      - unavailable : Redis configured but unreachable; fleet not observable.
    status: healthy (observed == configured) | degraded (mismatch) |
            unavailable | local_only.
    """

    monitoring: str
    status: str
    mode: str = ""  # deployment_mode (informational, from config)
    configured: int
    observed: int | None = None
    healthy: int | None = None
    heartbeat_interval_seconds: int | None = None
    heartbeat_ttl_seconds: int | None = None
    note: str = ""
    workers: list[WorkerOut] = []


class ConcurrencyLaneOut(CamelModel):
    """One global concurrency lane (OpenAI / TTS / assessment). `active` is the
    real fleet-wide count via the Redis semaphore when scope == 'global', or the
    per-process in-flight count when scope == 'process' (Redis fallback)."""

    name: str
    active: int
    limit: int
    scope: str  # global | process
    waiting: int | None = None
    queued: int | None = None


class ConcurrencyOut(CamelModel):
    scope: str  # global (redis) | per_process
    redis: RedisHealthOut
    openai: ConcurrencyLaneOut
    assessment: ConcurrencyLaneOut


class InfraCheckOut(CamelModel):
    """One realtime infrastructure check. `status` is the REAL result of the
    check; a green state is never shown unless the check actually succeeded."""

    key: str
    label: str
    status: str  # healthy | degraded | unavailable | misconfigured | not_configured
    detail: str = ""


class SystemLiveOut(CamelModel):
    """Lean, fast-polling payload (backend + fleet + concurrency + checks +
    alerts). Excludes the heavier config sections that only need a first load."""

    generated_at: str
    backend: BackendHealthOut
    database: DatabaseHealthOut
    redis: RedisHealthOut
    openai: ServiceHealthOut
    workers: WorkerFleetOut
    concurrency: ConcurrencyOut
    checks: list[InfraCheckOut]
    alerts: list[AlertOut]


class SystemOverviewOut(CamelModel):
    generated_at: str
    backend: BackendHealthOut
    database: DatabaseHealthOut
    redis: RedisHealthOut
    openai: ServiceHealthOut
    storage: StorageHealthOut
    ai_config: AiConfigurationOut
    credentials: list[CredentialStatusOut]
    alerts: list[AlertOut]
    activity: list[ActivityOut]
    # Live runtime sections (also polled on their own via /admin/system/live):
    workers: WorkerFleetOut
    concurrency: ConcurrencyOut
    checks: list[InfraCheckOut]


class MutationResultOut(CamelModel):
    success: bool
    message: str = ""
