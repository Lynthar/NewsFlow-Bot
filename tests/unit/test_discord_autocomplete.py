"""Discord /feed URL autocomplete: wiring, suggestion source, and limits.

Drives the shared `_url_autocomplete` callback against the real database —
no real bot or gateway. The wiring tests walk the actual Command objects
so a dropped decorator (or a future param rename) fails loudly.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from newsflow.adapters.discord.bot import (
    AUTOCOMPLETE_MAX_CHOICES,
    AUTOCOMPLETE_MAX_LEN,
    FeedCommands,
)
from newsflow.models.base import close_db
from tests import seed

AUTOCOMPLETED = (
    "feed_remove",
    "feed_pause",
    "feed_resume",
    "feed_silent",
    "feed_display",
    "feed_status",
    "feed_language",
    "feed_translate",
    "feed_filter_set",
    "feed_filter_show",
    "feed_filter_clear",
)

# add/test take URLs that are new by nature; suggesting existing subs
# there would only ever suggest duplicates.
NOT_AUTOCOMPLETED = ("feed_add", "feed_test")

CHANNEL = "123"


async def _sub(db, i: int = 0, *, title=None, url=None, active: bool = True):
    return await seed.subscription(
        db,
        platform="discord",
        channel_id=CHANNEL,
        url=f"https://ex.com/{i}" if url is None else url,
        title=f"Feed {i}" if title is None else title,
        is_active=active,
    )


def _interaction(command_name: str = "remove"):
    interaction = MagicMock()
    interaction.channel_id = int(CHANNEL)
    interaction.command = SimpleNamespace(name=command_name)
    return interaction


def _cog() -> FeedCommands:
    return FeedCommands(MagicMock())


# --- wiring ------------------------------------------------------------------


def test_url_autocomplete_wired_on_every_managing_command():
    for attr in AUTOCOMPLETED:
        command = getattr(FeedCommands, attr)
        param = next(p for p in command.parameters if p.name == "url")
        assert param.autocomplete, f"{attr} lost its url autocomplete"


def test_add_and_test_deliberately_have_no_url_autocomplete():
    for attr in NOT_AUTOCOMPLETED:
        command = getattr(FeedCommands, attr)
        param = next(p for p in command.parameters if p.name == "url")
        assert not param.autocomplete


# --- suggestion source -------------------------------------------------------


async def test_suggests_stored_urls_for_channel_subs(db):
    for i in range(3):
        await _sub(db, i)
    choices = await _cog()._url_autocomplete(_interaction(), "")
    assert [c.value for c in choices] == [f"https://ex.com/{i}" for i in range(3)]
    assert all(f"Feed {i}" in choices[i].name for i in range(3))


async def test_filters_by_substring_of_title_or_url_case_insensitive(db):
    await _sub(db, title="Hacker News", url="https://hnrss.org/frontpage")
    await _sub(db, title="Ars Technica", url="https://feeds.arstechnica.com/arstechnica/index")
    by_title = await _cog()._url_autocomplete(_interaction(), "HACKER")
    by_url = await _cog()._url_autocomplete(_interaction(), "arstechnica")
    assert [c.value for c in by_title] == ["https://hnrss.org/frontpage"]
    assert [c.value for c in by_url] == ["https://feeds.arstechnica.com/arstechnica/index"]


# --- Discord API limits --------------------------------------------------------


async def test_caps_at_discord_choice_limit(db):
    for i in range(AUTOCOMPLETE_MAX_CHOICES + 5):
        await _sub(db, i)
    choices = await _cog()._url_autocomplete(_interaction(), "")
    assert len(choices) == AUTOCOMPLETE_MAX_CHOICES


async def test_skips_urls_too_long_for_a_choice_value(db):
    long_url = "https://ex.com/" + "a" * AUTOCOMPLETE_MAX_LEN
    await _sub(db, title="Long", url=long_url)
    await _sub(db, title="Ok", url="https://ok.example")
    choices = await _cog()._url_autocomplete(_interaction(), "")
    assert [c.value for c in choices] == ["https://ok.example"]


async def test_truncates_choice_name_to_100_chars(db):
    await _sub(db, title="T" * 150)
    choices = await _cog()._url_autocomplete(_interaction(), "")
    assert len(choices) == 1
    assert len(choices[0].name) == AUTOCOMPLETE_MAX_LEN
    assert choices[0].name.endswith("…")
    assert choices[0].value == "https://ex.com/0"


# --- command-aware filtering ---------------------------------------------------


async def test_pause_suggests_only_active_subs(db):
    await _sub(db, 0, active=True)
    await _sub(db, 1, active=False)
    choices = await _cog()._url_autocomplete(_interaction("pause"), "")
    assert [c.value for c in choices] == ["https://ex.com/0"]


async def test_resume_suggests_only_paused_subs_plus_all(db):
    await _sub(db, 0, active=True)
    await _sub(db, 1, active=False)
    choices = await _cog()._url_autocomplete(_interaction("resume"), "")
    assert [c.value for c in choices] == ["all", "https://ex.com/1"]


async def test_resume_offers_no_all_when_nothing_paused(db):
    await _sub(db, 0, active=True)
    choices = await _cog()._url_autocomplete(_interaction("resume"), "")
    assert choices == []


async def test_other_commands_suggest_paused_and_active_alike(db):
    await _sub(db, 0, active=True)
    await _sub(db, 1, active=False)
    choices = await _cog()._url_autocomplete(_interaction("status"), "")
    assert [c.value for c in choices] == ["https://ex.com/0", "https://ex.com/1"]


# --- degradation ----------------------------------------------------------------


async def test_failure_degrades_to_empty_suggestions(db, configure, tmp_path):
    # Point the database at a file that cannot be opened: the first query fails.
    await close_db()
    configure(database_url=f"sqlite+aiosqlite:///{tmp_path / 'missing' / 'x.db'}")
    choices = await _cog()._url_autocomplete(_interaction(), "x")
    assert choices == []
