"""/template (Telegram) and /feed template (Discord) command surfaces.

Drives the real handlers with mocked service I/O (repo convention) and
pins: show/set/reset/all forms, multiline + \\n input normalization,
set-time placeholder validation, the conditional admin gate (bare show
stays open, mutations are gated), and the preview reply.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.adapters.discord.bot import FeedCommands
from newsflow.adapters.telegram.bot import template_command
from newsflow.services.subscription_service import SubscriptionActionResult


class _SessionCtx:
    def __init__(self):
        self.session = MagicMock()
        self.session.commit = AsyncMock()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


def _service(detail=None, result=None, count=0):
    service = MagicMock()
    service.get_subscription_detail = AsyncMock(return_value=detail)
    service.set_feed_template = AsyncMock(return_value=result)
    service.set_channel_template = AsyncMock(return_value=count)
    return service


def _detail(template=None, entries=(), translate=False, language="en"):
    detail = MagicMock()
    detail.subscription.message_template = template
    detail.subscription.translate = translate
    detail.subscription.target_language = language
    detail.recent_entries = list(entries)
    return detail


URL = "https://ex.com/feed"


# ---------------------------------------------------------------- telegram


def _tg_update(text: str):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = 777
    update.effective_chat.type = "private"
    update.effective_user.id = 42
    return update


async def _run_tg(text: str, service, gate=None):
    update = _tg_update(text)
    context = MagicMock()
    context.args = text.split()[1:]
    service_cls = MagicMock(return_value=service)
    service_cls.build_template_preview = MagicMock(return_value="PREVIEW-TEXT")
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "newsflow.adapters.telegram.bot.get_session_factory",
                return_value=lambda: _SessionCtx(),
            )
        )
        stack.enter_context(
            patch("newsflow.adapters.telegram.bot.SubscriptionService", service_cls)
        )
        if gate is not None:
            stack.enter_context(patch("newsflow.adapters.telegram.bot._require_group_admin", gate))
        await template_command(update, context)
    return update


def _reply_texts(update) -> list[str]:
    return [call.args[0] for call in update.message.reply_text.await_args_list]


async def test_tg_show_without_template_lists_placeholders():
    service = _service(detail=_detail(template=None))
    update = await _run_tg(f"/template {URL}", service)

    texts = _reply_texts(update)
    assert any("No template set" in t and "{title}" in t for t in texts)
    args = service.get_subscription_detail.await_args.kwargs
    assert args["platform"] == "telegram"
    assert args["channel_id"] == "777"
    assert args["feed_url"] == URL


async def test_tg_show_with_template_uses_pre_block():
    service = _service(detail=_detail(template="A\nB"))
    update = await _run_tg(f"/template {URL}", service)

    texts = _reply_texts(update)
    assert any("<pre>A\nB</pre>" in t for t in texts)


async def test_tg_set_multiline_keeps_newlines_and_previews():
    entry = MagicMock()
    service = _service(
        detail=_detail(entries=[entry]),
        result=SubscriptionActionResult(success=True, message="Template set for Example"),
    )
    update = await _run_tg(f"/template {URL} 📌 {{title}}\n{{summary}}", service)

    set_args = service.set_feed_template.await_args.args
    assert set_args == ("telegram", "777", URL, "📌 {title}\n{summary}")
    texts = _reply_texts(update)
    assert any("PREVIEW-TEXT" in t and "latest entry" in t for t in texts)


async def test_tg_set_backslash_n_is_normalized():
    service = _service(
        result=SubscriptionActionResult(success=True, message="Template set for Example"),
        detail=_detail(),
    )
    await _run_tg(f"/template {URL} {{title}}" + r"\n" + "{url}", service)

    stored = service.set_feed_template.await_args.args[3]
    assert stored == "{title}\n{url}"


async def test_tg_unknown_placeholder_rejected_before_storing():
    service = _service(detail=_detail())
    update = await _run_tg(f"/template {URL} {{tittle}}", service)

    service.set_feed_template.assert_not_awaited()
    texts = _reply_texts(update)
    assert any("unknown placeholder" in t and "{tittle}" in t for t in texts)


async def test_tg_reset_clears_template():
    service = _service(
        result=SubscriptionActionResult(success=True, message="Template cleared for Example")
    )
    update = await _run_tg(f"/template {URL} reset", service)

    assert service.set_feed_template.await_args.args == ("telegram", "777", URL, None)
    assert any("cleared" in t for t in _reply_texts(update))


async def test_tg_all_applies_to_channel():
    service = _service(count=3)
    update = await _run_tg("/template all {title}", service)

    assert service.set_channel_template.await_args.args == ("telegram", "777", "{title}")
    texts = _reply_texts(update)
    assert any("3 subscription(s)" in t for t in texts)
    assert any("sample data" in t for t in texts)


async def test_tg_mutations_gated_but_show_open():
    gate = AsyncMock(return_value=False)

    service = _service(detail=_detail(template=None))
    update = await _run_tg(f"/template {URL}", service, gate=gate)
    # Bare show: gate never consulted, reply still goes out.
    gate.assert_not_awaited()
    assert _reply_texts(update)

    service = _service()
    update = await _run_tg(f"/template {URL} {{title}}", service, gate=gate)
    gate.assert_awaited_once()
    service.set_feed_template.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()  # real gate sends its own denial


# ----------------------------------------------------------------- discord


def _interaction():
    interaction = MagicMock()
    interaction.channel_id = 555
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def _run_discord(service, *, url: str, template=None, reset=False):
    cog = FeedCommands(MagicMock())
    interaction = _interaction()
    service_cls = MagicMock(return_value=service)
    with (
        patch(
            "newsflow.adapters.discord.bot.get_session_factory",
            return_value=lambda: _SessionCtx(),
        ),
        patch("newsflow.adapters.discord.bot.SubscriptionService", service_cls),
    ):
        service_cls.build_template_preview = MagicMock(return_value="PREVIEW-TEXT")
        await FeedCommands.feed_template.callback(
            cog, interaction, url=url, template=template, reset=reset
        )
    return interaction


def _followup_texts(interaction) -> list[str]:
    return [call.args[0] for call in interaction.followup.send.await_args_list]


async def test_discord_set_and_preview():
    service = _service(
        detail=_detail(entries=[MagicMock()]),
        result=SubscriptionActionResult(success=True, message="Template set for Example"),
    )
    interaction = await _run_discord(service, url=URL, template=r"📌 **{title}**\n{url}")

    stored = service.set_feed_template.await_args.args[3]
    assert stored == "📌 **{title}**\n{url}"
    texts = _followup_texts(interaction)
    assert any("PREVIEW-TEXT" in t and "latest entry" in t for t in texts)
    assert interaction.followup.send.await_args.kwargs.get("ephemeral") is True


async def test_discord_unknown_placeholder_rejected():
    service = _service(detail=_detail())
    interaction = await _run_discord(service, url=URL, template="{tittle}")

    service.set_feed_template.assert_not_awaited()
    assert any("unknown placeholder" in t for t in _followup_texts(interaction))


async def test_discord_show_escapes_newlines_for_copy_paste():
    service = _service(detail=_detail(template="A\nB"))
    interaction = await _run_discord(service, url=URL)

    assert any("A\\nB" in t for t in _followup_texts(interaction))


async def test_discord_show_without_subscription():
    service = _service(detail=None)
    interaction = await _run_discord(service, url=URL)

    assert any("No subscription" in t for t in _followup_texts(interaction))


async def test_discord_reset_all_clears_channel():
    service = _service(count=2)
    interaction = await _run_discord(service, url="all", reset=True)

    assert service.set_channel_template.await_args.args == ("discord", "555", None)
    assert any("2 subscription(s)" in t for t in _followup_texts(interaction))
