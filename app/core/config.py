from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_prefix="JARVIS_",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "JARVIS"
    app_version: str = "1.0.0"
    environment: str = "development"
    api_host: str = "127.0.0.1"
    api_port: int = 8756
    api_token: str = ""
    database_url: str = Field(
        default="postgresql://postgres:Glad@localhost:5432/Jarvis",
        validation_alias=AliasChoices("DATABASE_URL", "JARVIS_DATABASE_URL"),
    )
    default_timezone: str = "UTC"
    log_level: str = "INFO"
    # "null" is the opaque origin Chromium sends for the packaged renderer
    # loaded from file://. Development uses the Vite origins.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,null"

    scheduler_enabled: bool = True
    reminder_tick_seconds: int = 15
    workflow_tick_seconds: int = 30

    ai_provider: str = Field(
        default="mock", validation_alias=AliasChoices("AI_PROVIDER", "JARVIS_AI_PROVIDER")
    )
    ai_model: str = Field(
        default="gpt-4o-mini", validation_alias=AliasChoices("AI_MODEL", "JARVIS_AI_MODEL")
    )
    ai_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("AI_BASE_URL", "JARVIS_AI_BASE_URL"),
    )
    ollama_base_url: str = "http://127.0.0.1:11434"
    ai_api_key: str = Field(
        default="", validation_alias=AliasChoices("AI_API_KEY", "JARVIS_AI_API_KEY")
    )
    ai_timeout_seconds: int = 60

    weather_enabled: bool = False
    weather_location: str = ""

    # ------------------------------------------------------------------ voice
    # Heavy voice dependencies are importable at runtime; the API degrades with
    # an explicit status instead of crashing when a component is unavailable.
    whisper_model: str = Field(
        default="base.en", validation_alias=AliasChoices("JARVIS_WHISPER_MODEL", "WHISPER_MODEL")
    )
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_language: str = "en"
    tts_voice: str = "en-GB-RyanNeural"
    tts_language: str = "en-GB"
    wake_model: str = "hey_jarvis"
    wake_threshold: float = 0.6
    wake_cooldown_seconds: float = 2.0
    max_audio_bytes: int = 25 * 1024 * 1024

    # ---------------------------------------------------------------- Spotify
    # Create an app at https://developer.spotify.com/dashboard, add the exact
    # redirect URI below, then put the client ID (and optionally the secret)
    # in the backend .env. Tokens never reach the renderer.
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8756/api/v1/spotify/callback"
    spotify_scopes: str = (
        "user-read-playback-state user-modify-playback-state "
        "user-read-currently-playing user-read-private user-read-email streaming"
    )

    # ------------------------------------------------------------- encryption
    data_dir: Path = BACKEND_ROOT / ".data"
    secrets_key: str = Field(
        default="", validation_alias=AliasChoices("JARVIS_SECRETS_KEY", "SECRETS_KEY")
    )

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        return url

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def spotify_scope_list(self) -> list[str]:
        return [scope for scope in self.spotify_scopes.split() if scope]

    @property
    def resolved_api_token(self) -> str:
        if self.api_token:
            return self.api_token
        if self.environment == "development":
            # Development fallback keeps the local API usable when Electron is
            # not managing the process (e.g. pytest or manual uvicorn).
            return os.environ.get("JARVIS_DEV_TOKEN", "jarvis-local-dev-token")
        return ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
