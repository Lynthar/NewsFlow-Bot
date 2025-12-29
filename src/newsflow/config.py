"""
Configuration management for NewsFlow Bot.

Self-hosted mode: Only need DISCORD_TOKEN or TELEGRAM_TOKEN to start.
All other settings have sensible defaults.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings with self-hosted friendly defaults.

    Minimal configuration example:
        DISCORD_TOKEN=your_token

    That's it! Everything else has sensible defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ===== Required (at least one) =====
    discord_token: str | None = None
    telegram_token: str | None = None

    # ===== Optional with sensible defaults =====

    # Database (SQLite by default, zero config)
    database_url: str = "sqlite+aiosqlite:///./data/newsflow.db"

    # Translation (disabled by default)
    translation_enabled: bool = False
    translation_provider: Literal["google", "deepl", "openai"] = "deepl"
    google_credentials_path: str | None = None
    google_project_id: str | None = None
    deepl_api_key: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str | None = None  # For compatible APIs

    # Scheduling
    fetch_interval_minutes: int = 60
    cleanup_interval_hours: int = 24
    entry_retention_days: int = 7

    # Cache (memory by default, Redis optional)
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None
    translation_cache_ttl_days: int = 7

    # API service (disabled by default)
    api_enabled: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    # ===== Hosting service extensions (self-hosted users can ignore) =====

    # Multi-tenant mode
    multi_tenant: bool = False

    # Quota limits (0 = unlimited)
    max_feeds_per_channel: int = 0
    max_entries_per_feed: int = 0

    # Admin user IDs (platform-specific)
    admin_user_ids: list[str] = []

    # ===== Derived properties =====

    @property
    def discord_enabled(self) -> bool:
        """Check if Discord is enabled (token provided)."""
        return bool(self.discord_token)

    @property
    def telegram_enabled(self) -> bool:
        """Check if Telegram is enabled (token provided)."""
        return bool(self.telegram_token)

    @property
    def data_dir(self) -> Path:
        """Get data directory from database URL."""
        if self.database_url.startswith("sqlite"):
            # Extract path from sqlite URL
            db_path = self.database_url.split("///")[-1]
            return Path(db_path).parent
        return Path("./data")

    # ===== Validators =====

    @field_validator("fetch_interval_minutes")
    @classmethod
    def validate_fetch_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("fetch_interval_minutes must be at least 1")
        return v

    @field_validator("entry_retention_days")
    @classmethod
    def validate_retention_days(cls, v: int) -> int:
        if v < 1:
            raise ValueError("entry_retention_days must be at least 1")
        return v

    def validate_minimal_config(self) -> bool:
        """Validate that at least one platform token is provided."""
        return bool(self.discord_token or self.telegram_token)

    def get_translation_api_key(self) -> str | None:
        """Get the API key for the configured translation provider."""
        if self.translation_provider == "google":
            return self.google_credentials_path
        elif self.translation_provider == "deepl":
            return self.deepl_api_key
        elif self.translation_provider == "openai":
            return self.openai_api_key
        return None

    def can_translate(self) -> bool:
        """Check if translation is properly configured."""
        if not self.translation_enabled:
            return False
        return bool(self.get_translation_api_key())


@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Usage:
        from newsflow.config import get_settings
        settings = get_settings()
    """
    return Settings()


# Convenience export
settings = get_settings()
