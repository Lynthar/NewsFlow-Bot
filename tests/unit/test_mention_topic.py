"""Per-feed mentions (Discord) and forum-topic delivery (Telegram).

Pins the wave-2 delivery-targeting semantics: the dispatcher fills
Message.mention/thread_id from the subscription; Discord prefixes the
mention (unless the template placed {mention}) and whitelists exactly
that target while the client-wide AllowedMentions baseline is none();
Telegram sends into the recorded forum topic, maps "message thread not
found" to TopicGoneError, and the dispatcher self-heals by clearing the
thread; /add records the topic it ran in; /settopic retargets.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from sqlalchemy import select

from newsflow.adapters.base import Message, TopicGoneError
from newsflow.adapters.discord.bot import (
    DiscordAdapter,
    FeedCommands,
    NewsFlowBot,
    _mention_allowance,
)
from newsflow.adapters.telegram.bot import (
    TelegramAdapter,
    _admin_cache,
    add_command,
    settopic_command,
)
from newsflow.core.feed_fetcher import FetchResult
from newsflow.core.message_template import render_template
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.dispatcher import Dispatcher
from newsflow.services.subscription_service import SubscriptionService
from tests import seed

URL = "https://ex.com/feed"


# ------------------------------------------------------------ core values


def test_mention_placeholder_renders():
    message = Message(title="T", summary="S", link="https://x/a", source="x", mention="<@&5>")
    out = render_template("{mention} {title}", message.to_template_values())
    assert out == "<@&5> T"


def test_mention_placeholder_empty_line_collapses():
    message = Message(title="T", summary="S", link="https://x/a", source="x", mention=None)
    out = render_template("{mention}\n{title}", message.to_template_values())
    assert out == "T"


# ------------------------------------------------------------- dispatcher


async def _feed_with_entries(session, count: int = 1) -> tuple[Feed, list[FeedEntry]]:
    feed = Feed(url=URL, title="Example", is_active=True, error_count=0)
    session.add(feed)
    await session.flush()
    entries = []
    base = datetime.now(UTC) - timedelta(minutes=30)
    for i in range(count):
        entry = FeedEntry(
            feed_id=feed.id,
            guid=f"e{i}",
            title=f"News {i}",
            summary="Summary",
            content=None,
            link=f"https://ex.com/{i}",
            published_at=base + timedelta(minutes=i),
        )
        session.add(entry)
        entries.append(entry)
    await session.commit()
    return feed, entries


def _sub(feed: Feed, **overrides) -> Subscription:
    defaults = dict(
        platform="telegram",
        platform_user_id="u",
        platform_channel_id="c",
        feed_id=feed.id,
        is_active=True,
        translate=False,
        target_language="en",
    )
    defaults.update(overrides)
    return Subscription(**defaults)


def _dispatcher() -> Dispatcher:
    return Dispatcher()


async def test_dispatcher_fills_mention_and_thread(session):
    feed, entries = await _feed_with_entries(session)
    sub = _sub(feed, mention="<@&9>", message_thread_id=77)
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entries[0], sub, session)

    assert message.mention == "<@&9>"
    assert message.thread_id == 77


async def test_template_pretrim_receives_mention(session):
    feed, entries = await _feed_with_entries(session)
    sub = _sub(feed, mention="<@&9>", message_template="{mention}|{title}")
    session.add(sub)
    await session.commit()

    message = await Dispatcher()._create_message(entries[0], sub, session)

    assert message.template_text == "<@&9>|News 0"


async def test_topic_gone_self_heals_and_batch_continues(session):
    d = _dispatcher()
    feed, entries = await _feed_with_entries(session, count=2)
    sub = _sub(feed, message_thread_id=77)
    session.add(sub)
    await session.commit()

    sent_messages: list[Message] = []

    async def fake_send(channel_id: str, message: Message) -> bool:
        sent_messages.append(message)
        if message.thread_id is not None:
            raise TopicGoneError(channel_id, message.thread_id, reason="thread not found")
        return True

    adapter = MagicMock()
    adapter.send_message = AsyncMock(side_effect=fake_send)
    adapter.is_connected = MagicMock(return_value=True)
    d._adapters["telegram"] = adapter

    sub_repo = SubscriptionRepository(session)
    sent = await d._dispatch_to_subscription(session, sub, sub_repo)
    await session.commit()

    # Entry 0 hit the dead topic and stays unsent; the heal cleared the
    # thread so entry 1 delivered to the default view in the same batch.
    assert sent == 1
    assert sub.message_thread_id is None
    assert sent_messages[0].thread_id == 77
    assert sent_messages[1].thread_id is None
    sent_rows = (await session.execute(select(SentEntry))).scalars().all()
    assert [row.guid for row in sent_rows] == ["e1"]


# -------------------------------------------------------- discord adapter


def _msg(**overrides) -> Message:
    fields: dict = dict(title="T", summary="S", link="https://x.test/a", source="x.test")
    fields.update(overrides)
    return Message(**fields)


def _discord_adapter() -> tuple[DiscordAdapter, MagicMock]:
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    adapter.bot = MagicMock()
    adapter.bot.get_channel = MagicMock(return_value=channel)
    return adapter, channel


def test_mention_allowance_shapes():
    role = _mention_allowance("<@&123>")
    assert role.everyone is False and role.users is False
    assert [o.id for o in role.roles] == [123]

    user = _mention_allowance("<@456>")
    assert user.everyone is False and user.roles is False
    assert [o.id for o in user.users] == [456]

    legacy = _mention_allowance("<@!789>")
    assert [o.id for o in legacy.users] == [789]

    garbage = _mention_allowance("@everyone")
    assert garbage.everyone is False and garbage.users is False and garbage.roles is False


def test_newsflowbot_baseline_allows_no_pings():
    bot = NewsFlowBot()
    allowed = bot.allowed_mentions
    assert allowed is not None
    assert allowed.everyone is False and allowed.users is False and allowed.roles is False


async def test_discord_mention_rides_default_embed():
    adapter, channel = _discord_adapter()
    ok = await adapter.send_message("42", _msg(mention="<@&9>"))

    assert ok is True
    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == "<@&9>"
    assert kwargs["embed"].title == "T"
    assert kwargs["embed"].url == "https://x.test/a"
    assert [o.id for o in kwargs["allowed_mentions"].roles] == [9]


async def test_discord_no_mention_keeps_plain_embed_call():
    adapter, channel = _discord_adapter()
    await adapter.send_message("42", _msg())

    call = channel.send.await_args
    assert "content" not in call.kwargs
    assert "allowed_mentions" not in call.kwargs


async def test_discord_template_gets_mention_prefix():
    adapter, channel = _discord_adapter()
    await adapter.send_message("42", _msg(template_text="body", mention="<@7>"))

    call = channel.send.await_args
    assert call.args[0] == "<@7>\nbody"
    assert [o.id for o in call.kwargs["allowed_mentions"].users] == [7]


async def test_discord_template_with_placed_mention_not_prefixed():
    adapter, channel = _discord_adapter()
    await adapter.send_message("42", _msg(template_text="tail — <@7>", mention="<@7>"))

    assert channel.send.await_args.args[0] == "tail — <@7>"


async def test_discord_template_without_mention_pings_nothing():
    adapter, channel = _discord_adapter()
    await adapter.send_message("42", _msg(template_text="@everyone free nitro"))

    allowed = channel.send.await_args.kwargs["allowed_mentions"]
    assert allowed.everyone is False and allowed.users is False and allowed.roles is False


# ------------------------------------------------------- telegram adapter


def _tg_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(token="test-token")
    adapter.app = MagicMock()
    adapter.app.bot.send_message = AsyncMock()
    return adapter


async def test_telegram_default_layout_targets_thread():
    adapter = _tg_adapter()
    ok = await adapter.send_message("123", _msg(thread_id=77))

    assert ok is True
    assert adapter.app.bot.send_message.await_args.kwargs["message_thread_id"] == 77


async def test_telegram_template_targets_thread():
    adapter = _tg_adapter()
    await adapter.send_message("123", _msg(template_text="body", thread_id=77))

    assert adapter.app.bot.send_message.await_args.kwargs["message_thread_id"] == 77


async def test_telegram_thread_gone_maps_to_topic_gone():
    from telegram.error import BadRequest

    adapter = _tg_adapter()
    adapter.app.bot.send_message = AsyncMock(side_effect=BadRequest("Message thread not found"))

    try:
        await adapter.send_message("123", _msg(thread_id=77))
        raised = None
    except TopicGoneError as e:
        raised = e

    assert raised is not None
    assert raised.thread_id == 77
    assert raised.channel_id == "123"


async def test_telegram_thread_error_without_thread_is_plain_failure():
    from telegram.error import BadRequest

    adapter = _tg_adapter()
    adapter.app.bot.send_message = AsyncMock(side_effect=BadRequest("Message thread not found"))

    ok = await adapter.send_message("123", _msg())

    assert ok is False


# ------------------------------------------------- service / repo round-trip


async def test_get_or_create_records_thread(session):
    feed, _entries = await _feed_with_entries(session)
    repo = SubscriptionRepository(session)

    sub, created = await repo.get_or_create_subscription(
        platform="telegram",
        user_id="u",
        channel_id="c",
        feed_id=feed.id,
        message_thread_id=77,
    )
    await session.commit()

    assert created is True
    assert sub.message_thread_id == 77

    # Re-subscribing must not clobber the recorded topic.
    again, created = await repo.get_or_create_subscription(
        platform="telegram", user_id="u", channel_id="c", feed_id=feed.id
    )
    assert created is False
    assert again.message_thread_id == 77


async def test_mention_and_thread_service_roundtrip(session):
    feed, _entries = await _feed_with_entries(session)
    sub = _sub(feed)
    other = Subscription(
        platform="telegram",
        platform_user_id="u",
        platform_channel_id="other",
        feed_id=feed.id,
        is_active=True,
    )
    session.add_all([sub, other])
    await session.commit()

    service = SubscriptionService(session)

    result = await service.set_feed_mention("telegram", "c", URL, "<@&5>")
    assert result.success is True
    await session.refresh(sub)
    assert sub.mention == "<@&5>"

    count = await service.set_channel_thread("telegram", "c", 42)
    assert count == 1
    await session.refresh(sub)
    assert sub.message_thread_id == 42
    await session.refresh(other)
    assert other.message_thread_id is None

    count = await service.set_channel_mention("telegram", "c", None)
    assert count == 1
    await session.refresh(sub)
    assert sub.mention is None

    result = await service.set_feed_thread("telegram", "c", URL, None)
    assert result.success is True
    assert "General" in result.message
    await session.refresh(sub)
    assert sub.message_thread_id is None


# ------------------------------------------------------ telegram commands

CHAT = "777"


async def _tg_subs(db, count: int = 1, **fields):
    """`count` subscriptions in the group; the first one is on URL."""
    return [
        await seed.subscription(
            db, channel_id=CHAT, url=URL if i == 0 else f"https://ex.com/{i}", **fields
        )
        for i in range(count)
    ]


async def _thread_of(db, sub_id: int) -> int | None:
    row = await seed.subscription_row(db, sub_id)
    assert row is not None
    return row.message_thread_id


def _tg_update(text: str, *, topic: int | None):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.is_topic_message = topic is not None
    update.message.message_thread_id = topic
    update.message.sender_chat = None
    update.effective_chat.id = int(CHAT)
    update.effective_chat.type = "supergroup"
    update.effective_user.id = 42
    return update


def _tg_context(args: list[str], *, admin: bool) -> MagicMock:
    _admin_cache.clear()
    context = MagicMock()
    context.args = args
    status = "administrator" if admin else "member"
    context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=status))
    return context


async def _run_settopic(text: str, *, topic: int | None, admin: bool = True):
    update = _tg_update(text, topic=topic)
    await settopic_command(update, _tg_context(text.split()[1:], admin=admin))
    return update


def _replies(update) -> list[str]:
    return [c.args[0] for c in update.message.reply_text.await_args_list]


async def test_settopic_points_at_current_topic(db):
    (sub,) = await _tg_subs(db)
    await _run_settopic(f"/settopic {URL}", topic=77)
    assert await _thread_of(db, sub.id) == 77


async def test_settopic_all_and_clear(db):
    subs = await _tg_subs(db, 3)
    update = await _run_settopic("/settopic all", topic=77)
    assert [await _thread_of(db, s.id) for s in subs] == [77, 77, 77]
    assert any("3 subscription(s)" in t for t in _replies(update))

    await _run_settopic(f"/settopic {URL} clear", topic=77)
    assert await _thread_of(db, subs[0].id) is None
    assert await _thread_of(db, subs[1].id) == 77  # only the named feed was cleared


async def test_settopic_outside_topic_means_general(db):
    (sub,) = await _tg_subs(db, message_thread_id=77)
    await _run_settopic(f"/settopic {URL}", topic=None)

    assert await _thread_of(db, sub.id) is None


async def test_settopic_denied_without_admin(db):
    (sub,) = await _tg_subs(db)
    update = await _run_settopic(f"/settopic {URL}", topic=77, admin=False)

    assert await _thread_of(db, sub.id) is None
    assert any("group admins" in t for t in _replies(update))


async def test_add_records_topic_it_ran_in(db, monkeypatch):
    fetcher = MagicMock()
    fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url=URL,
            success=True,
            entries=[{"guid": "e1", "title": "E", "link": "https://ex.com/e1"}],
            feed_title="Example",
        )
    )
    monkeypatch.setattr("newsflow.services.feed_service.get_fetcher", lambda: fetcher)
    update = _tg_update(f"/add {URL}", topic=55)
    processing = MagicMock()
    processing.edit_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=processing)

    with patch("newsflow.adapters.telegram.bot.get_dispatcher", return_value=MagicMock()):
        await add_command(update, _tg_context([URL], admin=True))

    async with db() as session:
        sub = await session.scalar(
            select(Subscription).where(Subscription.platform_channel_id == CHAT)
        )
    assert sub is not None and sub.message_thread_id == 55


# ------------------------------------------------------- discord command

DISCORD_CHANNEL = "555"


def _interaction():
    interaction = MagicMock()
    interaction.channel_id = int(DISCORD_CHANNEL)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def _discord_subs(db, count: int = 1, **fields):
    return [
        await seed.subscription(
            db,
            platform="discord",
            channel_id=DISCORD_CHANNEL,
            url=URL if i == 0 else f"https://ex.com/{i}",
            **fields,
        )
        for i in range(count)
    ]


async def _mention_of(db, sub_id: int) -> str | None:
    row = await seed.subscription_row(db, sub_id)
    assert row is not None
    return row.mention


async def _run_feed_mention(*, url: str, target=None, clear: bool = False):
    cog = FeedCommands(MagicMock())
    interaction = _interaction()
    await FeedCommands.feed_mention.callback(cog, interaction, url=url, target=target, clear=clear)
    return interaction


def _followups(interaction) -> list[str]:
    return [c.args[0] for c in interaction.followup.send.await_args_list]


async def test_feed_mention_set_from_native_pick(db):
    (sub,) = await _discord_subs(db)
    target = MagicMock()
    target.mention = "<@&55>"
    interaction = await _run_feed_mention(url=URL, target=target)

    assert await _mention_of(db, sub.id) == "<@&55>"
    assert any("<@&55>" in t for t in _followups(interaction))


async def test_feed_mention_clear_all(db):
    subs = await _discord_subs(db, 2, mention="<@&1>")
    interaction = await _run_feed_mention(url="all", clear=True)

    assert [await _mention_of(db, s.id) for s in subs] == [None, None]
    assert any("2 subscription(s)" in t for t in _followups(interaction))


async def test_feed_mention_show_current(db):
    await _discord_subs(db, mention="<@7>")
    interaction = await _run_feed_mention(url=URL)

    assert any("<@7>" in t for t in _followups(interaction))
