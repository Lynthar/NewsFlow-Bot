"""
Configuration management for NewsFlow Bot.

Self-hosted mode: Only need DISCORD_TOKEN or TELEGRAM_TOKEN to start.
All other settings have sensible defaults.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
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
    openai_model: str = "gpt-5.4-nano"
    openai_base_url: str | None = None  # For compatible APIs
    # Override the built-in OpenAI translation system prompt. Supports
    # {source_desc} and {target_name} placeholders. None → use default.
    translation_system_prompt: str | None = None

    # Scheduling
    fetch_interval_minutes: int = 60
    cleanup_interval_hours: int = 24
    entry_retention_days: int = 7

    # Cache (memory by default, Redis optional)
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None
    translation_cache_ttl_days: int = 7

    # Digest (LLM-generated daily / weekly summaries)
    digest_provider: Literal["openai"] = "openai"
    digest_model: str = "gpt-5.4-mini"
    digest_max_articles: int = 50
    digest_max_input_chars_per_article: int = 300
    digest_check_interval_minutes: int = 5
    # Override the built-in digest system prompt. Supports {window} and
    # {lang} placeholders. None → use default.
    digest_system_prompt: str | None = None

    # API service (disabled by default)
    api_enabled: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Webhook adapter: enabled whenever the referenced YAML file exists.
    # The file is both the source-of-truth (declarative — edit and restart)
    # and the on/off switch: remove it to disable webhook delivery entirely.
    webhooks_config_path: Path = Path("./data/webhooks.yaml")

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    # ===== Hosting service extensions (self-hosted users can ignore) =====

    # Multi-tenant mode
    multi_tenant: bool = False

    # Quota limits (0 = unlimited)
    max_feeds_per_channel: int = 0
    max_entries_per_feed: int = 0

    # Admin user IDs (platform-specific). default_factory avoids the
    # mutable-default warning pydantic v2 raises on bare `[]`.
    admin_user_ids: list[str] = Field(default_factory=list)

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
    def webhooks_enabled(self) -> bool:
        """Webhook adapter is enabled iff a config file actually exists at
        the configured path. Presence of the file is the opt-in — users who
        don't want webhook delivery just don't create the file."""
        try:
            return self.webhooks_config_path.is_file()
        except OSError:
            return False

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
