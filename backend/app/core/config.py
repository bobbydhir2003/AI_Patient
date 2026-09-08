"""Application settings loaded from environment / .env file."""
import logging
import re
from functools import lru_cache

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Environments where security-critical configuration must be strict (fail-closed).
STRICT_ENVIRONMENTS = ("production", "staging")
# The development-only JWT secret. Never valid in production/staging.
INSECURE_JWT_DEFAULT = "dev-insecure-change-me"
# HS256 keys shorter than this are rejected outside development.
JWT_MIN_SECRET_LENGTH = 32


class ConfigError(RuntimeError):
    """Raised at startup when production configuration is unsafe (fail-closed)."""

# Numeric tuning knobs that must never take down the whole app because of a
# stray unit suffix in a .env value (e.g. `ELEVENLABS_CACHE_MAX_ENTRIES=24s`).
# Critical settings (database_url, keys, secrets) are intentionally NOT in this
# list - a bad value there should still fail loudly.
_LENIENT_NUMERIC_FIELDS = (
    "openai_timeout_seconds",
    "openai_assessment_timeout_seconds",
    "openai_max_output_tokens",
    "openai_max_retries",
    # NOTE: access_token_expire_minutes is intentionally NOT lenient - a bad
    # token-lifetime value is security-critical and must fail loudly, never
    # silently fall back to a default.
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "PT AI Patient Simulator API"
    # Fail-closed default: an image that forgets to set ENVIRONMENT is treated
    # as production (strict validation) rather than silently running with dev
    # security. Local dev/tests set ENVIRONMENT=development explicitly.
    environment: str = "production"
    debug: bool = False

    # PostgreSQL in production/dev; tests override with SQLite.
    database_url: str = "postgresql+psycopg2://ptai:ptai@localhost:5432/ptai"
    auto_create_tables: bool = False  # convenience for local dev without alembic

    # --- SQLAlchemy connection pool (PostgreSQL only; SQLite has none) ---
    # Per-worker budget. Total fleet-wide connections ~= app_workers x
    # (db_pool_size + db_max_overflow), plus a handful used by the assessment
    # worker threads within the same pool. Defaults match SQLAlchemy's own
    # implicit defaults (5 / 10) - now explicit and tunable for the planned
    # multi-worker deployment instead of relying on library defaults.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800   # recycle connections periodically (avoids stale/dropped conns)
    db_pool_timeout_seconds: float = 30.0  # wait for a free connection before raising

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # Patient-chat / default per-request OpenAI timeout (low latency).
    openai_timeout_seconds: float = 30.0
    # Assessment generation is far heavier than patient chat (large combined
    # structured output), so it gets its own, longer per-request timeout applied
    # ONLY on the assessment code path (see assessment_call_budget). Patient chat
    # keeps openai_timeout_seconds. Override with OPENAI_ASSESSMENT_TIMEOUT_SECONDS.
    openai_assessment_timeout_seconds: float = 90.0
    openai_max_output_tokens: int = 400
    openai_patient_max_output_tokens: int | None = None
    openai_standard_assessment_max_output_tokens: int | None = None
    openai_referral_extraction_max_output_tokens: int | None = None
    openai_referral_domain_max_output_tokens: int | None = None
    openai_referral_review_max_output_tokens: int | None = None

    # NOTE (P0 fix): this field is NOT wired to the OpenAI SDK client and never
    # has been - the client is built with an explicit max_retries=0 so that
    # provider_retry (below) is the SINGLE retry layer. Kept only for backward
    # compatibility with any existing .env that sets OPENAI_MAX_RETRIES; changing
    # it has no runtime effect. Retry behavior is governed by provider_max_retries.
    openai_max_retries: int = 1  # DEAD CONFIG - see note above (unused)

    # --- Legacy typed-chat streaming flag (DORMANT) ---
    # The old typed HTTP patient path (/messages, /messages/stream) and its
    # ElevenLabs TTS have been removed; the patient conversation now runs only
    # through LiveKit + OpenAI Realtime. This flag no longer gates any live
    # request path and is retained only so existing .env files / the load-test
    # harness do not error on an unknown key.
    openai_patient_streaming_enabled: bool = False

    # --- LiveKit (production interview voice path - see app/livekit_agent/) ---
    # The student's spoken (and typed-during-interview) conversation runs over
    # LiveKit + OpenAI Realtime. All blank/disabled by default; the token
    # endpoint reports itself unavailable until an operator supplies real
    # LiveKit Cloud credentials via environment variables. The
    # API secret stays backend-only, exactly like ELEVENLABS_API_KEY above.
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_poc_enabled: bool = False
    # How long a minted POC room-join token remains valid.
    livekit_token_ttl_minutes: int = 60
    # Fixed, server-controlled dispatch name (Phase 2) - NEVER derived from
    # client/request input. Both livekit_token_service.py's explicit-dispatch
    # RoomAgentDispatch and livekit_agent/worker.py's WorkerOptions(agent_name=)
    # must reference the SAME value or dispatch silently never reaches the
    # worker (a mismatch is not a security issue, just a "nothing happens"
    # bug) - changing this requires updating both sides together.
    livekit_agent_name: str = "ptai-patient-agent"

    # --- Voice engine selection. "livekit" (LiveKit + OpenAI Realtime
    # prompt_agent) is the only supported interview voice architecture now -
    # the legacy browser SpeechRecognition + ElevenLabs path has been
    # retired. Kept as a field (rather than removed outright) so an
    # explicitly-set unsupported value fails loudly instead of silently
    # selecting a different engine. See student_livekit_enabled() in
    # livekit_token_service.py, which also requires this to be "livekit"
    # before it will ever mint a student token.
    voice_engine: str = "livekit"

    # --- OpenAI Realtime native-voice engine (POC - see
    # app/livekit_agent/realtime_client.py / realtime_session.py). A dedicated,
    # DEFAULT-OFF master switch, deliberately independent of the legacy Phase
    # 2-7 semantic flags above (server STT / Smart Turn / Phase 7 timers): the
    # Realtime engine REPLACES that whole input turn-taking stack for a session,
    # it does not layer on top of it. When false (the default), the worker
    # behaves byte-for-byte as today - the legacy Silero/Deepgram/SmartTurn/
    # ElevenLabs stack is untouched and remains the only path. Requires a real
    # OPENAI_API_KEY to actually engage (see realtime_engine_active below).
    # Conceptually this is the "VOICE_ENGINE=openai_realtime" selection, kept as
    # its own boolean so it can never change the existing voice_engine enum's
    # student-token-minting semantics.
    livekit_realtime_engine_enabled: bool = False
    # Selects which OpenAI Realtime architecture the worker uses. "prompt_agent"
    # is the only supported mode: it hands the whole conversation to OpenAI
    # Realtime (hosted prompt per patient, server_vad, native create_response)
    # and only persists/streams the final transcripts - see
    # app/livekit_agent/realtime_prompt_agent.py. The old backend-controlled
    # pipeline (patient_adapter-driven generation + semantic-vad turn control)
    # has been retired; this field is kept (rather than removed outright) so an
    # explicitly-set unsupported value fails loudly instead of silently
    # selecting a different engine.
    openai_realtime_engine_mode: str = "prompt_agent"
    # prompt_agent hosted-prompt IDs, one per canonical case_id. These are
    # SECRETS supplied per environment (never hardcoded, never sent to the
    # frontend) and resolved server-side from the interview's trusted case_id
    # (see realtime_patient_configs.resolve_patient_config). Empty => that
    # patient is not yet configured and its interview fails to start loudly.
    openai_realtime_carly_prompt_id: str = ""
    openai_realtime_camden_prompt_id: str = ""
    openai_realtime_sofia_prompt_id: str = ""
    openai_realtime_jayden_prompt_id: str = ""
    # GA Realtime model + native voice. "gpt-realtime" is the current GA model;
    # "marin"/"cedar" are OpenAI's recommended voices. Single voice for the POC.
    openai_realtime_model: str = "gpt-realtime"
    openai_realtime_voice: str = "marin"
    # Input-audio transcription model for the authoritative student transcript
    # (Realtime transcribes the student's speech; see Phase G reconciliation).
    openai_realtime_transcription_model: str = "gpt-4o-mini-transcribe"
    # semantic_vad eagerness: "low" waits longest through thinking pauses (best
    # fit for the "When your pain started, were you... [pause] ...walking or
    # sitting?" case). One of low/medium/high/auto.
    openai_realtime_semantic_eagerness: str = "low"

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    log_level: str = "INFO"

    # --- Authentication (JWT) ---
    # SECRET must be overridden in production via the JWT_SECRET_KEY env var.
    # The default below is only a development convenience and is never secure.
    jwt_secret_key: str = "dev-insecure-change-me"
    jwt_algorithm: str = "HS256"

    # Server-side key that encrypts API credentials stored in the runtime config
    # table. Lives OUTSIDE the database. Empty => secure secret storage disabled.
    config_encryption_key: str = ""
    access_token_expire_minutes: int = 60 * 12  # 12 hours
    # Allow open student self-registration through POST /api/auth/register.
    allow_student_self_registration: bool = True
    # When true, registration requires the email to have an APPROVED access
    # request (admin-gated). When false, registration behaves exactly as before.
    require_access_approval: bool = False

    # --- Rate limiting (abuse / cost protection) ---
    # Limits are process-local (per uvicorn worker); see A6 in the report. Values
    # use slowapi's "<count>/<period>" syntax. Set RATE_LIMIT_ENABLED=false to
    # disable entirely (e.g. behind an external gateway that already throttles).
    rate_limit_enabled: bool = True
    login_rate_limit: str = "10/minute"
    interview_rate_limit: str = "30/minute"
    voice_rate_limit: str = "60/minute"
    assessment_rate_limit: str = "10/minute"

    # --- Login brute-force throttle (in addition to the IP rate limit) ---
    # Failed attempts are counted per (IP, normalized-email) pair. After
    # login_max_failed_attempts within the window, further attempts are refused
    # for login_lockout_seconds. Never permanently locks an account; the counter
    # resets on a successful login. Process-local (see A9 in the report).
    login_throttle_enabled: bool = True
    login_max_failed_attempts: int = 5
    login_attempt_window_seconds: int = 900   # 15 minutes
    login_lockout_seconds: int = 300          # 5 minutes

    # --- Request-size guards (reject before spending on paid APIs) ---
    max_student_message_chars: int = 2000
    max_transcript_turn_chars: int = 4000

    # =====================================================================
    #  Priority B - traffic control, telemetry & scalability
    # =====================================================================
    # Telemetry (all process-local / in-memory).
    active_user_window_seconds: int = 120        # "active" = activity within this window
    telemetry_sample_interval_seconds: int = 15  # live-chart history sampling cadence
    live_session_idle_prune_seconds: int = 900   # drop live-session rows after inactivity

    # Concurrency guards. This is a GLOBAL (fleet-wide) limit when Redis is
    # configured (see redis_url below) - one shared OpenAI budget across all
    # uvicorn workers, not per-process.
    max_concurrent_ai_interviews: int = 20
    ai_interview_wait_seconds: float = 2.0       # bounded wait before a 503 overload

    # --- Redis (fleet-wide concurrency control across multiple uvicorn workers) ---
    # Empty by default (single-worker/local-dev safe). REQUIRED in production/
    # staging unless redis_required_for_concurrency is explicitly overridden -
    # see _enforce_production_safety below. Without Redis, OpenAI/TTS/assessment
    # concurrency limits are per-process only (effective limit = configured x
    # worker count), which is unsafe once running more than one uvicorn worker.
    redis_url: str = ""
    # None = auto (required in production/staging, optional in development).
    # Set explicitly to force either behavior.
    redis_required_for_concurrency: bool | None = None
    redis_connect_timeout_seconds: float = 0.5
    redis_socket_timeout_seconds: float = 0.5
    # Short cache TTL for the reachability pre-check the concurrency guard
    # (DistributedSemaphore) runs before every acquire. Without this, EVERY
    # TTS/interview slot acquisition costs an extra PING round trip on top of
    # the Lua acquire script. Caching this fast-path check for a brief window
    # is safe: the Lua acquire/release call - which is what actually
    # determines correctness - still hits Redis on every single call; this
    # only decides whether to attempt it or fail fast when Redis is known-down.
    redis_health_cache_seconds: float = 1.0
    # How long a held concurrency slot survives without being released before
    # it self-expires (protects against a crashed worker leaking a slot
    # forever). Must comfortably exceed the slowest real call this guards
    # (OpenAI/ElevenLabs timeout + retries, or an assessment job).
    redis_semaphore_lease_seconds: float = 180.0

    # Master switch for background threads (telemetry sampler + assessment worker).
    # Disabled in the test suite, which drives the worker deterministically.
    background_workers_enabled: bool = True

    # --- Worker presence monitoring (Redis-backed heartbeat; see
    # core/worker_registry.py). Each uvicorn worker registers itself in Redis
    # and refreshes a short-TTL record so the System Dashboard can observe the
    # LIVE fleet (never a config-derived count). TTL must comfortably exceed the
    # heartbeat interval so a slightly late beat does not flap a healthy worker.
    worker_heartbeat_interval_seconds: int = 3   # ~2-5s per requirements
    worker_heartbeat_ttl_seconds: int = 12       # record self-expires if beats stop

    # --- OpenAI capacity tracking (real provider-reported usage) ---
    # Configure to match your OpenAI project tier for the active model
    # (gpt-4o-mini). Defaults below match a ~250K TPM / 3K RPM tier - override
    # via OPENAI_TPM_LIMIT/OPENAI_RPM_LIMIT if your tier differs.
    openai_tpm_limit: int = 250_000   # tokens per minute
    openai_rpm_limit: int = 3_000     # requests per minute
    openai_capacity_busy_pct: float = 0.70
    openai_capacity_protecting_pct: float = 0.85
    openai_capacity_critical_pct: float = 0.95

    # Assessment background queue (DB-backed; live interviews get priority).
    assessment_queue_enabled: bool = True
    assessment_worker_concurrency: int = 3
    # Adaptive throttling: effective workers by OpenAI capacity state.
    assessment_workers_busy: int = 2
    assessment_workers_protecting: int = 1
    assessment_pause_on_critical: bool = True
    assessment_poll_interval_seconds: float = 1.0

    # HARD ceiling on LOGICAL OpenAI generation calls per single standard
    # assessment execution. The redesigned pipeline uses 2 normally (combined
    # generate + independent verify) and at most 3 (one combined correction).
    # This is a fail-closed safety net: if logic ever tried to exceed it, the
    # assessment is marked NEEDS_REVIEW rather than making another provider call,
    # so a bug or bad response can never recreate the old 6-11 call runaway.
    assessment_max_openai_calls: int = 3
    # Referral assessments legitimately fan out over seven domains; they share
    # the usage-recording + call-counting infrastructure but get their own,
    # higher safety ceiling (never the 3-call standard cap).
    referral_assessment_max_openai_calls: int = 20

    # Provider retry/backoff (transient errors only).
    provider_max_retries: int = 3                # attempts AFTER the first try
    provider_retry_base_ms: int = 200
    provider_retry_max_ms: int = 4000

    # Bounded actor context.
    actor_max_recent_turns: int = 12             # last-N transcript turns sent to the model
    actor_context_char_limit: int = 12000        # secondary soft cap on history characters

    # Deployment description (cannot be auto-detected reliably; declared here).
    deployment_mode: str = "single_instance"     # single_instance | multi_worker | multi_node
    app_workers: int = 1                         # uvicorn/gunicorn worker count (informational)

    # Mock provider mode (for load testing WITHOUT spending OpenAI credits).
    # Never enable in production; normal behavior is unchanged when false. This is
    # the STARTUP default; the load-test controller can set a runtime DB override
    # (system_settings["mock_ai"]) for the duration of a Simulated-AI test.
    mock_ai: bool = False
    mock_model_latency_ms: int = 800

    # --- Load & Capacity Testing (J) ---
    load_test_enabled: bool = True
    load_test_max_users: int = 170              # hard safety cap on target users (scalability target)
    # Real-provider (paid) load tests spend real OpenAI/ElevenLabs credits, so
    # they get a MUCH smaller user cap than simulated runs. A real-provider
    # request above this cap is rejected before any provider traffic starts.
    load_test_real_provider_max_users: int = 10
    load_test_max_duration_seconds: int = 3600  # 60 min ceiling (soak)
    load_test_target_base_url: str = "http://127.0.0.1:8000"

    # Alert / health thresholds (configurable).
    alert_p95_latency_ms: int = 2000
    alert_error_rate: float = 0.05
    alert_ai_concurrency_pct: float = 0.8
    alert_assessment_queue: int = 20
    alert_db_pool_pct: float = 0.9
    alert_cpu_pct: float = 0.90
    alert_memory_pct: float = 0.90
    health_elevated_cpu_pct: float = 0.75
    health_elevated_memory_pct: float = 0.75

    # --- First-admin bootstrap (used by scripts/create_admin.py only) ---
    admin_email: str = ""
    admin_password: str = ""
    admin_full_name: str = "Administrator"

    @field_validator(*_LENIENT_NUMERIC_FIELDS, mode="before")
    @classmethod
    def _coerce_lenient_numeric(cls, value: object, info: ValidationInfo) -> object:
        """Tolerate a stray non-numeric suffix on tuning values.

        A `.env` typo like `ELEVENLABS_CACHE_MAX_ENTRIES=24s` used to raise a
        ValidationError inside create_app(), which crashed uvicorn on startup
        and made EVERY /api route (including /api/cases) unreachable - surfacing
        to students as "Could not load patient cases". These are non-critical
        performance knobs, so we strip a trailing unit suffix (24s -> 24) and,
        if the value is still unparseable, fall back to the field default -
        always with a warning so the misconfiguration is visible in the logs.
        """
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        match = re.match(r"^[-+]?\d+(?:\.\d+)?", stripped)
        if match and match.group(0) != stripped:
            logger.warning(
                "Config %s=%r has a non-numeric suffix; using %r instead.",
                info.field_name, value, match.group(0),
            )
            return match.group(0)
        if match:
            return value
        default = cls.model_fields[info.field_name].default
        logger.warning(
            "Config %s=%r is not a valid number; falling back to default %r.",
            info.field_name, value, default,
        )
        return default

    @field_validator("voice_engine", mode="before")
    @classmethod
    def _validate_voice_engine(cls, value: object) -> object:
        """Fail safe, never fail loud: an unrecognized VOICE_ENGINE value
        (typo, stray whitespace, leftover placeholder, or the retired
        "legacy") must never crash the app - it falls back to the only
        supported engine ('livekit') with a visible warning, the same
        discipline _coerce_lenient_numeric already applies to non-critical
        tuning knobs above."""
        valid = ("livekit",)
        normalized = str(value).strip().lower() if value is not None else "livekit"
        if normalized in valid:
            return normalized
        logger.warning(
            "Config VOICE_ENGINE=%r is not one of %s; falling back to 'livekit'.", value, valid,
        )
        return "livekit"

    @field_validator("access_token_expire_minutes")
    @classmethod
    def _validate_token_lifetime(cls, value: int) -> int:
        # Security-critical: an invalid lifetime must fail loudly (pydantic
        # already rejects non-numeric input). Reject non-positive / absurd values
        # rather than silently accepting a token that never or always expires.
        if value <= 0:
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES must be a positive number of minutes.")
        if value > 60 * 24 * 30:  # 30 days
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES is unreasonably large (max 30 days).")
        return value

    @field_validator("openai_realtime_engine_mode", mode="before")
    @classmethod
    def _validate_realtime_engine_mode(cls, value: object) -> object:
        normalized = str(value or "prompt_agent").strip().lower()
        if normalized != "prompt_agent":
            raise ValueError("OPENAI_REALTIME_ENGINE_MODE must be 'prompt_agent'")
        return normalized

    @property
    def is_strict_environment(self) -> bool:
        return self.environment.strip().lower() in STRICT_ENVIRONMENTS

    @property
    def redis_required(self) -> bool:
        """Whether Redis is REQUIRED for global OpenAI/TTS/assessment
        concurrency control. Explicit override wins; otherwise required in
        production/staging (fail closed) and optional in development (local
        per-process fallback), matching every other strict-vs-dev split in
        this file (e.g. the JWT secret check below)."""
        if self.redis_required_for_concurrency is not None:
            return self.redis_required_for_concurrency
        return self.is_strict_environment

    @staticmethod
    def _jwt_secret_is_insecure(secret: str) -> bool:
        secret = (secret or "").strip()
        return (
            not secret
            or secret == INSECURE_JWT_DEFAULT
            or len(secret) < JWT_MIN_SECRET_LENGTH
        )

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> "Settings":
        """Fail closed: in production/staging the app refuses to start with an
        insecure JWT secret, and never runs with debug enabled."""
        if self.is_strict_environment:
            if self._jwt_secret_is_insecure(self.jwt_secret_key):
                raise ConfigError(
                    "JWT_SECRET_KEY is missing, blank, the insecure development "
                    f"default, or shorter than {JWT_MIN_SECRET_LENGTH} characters. "
                    "Set a strong JWT_SECRET_KEY before starting in "
                    f"'{self.environment}'. Generate one with: "
                    'python -c "import secrets; print(secrets.token_urlsafe(48))"'
                )
            if self.debug:
                # Never leak debug behavior in a strict environment.
                logger.warning("DEBUG was enabled in '%s'; forcing debug=false.", self.environment)
                object.__setattr__(self, "debug", False)
            if not self.config_encryption_key.strip():
                logger.warning(
                    "CONFIG_ENCRYPTION_KEY is not set in '%s'; encrypted API-key "
                    "storage is disabled and provider keys fall back to env vars.",
                    self.environment,
                )
            if self.redis_required and not self.redis_url.strip():
                raise ConfigError(
                    "REDIS_URL is not set, but global OpenAI/TTS/assessment concurrency "
                    f"control is required in '{self.environment}' (defaults to required "
                    "in production/staging so multiple uvicorn workers cannot silently "
                    "multiply provider concurrency limits). Provision Redis and set "
                    "REDIS_URL, or explicitly set REDIS_REQUIRED_FOR_CONCURRENCY=false to "
                    "run with per-process concurrency limits instead (NOT safe with more "
                    "than one uvicorn worker)."
                )
            if self.redis_required_for_concurrency is False:
                logger.warning(
                    "REDIS_REQUIRED_FOR_CONCURRENCY=false in '%s': OpenAI/TTS/assessment "
                    "concurrency limits will be PER-PROCESS if Redis is unset or "
                    "unreachable. With more than one uvicorn worker this multiplies the "
                    "effective provider concurrency by the worker count.",
                    self.environment,
                )
        elif self._jwt_secret_is_insecure(self.jwt_secret_key):
            # Development convenience: allow the weak default but make it visible.
            logger.warning(
                "Using an insecure JWT secret in '%s'. This is only safe for local "
                "development; production/staging will refuse to start.",
                self.environment,
            )
        return self

    @property
    def realtime_engine_active(self) -> bool:
        """OpenAI Realtime voice engine: true only when explicitly enabled AND
        a real OpenAI key is configured. When false (the default), the worker
        never opens a Realtime session - see worker.py's
        _maybe_start_realtime_session, which fails safe to None in every
        off/unconfigured case."""
        return self.livekit_realtime_engine_enabled and bool(self.openai_api_key)

    @property
    def realtime_prompt_agent_active(self) -> bool:
        """prompt_agent engine: OpenAI Realtime owns the whole conversation
        (hosted prompt per patient). True whenever the Realtime engine is
        enabled/keyed - "prompt_agent" is the only mode `openai_realtime_
        engine_mode` can validate to now, so this is equivalent to
        `realtime_engine_active` but kept as its own property since callers
        read it as "is the (only) Realtime engine active" for clarity."""
        return self.realtime_engine_active and self.openai_realtime_engine_mode == "prompt_agent"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
