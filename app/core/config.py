"""Application settings. Everything is environment-driven — no literals in code."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    log_level: str = "INFO"

    # The business day is Indonesian. The server is authoritative on time, so
    # "today" for attendance and every report window is resolved in this zone,
    # never in UTC and never from the device clock.
    timezone: str = Field(default="Asia/Jakarta", alias="APP_TIMEZONE")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/api_fwa_sales_dev"
    db_echo: bool = False

    jwt_secret: str = "change-me-in-every-environment"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_days: int = 30

    service_api_key: str = "change-me-service-key"

    login_rate_limit_attempts: int = 10
    login_rate_limit_window_seconds: int = 300

    file_storage_dir: Path = Path("./var/uploads")
    file_url_secret: str = "change-me-file-signing-secret"
    file_url_ttl_seconds: int = 300
    max_upload_bytes: int = 5 * 1024 * 1024
    public_base_url: str = "http://localhost:8000"

    cors_origins: str = ""

    # A GPS fix reporting worse accuracy than this is stored but marked unverified.
    # This is a device-quality tolerance, not the CICO geofence radius — that one is
    # a business decision and is still open (CLAUDE.md §11.2).
    geo_accuracy_tolerance_m: float = 100.0

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises if the zone name is not installed
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
