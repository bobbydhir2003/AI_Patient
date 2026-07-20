"""Application settings loaded from environment / .env file."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "PT AI Patient Simulator API"
    environment: str = "development"
    debug: bool = True

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
    access_token_expire_minutes: int = 60 * 12  # 12 hours
    # Allow open student self-registration through POST /api/auth/register.
    allow_student_self_registration: bool = True

    # --- First-admin bootstrap (used by scripts/create_admin.py only) ---
    admin_email: str = ""
    admin_password: str = ""
    admin_full_name: str = "Administrator"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
