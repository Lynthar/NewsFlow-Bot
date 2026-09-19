"""Discord digest sends and the DIGEST_MENTION_ON_DELIVERY allowance.

The client-wide baseline is AllowedMentions.none(), which used to neuter
the digest header's @here too — the feature rendered literal text and
notified nobody. Digest sends now pass an explicit everyone-allowance when
the feature is on (safe: apply_digest_header neutralized body mentions
first), and inherit the none() baseline when it's off.
"""

from unittest.mock import AsyncMock, MagicMock

import discord

from newsflow.adapters.discord.bot import DiscordAdapter


def _adapter() -> tuple[DiscordAdapter, MagicMock]:
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=MagicMock(id=99, pin=AsyncMock()))
    adapter.bot = MagicMock()
    adapter.bot.get_channel = MagicMock(return_value=channel)
    return adapter, channel


async def test_digest_send_allows_everyone_when_mention_on(configure):
    configure(digest_mention_on_delivery=True)
    adapter, channel = _adapter()

    ok = await adapter.send_digest_text("42", "@here 📰 **Digest**\n\nbody")

    assert ok is True
    allowed = channel.send.await_args.kwargs["allowed_mentions"]
    assert allowed.everyone is True


async def test_digest_send_inherits_none_when_mention_off(configure):
    """Feature off → no explicit allowance → the client-wide
    AllowedMentions.none() baseline keeps everything inert."""
    configure(digest_mention_on_delivery=False)
    adapter, channel = _adapter()

    ok = await adapter.send_digest_text("42", "body")

    assert ok is True
    assert "allowed_mentions" not in channel.send.await_args.kwargs


async def test_digest_pinned_send_carries_the_same_allowance(configure):
    configure(digest_mention_on_delivery=True, digest_auto_pin=True)
    adapter, channel = _adapter()

    sent, pin_id = await adapter.send_digest_text_pinned("42", "@here digest")

    assert (sent, pin_id) == (True, "99")
    allowed = channel.send.await_args.kwargs["allowed_mentions"]
    assert allowed.everyone is True


async def test_plain_send_text_never_gets_the_allowance(configure):
    """Non-digest system notices (feed-deactivation etc.) must stay on
    the none() baseline regardless of the digest setting."""
    configure(digest_mention_on_delivery=True)
    adapter, channel = _adapter()

    ok = await adapter.send_text("42", "@everyone hi")

    assert ok is True
    assert "allowed_mentions" not in channel.send.await_args.kwargs
