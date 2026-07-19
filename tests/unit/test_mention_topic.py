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
from newsflow.adapters.telegram.bot import TelegramAdapter, add_command, settopic_command
from newsflow.core.message_template import render_template
from newsflow.models.feed import Feed, FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.repositories.subscription_repository import SubscriptionRepository
from newsflow.services.dispatcher import Dispatcher
from newsflow.services.subscription_service import (
    SubscriptionActionResult,
    SubscriptionService,
)

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
    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.webhooks_enabled = False
    fake.fetch_interval_minutes = 60
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
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
    assert kwargs["embed"].description == "[T](https://x.test/a)"
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


class _SessionCtx:
    def __init__(self):
        self.session = MagicMock()
        self.session.commit = AsyncMock()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


def _tg_service(result=None, count=0, subscribe_result=None):
    service = MagicMock()
    service.set_feed_thread = AsyncMock(return_value=result)
    service.set_channel_thread = AsyncMock(return_value=count)
    service.subscribe = AsyncMock(return_value=subscribe_result)
    return service


def _tg_update(text: str, *, topic: int | None):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.is_topic_message = topic is not None
    update.message.message_thread_id = topic
    update.effective_chat.id = 777
    update.effective_chat.type = "supergroup"
    update.effective_user.id = 42
    return update


async def _run_settopic(text: str, service, *, topic: int | None, admin: bool = True):
    update = _tg_update(text, topic=topic)
    context = MagicMock()
    context.args = text.split()[1:]
    with (
        patch(
            "newsflow.adapters.telegram.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch(
            "newsflow.adapters.telegram.bot.SubscriptionService",
            MagicMock(return_value=service),
        ),
        patch(
            "newsflow.adapters.telegram.bot._require_group_admin",
            AsyncMock(return_value=admin),
        ),
    ):
        await settopic_command(update, context)
    return update


async def test_settopic_points_at_current_topic():
    service = _tg_service(result=SubscriptionActionResult(success=True, message="ok"))
    await _run_settopic(f"/settopic {URL}", service, topic=77)

    assert service.set_feed_thread.await_args.args == ("telegram", "777", URL, 77)


async def test_settopic_all_and_clear():
    service = _tg_service(count=3)
    update = await _run_settopic("/settopic all", service, topic=77)
    assert service.set_channel_thread.await_args.args == ("telegram", "777", 77)
    assert any("3 subscription(s)" in c.args[0] for c in update.message.reply_text.await_args_list)

    service = _tg_service(result=SubscriptionActionResult(success=True, message="ok"))
    await _run_settopic(f"/settopic {URL} clear", service, topic=77)
    assert service.set_feed_thread.await_args.args == ("telegram", "777", URL, None)


async def test_settopic_outside_topic_means_general():
    service = _tg_service(result=SubscriptionActionResult(success=True, message="ok"))
    await _run_settopic(f"/settopic {URL}", service, topic=None)

    assert service.set_feed_thread.await_args.args == ("telegram", "777", URL, None)


async def test_settopic_denied_without_admin():
    service = _tg_service()
    await _run_settopic(f"/settopic {URL}", service, topic=77, admin=False)

    service.set_feed_thread.assert_not_awaited()
    service.set_channel_thread.assert_not_awaited()


async def test_add_records_topic_it_ran_in():
    from newsflow.services.subscription_service import SubscribeResult

    subscribe_result = SubscribeResult(success=False, message="nope")
    service = _tg_service(subscribe_result=subscribe_result)
    update = _tg_update(f"/add {URL}", topic=55)
    update.effective_chat.type = "supergroup"
    processing = MagicMock()
    processing.edit_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=processing)
    context = MagicMock()
    context.args = [URL]

    with (
        patch(
            "newsflow.adapters.telegram.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch(
            "newsflow.adapters.telegram.bot.SubscriptionService",
            MagicMock(return_value=service),
        ),
        patch(
            "newsflow.adapters.telegram.bot._require_group_admin",
            AsyncMock(return_value=True),
        ),
    ):
        await add_command(update, context)

    assert service.subscribe.await_args.kwargs["message_thread_id"] == 55


# ------------------------------------------------------- discord command


def _interaction():
    interaction = MagicMock()
    interaction.channel_id = 555
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _discord_service(detail=None, result=None, count=0):
    service = MagicMock()
    service.get_subscription_detail = AsyncMock(return_value=detail)
    service.set_feed_mention = AsyncMock(return_value=result)
    service.set_channel_mention = AsyncMock(return_value=count)
    return service


async def _run_feed_mention(service, *, url: str, target=None, clear: bool = False):
    cog = FeedCommands(MagicMock())
    interaction = _interaction()
    with (
        patch(
            "newsflow.adapters.discord.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch(
            "newsflow.adapters.discord.bot.SubscriptionService",
            MagicMock(return_value=service),
        ),
    ):
        await FeedCommands.feed_mention.callback(
            cog, interaction, url=url, target=target, clear=clear
        )
    return interaction


async def test_feed_mention_set_from_native_pick():
    target = MagicMock()
    target.mention = "<@&55>"
    service = _discord_service(
        result=SubscriptionActionResult(success=True, message="Mention set for Example")
    )
    interaction = await _run_feed_mention(service, url=URL, target=target)

    assert service.set_feed_mention.await_args.args == ("discord", "555", URL, "<@&55>")
    texts = [c.args[0] for c in interaction.followup.send.await_args_list]
    assert any("<@&55>" in t for t in texts)


async def test_feed_mention_clear_all():
    service = _discord_service(count=2)
    interaction = await _run_feed_mention(service, url="all", clear=True)

    assert service.set_channel_mention.await_args.args == ("discord", "555", None)
    texts = [c.args[0] for c in interaction.followup.send.await_args_list]
    assert any("2 subscription(s)" in t for t in texts)


async def test_feed_mention_show_current():
    detail = MagicMock()
    detail.subscription.mention = "<@7>"
    service = _discord_service(detail=detail)
    interaction = await _run_feed_mention(service, url=URL)

    texts = [c.args[0] for c in interaction.followup.send.await_args_list]
    assert any("<@7>" in t for t in texts)
