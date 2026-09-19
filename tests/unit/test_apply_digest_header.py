"""Tests for Dispatcher.apply_digest_header.

This is the shim shared between the scheduled digest loop
(`_tick_digests`) and the manual `/digest now` handlers in both
Discord and Telegram adapters — before it existed, the mention
prefix only fired on the scheduled path, so users testing via
`/digest now` saw no prefix and thought the feature was broken.
"""

from newsflow.services.dispatcher import Dispatcher


def _make_dispatcher(configure, mention_on: bool) -> Dispatcher:
    configure(digest_mention_on_delivery=mention_on)
    return Dispatcher()


def test_disabled_returns_text_unchanged(configure):
    d = _make_dispatcher(configure, mention_on=False)
    assert d.apply_digest_header("hello", "discord") == "hello"
    assert d.apply_digest_header("hello", "telegram") == "hello"
    assert d.apply_digest_header("hello", "webhook") == "hello"


def test_enabled_adds_at_here_on_discord(configure):
    d = _make_dispatcher(configure, mention_on=True)
    out = d.apply_digest_header("body", "discord")
    assert out.startswith("@here 📰 **Digest**")
    assert out.endswith("\n\nbody")


def test_enabled_adds_header_without_at_here_on_telegram(configure):
    """Telegram groups notify by default; adding @here (which
    isn't a real Telegram thing) would just show as literal text."""
    d = _make_dispatcher(configure, mention_on=True)
    out = d.apply_digest_header("body", "telegram")
    assert "📰 **Digest**" in out
    assert "@here" not in out


def test_enabled_same_behavior_on_webhook_as_telegram(configure):
    """Webhooks get the visible header but no platform-specific
    mention token — they're machine endpoints, not human channels."""
    d = _make_dispatcher(configure, mention_on=True)
    out = d.apply_digest_header("body", "webhook")
    assert "📰 **Digest**" in out
    assert "@here" not in out


def test_discord_body_mass_mentions_are_neutralized(configure):
    """The Discord adapter sends digest text with everyone-mentions
    ALLOWED (so the code-added header can ping) — which is only safe
    because model-emitted @everyone/@here inside the LLM body get a
    zero-width space injected here first."""
    d = _make_dispatcher(configure, mention_on=True)
    out = d.apply_digest_header("hi @everyone and @here!", "discord")
    header, _, body = out.partition("\n\n")
    assert header.startswith("@here")  # the ONE live mention
    assert "@everyone" not in body
    assert "@here" not in body
    # Visible text is preserved — only a zero-width space was inserted.
    assert body.replace("​", "") == "hi @everyone and @here!"


def test_non_discord_body_left_untouched(configure):
    """Telegram/webhook have no Discord-style mass mentions; their body
    bytes must pass through unmodified (webhook consumers may hash them)."""
    d = _make_dispatcher(configure, mention_on=True)
    out = d.apply_digest_header("hi @everyone", "telegram")
    assert out.endswith("\n\nhi @everyone")
    assert "​" not in out
