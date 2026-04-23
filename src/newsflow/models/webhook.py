"""WebhookDestination: named HTTP endpoint that receives pushed feed entries.

A destination is owned declaratively by `webhooks.yaml` — the bot syncs this
table to the YAML on every startup. Subscriptions reference a destination by
its `name` via `Subscription.platform_channel_id` (with `platform="webhook"`),
so the URL can be rotated without touching the subscription rows.

`format` picks one of the converters in adapters/webhook/formats.py
(generic / slack / ntfy / …). `secret` is used for HMAC-SHA256 signing of the
payload when present — recipients with the same key can verify integrity.
"""

from sqlalchemy import JSON, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from newsflow.models.base import Base


class WebhookDestination(Base):
    __tablename__ = "webhook_destinations"

    # User-facing alias. Used as Subscription.platform_channel_id so the URL
    # itself doesn't leak into the subscription list or application logs.
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    # Delivery target.
    url: Mapped[str] = mapped_column(String(2048), nullable=False)

    # Payload converter. Defaults to the canonical generic JSON so arbitrary
    # downstream integrations (n8n, Zapier, self-written endpoints) work
    # without per-vendor handling.
    format: Mapped[str] = mapped_column(String(32), default="generic")

    # HMAC-SHA256 key. When set, we send an `X-NewsFlow-Signature: sha256=<hex>`
    # header computed over the exact bytes of the POST body, so recipients on
    # open endpoints can verify the sender.
    secret: Mapped[str | None] = mapped_column(String(256))

    # Extra headers merged into every request (e.g. Bearer tokens for Zapier
    # or X-* routing hints for n8n). JSON-serialised dict; SQLite stores
    # TEXT, Postgres stores JSONB.
    headers: Mapped[dict | None] = mapped_column(JSON)

    # Per-destination request timeout. Kept small so a hung webhook can't
    # stall the dispatch loop for all other platforms.
    timeout_s: Mapped[int] = mapped_column(Integer, default=10)

    __table_args__ = (
        Index("ix_webhook_destinations_name", "name", unique=True),
    )

    def __repr__(self) -> str:
        return f"<WebhookDestination(name='{self.name}', format='{self.format}')>"
