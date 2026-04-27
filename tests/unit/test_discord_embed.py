"""Tests for Discord embed construction.

The Discord adapter pulls FeedEntry.published_at out of SQLite, which —
even on DateTime(timezone=True) columns — drops tzinfo on read with the
aiosqlite driver. discord.py's Embed.timestamp setter calls
.astimezone() on naive values, which interprets them as the host's
local time. On a non-UTC host that shifts the displayed timestamp by
the host offset (e.g., a Windows dev box on CET would show a +2h
skew). _create_embed must coerce naive values to UTC before handing
them to discord.py.
"""

from datetime import datetime, timezone

from newsflow.adapters.base import Message
from newsflow.adapters.discord.bot import DiscordAdapter


def _msg(published_at):
    return Message(
        title="t", summary="s", link="https://x.test/a",
        source="x", published_at=published_at, image_url=None,
    )


def test_create_embed_coerces_naive_published_at_to_utc():
    naive = datetime(2026, 4, 24, 12, 0, 0)  # no tzinfo — simulates DB read-back
    # _create_embed doesn't actually use `self`; bypass DiscordAdapter
    # construction (which spins up a discord.py Bot) by passing None.
    embed = DiscordAdapter._create_embed(None, _msg(naive))
    assert embed.timestamp is not None
    assert embed.timestamp.tzinfo is timezone.utc
    # And the wall-clock time is preserved (we treat naive as UTC, not
    # as host-local — astimezone() would have shifted it).
    assert embed.timestamp.replace(tzinfo=None) == naive


def test_create_embed_passes_through_aware_published_at():
    aware = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
    embed = DiscordAdapter._create_embed(None, _msg(aware))
    assert embed.timestamp == aware


def test_create_embed_falls_back_to_now_when_published_at_missing():
    embed = DiscordAdapter._create_embed(None, _msg(None))
    assert embed.timestamp is not None
    assert embed.timestamp.tzinfo is not None
