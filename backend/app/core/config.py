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
    "openai_max_output_tokens",
    "openai_max_retries",
    "patient_streaming_first_audio_target_ms",
    "elevenlabs_timeout_seconds",
    "elevenlabs_max_text_chars",
    "elevenlabs_cache_max_entries",
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

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 30.0
    openai_max_output_tokens: int = 400
    openai_patient_max_output_tokens: int | None = None
    openai_standard_assessment_max_output_tokens: int | None = None
    openai_referral_extraction_max_output_tokens: int | None = None
    openai_referral_domain_max_output_tokens: int | None = None
    openai_referral_review_max_output_tokens: int | None = None

    openai_max_retries: int = 1  # retries after the first failed attempt

    # --- Low-latency streaming patient responses (feature-flagged) ---
    # Master switch for the streamed OpenAI text + sentence pipeline. When
    # false (default), the /messages/stream endpoint returns 409 and the
    # frontend uses the original atomic-response path. Disable instantly by
    # setting OPENAI_PATIENT_STREAMING_ENABLED=false and restarting uvicorn.
    openai_patient_streaming_enabled: bool = False
    # Sentence-level TTS pipelining (frontend speaks approved sentences as
    # they arrive). If false while streaming is on, text still streams but
    # the frontend synthesizes audio only after the final commit.
    patient_sentence_pipelining_enabled: bool = True
    # Reserved: ElevenLabs WebSocket streaming input. The current design uses
    # per-sentence HTTP over the shared keep-alive client (see VOICE docs).
    elevenlabs_streaming_input_enabled: bool = False
    # Development target for question-submitted -> first audible word.
    patient_streaming_first_audio_target_ms: int = 2000

    # --- ElevenLabs text-to-speech (patient voice) ---
    # The key stays on the backend ONLY. The browser calls FastAPI, and FastAPI
    # calls ElevenLabs. Never expose this key to the React frontend.
    elevenlabs_api_key: str = ""
    elevenlabs_enabled: bool = True
    elevenlabs_default_model: str = "eleven_multilingual_v2"
    elevenlabs_output_format: str = "mp3_44100_128"
    elevenlabs_timeout_seconds: float = 20.0
    # Patient replies are capped at MAX_PATIENT_RESPONSE_CHARS (900); this adds
    # headroom but blocks arbitrary long-text synthesis through the endpoint.
    elevenlabs_max_text_chars: int = 1200
    elevenlabs_cache_max_entries: int = 24

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

    # Concurrency guards (threading semaphores; sync endpoints run in a threadpool).
    max_concurrent_ai_interviews: int = 20
    ai_interview_wait_seconds: float = 2.0       # bounded wait before a 503 overload
    max_concurrent_tts_requests: int = 20
    tts_wait_seconds: float = 0.5                # brief wait; then degrade to text-only

    # Master switch for background threads (telemetry sampler + assessment worker).
    # Disabled in the test suite, which drives the worker deterministically.
    background_workers_enabled: bool = True

    # --- OpenAI capacity tracking (real provider-reported usage) ---
    # Configure to match your OpenAI tier for the active model (gpt-4o-mini shown).
    openai_tpm_limit: int = 200_000   # tokens per minute
    openai_rpm_limit: int = 500       # requests per minute
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

    # Mock provider mode (for load testing WITHOUT spending OpenAI/ElevenLabs).
    # Never enable in production; normal behavior is unchanged when false. This is
    # the STARTUP default; the load-test controller can set a runtime DB override
    # (system_settings["mock_ai"]) for the duration of a Simulated-AI test.
    mock_ai: bool = False
    mock_model_latency_ms: int = 800
    mock_tts_latency_ms: int = 300

    # --- Load & Capacity Testing (J) ---
    load_test_enabled: bool = True
    load_test_max_users: int = 100              # hard safety cap on target users
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

    @property
    def is_strict_environment(self) -> bool:
        return self.environment.strip().lower() in STRICT_ENVIRONMENTS

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
        elif self._jwt_secret_is_insecure(self.jwt_secret_key):
            # Development convenience: allow the weak default but make it visible.
            logger.warning(
                "Using an insecure JWT secret in '%s'. This is only safe for local "
                "development; production/staging will refuse to start.",
                self.environment,
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
