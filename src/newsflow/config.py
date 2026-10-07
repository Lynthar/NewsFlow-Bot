"""
Configuration management for NewsFlow Bot.

Self-hosted mode: Only need DISCORD_TOKEN or TELEGRAM_TOKEN to start.
All other settings have sensible defaults.
"""

import os
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from dotenv import dotenv_values
from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # Max feeds fetched concurrently per round — bounds the FeedFetcher
    # semaphore and the number of open HTTP connections. Raise for large
    # feed counts on a fast host; lower to ease memory / upstream rate limits.
    feed_max_concurrent: int = 10
    cleanup_interval_hours: int = 24
    # Must stay ABOVE the weekly digest's 7-day window: cleanup deletes FeedEntry by
    # created_at, so coinciding horizons silently drop the digest's oldest day.
    entry_retention_days: int = 10

    # Max published_at age still eligible for dispatch; 0 disables. NULL always passes.
    # Stops archive-re-serving feeds from pushing year-old articles.
    max_entry_publish_age_days: int = 14

    # Dedupe signal: "this channel already saw this (feed_id, guid)". Must outlive
    # entry_retention_days by a wide margin — dropping it while the feed still serves
    # the guid makes the dispatcher deliver the entry again.
    sent_entry_retention_days: int = 90

    # Cache (memory by default, Redis optional)
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None
    translation_cache_ttl_days: int = 7

    # Digest (LLM-generated daily / weekly summaries)
    digest_provider: Literal["openai"] = "openai"
    digest_model: str = "gpt-5.4-mini"
    # Per-article summary truncation length fed to the digest LLM prompt.
    # (The article *count* cap is per-channel: ChannelDigest.max_articles.)
    digest_max_input_chars_per_article: int = 300
    digest_check_interval_minutes: int = 5
    # Override the built-in digest system prompt. Supports {window} and
    # {lang} placeholders. None → use default.
    digest_system_prompt: str | None = None
    # Adds a visible header (plus `@here` on Discord) to digest deliveries.
    # Only the code-added header may ping: Dispatcher.apply_digest_header neutralizes
    # mass-mention tokens in the LLM body before the send allows @everyone/@here.
    digest_mention_on_delivery: bool = False
    # Pins each digest's first chunk and unpins the previous one. Pin failures degrade
    # gracefully (digest still delivers, old pin stays, warning logged); webhook no-ops.
    digest_auto_pin: bool = False

    # API service (disabled by default)
    api_enabled: bool = False
    # Loopback by default: GET endpoints expose feed URLs (often token-bearing) and
    # error details. The Docker image overrides to 0.0.0.0; the port mapping is the
    # boundary there.
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # Shared secret for API write endpoints (feed mutations + /api/ingest).
    # Empty = write access disabled (fail closed). Set via the API_KEY env var.
    # Once set, READ endpoints (except health probes) require it too.
    api_key: str = ""
    # Key for /api/ingest alone, for the systems that push entries from outside: a leak
    # there then grants no management access. Empty = API_KEY serves ingest too.
    ingest_api_key: str = ""
    # CORS allowlist for browser callers. Empty (default) = no CORS headers
    # at all; the old blanket allow_origins=["*"] is opt-in via
    # API_CORS_ORIGINS=* if someone truly wants it. Comma or JSON list.
    api_cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Webhook adapter: enabled whenever the referenced YAML file exists.
    # The file is both the source-of-truth (declarative — edit and restart)
    # and the on/off switch: remove it to disable webhook delivery entirely.
    webhooks_config_path: Path = Path("./data/webhooks.yaml")

    # Declarative non-RSS sources (JSON-API, IMAP email): the file existing turns the
    # sync on. Removing it only stops the sync — synced sources keep running; an
    # empty `sources: {}` is what retires them.
    sources_config_path: Path = Path("./data/sources.yaml")

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"
    # Deliberately decoupled from LOG_LEVEL: DEBUG-level app troubleshooting must not
    # dump bound SQL parameters (webhook tokens, HMAC secrets, stored API keys).
    db_echo: bool = False

    # ===== Permissions =====

    # Group owner/administrator gate for state-changing Telegram commands in groups;
    # private chats are never restricted. Discord uses native default_permissions.
    telegram_admin_only: bool = True

    # Numeric ids that always pass the Telegram group-admin gate; empty = no bypass.
    # NoDecode + the before-validator accept both `123,456` and a JSON list —
    # pydantic-settings' JSON-only decoding would crash startup on the comma form.
    admin_user_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ===== Hosting service extensions (self-hosted users can ignore) =====

    # Multi-tenant mode
    multi_tenant: bool = False

    # Quota limits (0 = unlimited)
    max_feeds_per_channel: int = 0

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
    def sources_enabled(self) -> bool:
        """Non-RSS source sync runs iff a sources.yaml exists at the configured
        path — same file-presence opt-in as webhooks."""
        try:
            return self.sources_config_path.is_file()
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

    @field_validator("admin_user_ids", "api_cors_origins", mode="before")
    @classmethod
    def parse_admin_user_ids(cls, v: object) -> object:
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json

                v = json.loads(s)
            else:
                return [part.strip() for part in s.split(",") if part.strip()]
        # Telegram ids are numbers, so a JSON list naturally holds ints.
        return [str(item) for item in v] if isinstance(v, list) else v

    @field_validator("feed_max_concurrent")
    @classmethod
    def validate_feed_max_concurrent(cls, v: int) -> int:
        if v < 1:
            # 0 would build an asyncio.Semaphore(0): every fetch blocks
            # forever while the bot looks alive — until the container
            # healthcheck finally trips hours later.
            raise ValueError("feed_max_concurrent must be at least 1")
        return v

    @field_validator("entry_retention_days")
    @classmethod
    def validate_retention_days(cls, v: int) -> int:
        if v < 1:
            raise ValueError("entry_retention_days must be at least 1")
        return v

    @field_validator(
        "cleanup_interval_hours",
        "digest_check_interval_minutes",
        "translation_cache_ttl_days",
        "digest_max_input_chars_per_article",
    )
    @classmethod
    def validate_positive_intervals(cls, v: int, info: ValidationInfo) -> int:
        # A zero/negative interval turns the corresponding sleep-loop into a
        # busy spin (asyncio.sleep(<=0) returns immediately); a non-positive
        # TTL/char budget silently disables the feature it configures.
        if v < 1:
            raise ValueError(f"{info.field_name} must be at least 1")
        return v

    @field_validator("api_port")
    @classmethod
    def validate_api_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("api_port must be a valid TCP port (1-65535)")
        return v

    @field_validator("max_feeds_per_channel")
    @classmethod
    def validate_max_feeds(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_feeds_per_channel must be >= 0 (0 = unlimited)")
        return v

    @field_validator("max_entry_publish_age_days")
    @classmethod
    def validate_max_publish_age(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_entry_publish_age_days must be >= 0 (0 disables the filter)")
        return v

    @field_validator("sent_entry_retention_days")
    @classmethod
    def validate_sent_entry_retention(cls, v: int) -> int:
        if v < 1:
            raise ValueError("sent_entry_retention_days must be at least 1")
        return v

    @model_validator(mode="after")
    def validate_retention_order(self) -> Self:
        if self.sent_entry_retention_days <= self.entry_retention_days:
            raise ValueError(
                "sent_entry_retention_days must be greater than entry_retention_days: "
                "cleanup would drop the record that an entry was sent while the entry "
                "is still there, and it would be delivered again"
            )
        return self

    def config_warnings(self) -> list[str]:
        """Settings that load but likely don't do what was meant. Startup logs these;
        checkconfig reports them. Neither refuses to run over one."""
        warnings = [
            f"{key} is not a setting and is ignored — did you mean {name}?"
            for key, name in self._misspelled_keys()
        ]
        if self.entry_retention_days <= 7:
            warnings.append(
                f"entry_retention_days={self.entry_retention_days} does not outlast the "
                "weekly digest's 7-day window; its oldest articles are cleaned up first"
            )
        age_gate = self.max_entry_publish_age_days
        if age_gate and self.sent_entry_retention_days <= age_gate:
            warnings.append(
                "sent_entry_retention_days does not exceed max_entry_publish_age_days: an "
                "entry a feed keeps serving is delivered again once its sent record expires"
            )
        if self.translation_enabled and not self.can_translate():
            warnings.append(
                f"translation_enabled but provider {self.translation_provider!r} "
                "has no API key — entries will be delivered untranslated"
            )
        if self.api_enabled and not self.api_key and not self.ingest_api_key:
            warnings.append(
                "api_enabled without API_KEY — write endpoints and /api/ingest "
                "are fail-closed (503), push sources won't work"
            )
        return warnings

    def _misspelled_keys(self) -> list[tuple[str, str]]:
        """(key, setting) for environment and .env keys one typo away from a setting.
        Unknown keys are ignored on load, and other programs' variables are legitimate,
        so only a near miss is worth reporting."""
        names = list(type(self).model_fields)
        keys = set(os.environ)
        env_file = self.model_config.get("env_file")
        if isinstance(env_file, str | Path) and Path(env_file).is_file():
            keys.update(dotenv_values(env_file))
        found = []
        for key in sorted(keys):
            if key.lower() in names:
                continue
            match = get_close_matches(key.lower(), names, n=1, cutoff=0.85)
            if match:
                found.append((key, match[0].upper()))
        return found

    def validate_minimal_config(self) -> bool:
        """At least one delivery platform must be configured. A chat-platform
        token counts, and so does a webhooks.yaml on disk — a headless
        RSS→webhook pipeline is a complete deployment on its own."""
        return bool(self.discord_token or self.telegram_token or self.webhooks_enabled)

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
