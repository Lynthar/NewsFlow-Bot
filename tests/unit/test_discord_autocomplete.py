"""Discord /feed URL autocomplete: wiring, suggestion source, and limits.

Drives the shared `_url_autocomplete` callback with mocked service I/O —
no real bot or gateway. The wiring tests walk the actual Command objects
so a dropped decorator (or a future param rename) fails loudly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.adapters.discord.bot import (
    AUTOCOMPLETE_MAX_CHOICES,
    AUTOCOMPLETE_MAX_LEN,
    FeedCommands,
)

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


class _SessionCtx:
    def __init__(self) -> None:
        self.session = MagicMock()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


def _mock_sub(i: int = 0, *, title=None, url=None, active: bool = True):
    feed = MagicMock()
    feed.title = f"Feed {i}" if title is None else title
    feed.url = f"https://ex.com/{i}" if url is None else url
    sub = MagicMock()
    sub.feed = feed
    sub.is_active = active
    return sub


def _interaction(command_name: str = "remove", channel_id: int = 123):
    interaction = MagicMock()
    interaction.channel_id = channel_id
    interaction.command = SimpleNamespace(name=command_name)
    return interaction


def _patch_subs(subs):
    service = MagicMock()
    service.get_channel_subscriptions = AsyncMock(return_value=subs)
    patches = (
        patch(
            "newsflow.adapters.discord.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch("newsflow.adapters.discord.bot.SubscriptionService", return_value=service),
    )
    return patches, service


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


async def test_suggests_stored_urls_for_channel_subs():
    (p1, p2), service = _patch_subs([_mock_sub(i) for i in range(3)])
    with p1, p2:
        choices = await _cog()._url_autocomplete(_interaction(), "")
    assert [c.value for c in choices] == [f"https://ex.com/{i}" for i in range(3)]
    assert all(f"Feed {i}" in choices[i].name for i in range(3))
    # Paused subs must stay suggestable or /feed resume can't find them.
    service.get_channel_subscriptions.assert_awaited_once_with(
        platform="discord",
        channel_id="123",
        include_inactive=True,
    )


async def test_filters_by_substring_of_title_or_url_case_insensitive():
    subs = [
        _mock_sub(title="Hacker News", url="https://hnrss.org/frontpage"),
        _mock_sub(title="Ars Technica", url="https://feeds.arstechnica.com/arstechnica/index"),
    ]
    (p1, p2), _ = _patch_subs(subs)
    with p1, p2:
        by_title = await _cog()._url_autocomplete(_interaction(), "HACKER")
        by_url = await _cog()._url_autocomplete(_interaction(), "arstechnica")
    assert [c.value for c in by_title] == ["https://hnrss.org/frontpage"]
    assert [c.value for c in by_url] == ["https://feeds.arstechnica.com/arstechnica/index"]


# --- Discord API limits --------------------------------------------------------


async def test_caps_at_discord_choice_limit():
    (p1, p2), _ = _patch_subs([_mock_sub(i) for i in range(AUTOCOMPLETE_MAX_CHOICES + 5)])
    with p1, p2:
        choices = await _cog()._url_autocomplete(_interaction(), "")
    assert len(choices) == AUTOCOMPLETE_MAX_CHOICES


async def test_skips_urls_too_long_for_a_choice_value():
    long_url = "https://ex.com/" + "a" * AUTOCOMPLETE_MAX_LEN
    (p1, p2), _ = _patch_subs(
        [
            _mock_sub(title="Long", url=long_url),
            _mock_sub(title="Ok", url="https://ok.example"),
        ]
    )
    with p1, p2:
        choices = await _cog()._url_autocomplete(_interaction(), "")
    assert [c.value for c in choices] == ["https://ok.example"]


async def test_truncates_choice_name_to_100_chars():
    (p1, p2), _ = _patch_subs([_mock_sub(title="T" * 150)])
    with p1, p2:
        choices = await _cog()._url_autocomplete(_interaction(), "")
    assert len(choices) == 1
    assert len(choices[0].name) == AUTOCOMPLETE_MAX_LEN
    assert choices[0].name.endswith("…")
    assert choices[0].value == "https://ex.com/0"


# --- command-aware filtering ---------------------------------------------------


async def test_pause_suggests_only_active_subs():
    subs = [_mock_sub(0, active=True), _mock_sub(1, active=False)]
    (p1, p2), _ = _patch_subs(subs)
    with p1, p2:
        choices = await _cog()._url_autocomplete(_interaction("pause"), "")
    assert [c.value for c in choices] == ["https://ex.com/0"]


async def test_resume_suggests_only_paused_subs_plus_all():
    subs = [_mock_sub(0, active=True), _mock_sub(1, active=False)]
    (p1, p2), _ = _patch_subs(subs)
    with p1, p2:
        choices = await _cog()._url_autocomplete(_interaction("resume"), "")
    assert [c.value for c in choices] == ["all", "https://ex.com/1"]


async def test_resume_offers_no_all_when_nothing_paused():
    (p1, p2), _ = _patch_subs([_mock_sub(0, active=True)])
    with p1, p2:
        choices = await _cog()._url_autocomplete(_interaction("resume"), "")
    assert choices == []


async def test_other_commands_suggest_paused_and_active_alike():
    subs = [_mock_sub(0, active=True), _mock_sub(1, active=False)]
    (p1, p2), _ = _patch_subs(subs)
    with p1, p2:
        choices = await _cog()._url_autocomplete(_interaction("status"), "")
    assert [c.value for c in choices] == ["https://ex.com/0", "https://ex.com/1"]


# --- degradation ----------------------------------------------------------------


async def test_failure_degrades_to_empty_suggestions():
    with patch(
        "newsflow.adapters.discord.bot.get_session_factory",
        side_effect=RuntimeError("db down"),
    ):
        choices = await _cog()._url_autocomplete(_interaction(), "x")
    assert choices == []
