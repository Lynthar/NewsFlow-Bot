"""Tests for Dispatcher.apply_digest_header.

This is the shim shared between the scheduled digest loop
(`_tick_digests`) and the manual `/digest now` handlers in both
Discord and Telegram adapters — before it existed, the mention
prefix only fired on the scheduled path, so users testing via
`/digest now` saw no prefix and thought the feature was broken.
"""

from unittest.mock import MagicMock, patch

from newsflow.services.dispatcher import Dispatcher


def _make_dispatcher(mention_on: bool) -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.webhooks_enabled = False
    fake.fetch_interval_minutes = 60
    fake.digest_mention_on_delivery = mention_on
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        return Dispatcher()


def test_disabled_returns_text_unchanged():
    d = _make_dispatcher(mention_on=False)
    assert d.apply_digest_header("hello", "discord") == "hello"
    assert d.apply_digest_header("hello", "telegram") == "hello"
    assert d.apply_digest_header("hello", "webhook") == "hello"


def test_enabled_adds_at_here_on_discord():
    d = _make_dispatcher(mention_on=True)
    out = d.apply_digest_header("body", "discord")
    assert out.startswith("@here 📰 **Digest**")
    assert out.endswith("\n\nbody")


def test_enabled_adds_header_without_at_here_on_telegram():
    """Telegram groups notify by default; adding @here (which
    isn't a real Telegram thing) would just show as literal text."""
    d = _make_dispatcher(mention_on=True)
    out = d.apply_digest_header("body", "telegram")
    assert "📰 **Digest**" in out
    assert "@here" not in out


def test_enabled_same_behavior_on_webhook_as_telegram():
    """Webhooks get the visible header but no platform-specific
    mention token — they're machine endpoints, not human channels."""
    d = _make_dispatcher(mention_on=True)
    out = d.apply_digest_header("body", "webhook")
    assert "📰 **Digest**" in out
    assert "@here" not in out
