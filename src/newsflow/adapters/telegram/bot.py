"""
Telegram bot adapter.

Implements Telegram-specific functionality using python-telegram-bot.
"""

import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from newsflow.adapters.base import BaseAdapter, Message
from newsflow.config import get_settings
from newsflow.core.filter import parse_keyword_csv
from newsflow.core.timeutil import relative_time, time_until
from newsflow.models.base import get_session_factory
from newsflow.models.subscription import Subscription
from newsflow.services import SubscriptionService, get_dispatcher

LIST_PAGE_SIZE = 20

logger = logging.getLogger(__name__)

# Global adapter reference for command handlers
_adapter: "TelegramAdapter | None" = None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "🗞️ <b>Welcome to NewsFlow Bot!</b>\n\n"
        "I can send you updates from RSS feeds.\n\n"
        "<b>Feed management:</b>\n"
        "/add &lt;url&gt; — Subscribe\n"
        "/remove &lt;url&gt; — Unsubscribe\n"
        "/pause &lt;url&gt; — Temporarily stop delivery\n"
        "/resume &lt;url&gt; — Resume delivery\n"
        "/list [page] — List subscribed feeds\n"
        "/info &lt;url&gt; — Detailed status of one feed\n"
        "/test &lt;url&gt; — Check if a URL is a valid feed\n\n"
        "<b>OPML:</b>\n"
        "/export — Download subscriptions as OPML\n"
        "/import &lt;url&gt; — Import from a hosted OPML URL\n"
        "(or just upload an .opml file to this chat)\n\n"
        "<b>Settings (channel-wide):</b>\n"
        "/language &lt;code&gt; — Default translation language\n"
        "/translate &lt;on/off&gt; — Default translation toggle\n\n"
        "<b>Settings (per-feed overrides):</b>\n"
        "/setlang &lt;url&gt; &lt;code&gt; — Per-feed language\n"
        "/settrans &lt;url&gt; &lt;on/off&gt; — Per-feed translate\n"
        "/filter &lt;url&gt; [show | clear | include=a,b exclude=c] — Keyword filter\n\n"
        "<b>Other:</b>\n"
        "/status — Bot status\n"
        "/help — This message",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await start_command(update, context)


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /add command."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /add <rss_url>\n\n"
            "Example: /add https://example.com/feed.xml"
        )
        return

    url = context.args[0]
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    # Send processing message
    processing_msg = await update.message.reply_text("⏳ Adding feed...")

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)

        result = await service.subscribe(
            platform="telegram",
            user_id=user_id,
            channel_id=chat_id,
            feed_url=url,
        )

        await session.commit()

    # Deliver a preview entry in the background so the user sees content
    # without waiting a full fetch interval.
    if result.success and result.is_new and result.subscription:
        asyncio.create_task(
            get_dispatcher().schedule_preview(result.subscription.id)
        )

    if result.success:
        feed_title = result.feed.title or url
        if result.is_new:
            message = f"✅ <b>Feed Added</b>\n\n<b>{feed_title}</b>\n\n{url}"
        else:
            message = f"✅ <b>Feed Added</b>\n\n<b>{feed_title}</b>\n\n{result.message}"
    else:
        message = f"❌ <b>Failed to Add Feed</b>\n\n{result.message}\n\nURL: {url}"

    await processing_msg.edit_text(message, parse_mode="HTML")


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /remove command."""
    if not context.args:
        await update.message.reply_text("Usage: /remove <rss_url>")
        return

    url = context.args[0]
    chat_id = str(update.effective_chat.id)

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)

        result = await service.unsubscribe(
            platform="telegram",
            channel_id=chat_id,
            feed_url=url,
        )

        await session.commit()

    if result.success:
        message = f"✅ <b>Feed Removed</b>\n\n{result.message}"
    else:
        message = f"❌ <b>Failed to Remove Feed</b>\n\n{result.message}"

    await update.message.reply_text(message, parse_mode="HTML")


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _sub_status_chip(sub: Subscription) -> str | None:
    feed = sub.feed
    if not sub.is_active:
        return "⏸ paused"
    if not feed.is_active:
        return "🛑 auto-disabled"
    if feed.error_count > 0:
        return f"⚠️ {feed.error_count} errors, retry {time_until(feed.next_retry_at)}"
    return None


def _format_sub_line(sub: Subscription) -> str:
    feed = sub.feed
    title = _escape_html(feed.title or "Untitled")
    parts = [
        f"🌐 {sub.target_language}" if sub.translate else "📰 no translate"
    ]
    chip = _sub_status_chip(sub)
    if chip:
        parts.append(chip)
    meta = " · ".join(parts)
    return f"<b>{title}</b> · {meta}\n{_escape_html(feed.url)}"


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /list [page]. Paginated: LIST_PAGE_SIZE feeds per page."""
    chat_id = str(update.effective_chat.id)

    try:
        page = int(context.args[0]) if context.args else 1
    except (ValueError, IndexError):
        page = 1

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        subs = list(
            await service.get_channel_subscriptions(
                platform="telegram", channel_id=chat_id
            )
        )

    if not subs:
        await update.message.reply_text(
            "📭 <b>No feeds subscribed</b>\n\n"
            "Use /add &lt;url&gt; to subscribe to an RSS feed.",
            parse_mode="HTML",
        )
        return

    total = len(subs)
    total_pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * LIST_PAGE_SIZE
    page_subs = subs[start : start + LIST_PAGE_SIZE]

    header = f"📰 <b>Subscribed Feeds ({total})</b>"
    if total_pages > 1:
        header += f" — page {page}/{total_pages}"

    body = "\n\n".join(_format_sub_line(s) for s in page_subs)

    footer = ""
    if page < total_pages:
        footer = f"\n\n<i>Use /list {page + 1} for the next page.</i>"

    await update.message.reply_text(
        header + "\n\n" + body + footer,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pause <url>."""
    if not context.args:
        await update.message.reply_text("Usage: /pause <rss_url>")
        return
    url = context.args[0]
    chat_id = str(update.effective_chat.id)

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.pause_subscription(
            platform="telegram", channel_id=chat_id, feed_url=url
        )
        await session.commit()

    prefix = "⏸" if result.success else "❌"
    await update.message.reply_text(
        f"{prefix} {_escape_html(result.message)}", parse_mode="HTML"
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume <url>."""
    if not context.args:
        await update.message.reply_text("Usage: /resume <rss_url>")
        return
    url = context.args[0]
    chat_id = str(update.effective_chat.id)

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.resume_subscription(
            platform="telegram", channel_id=chat_id, feed_url=url
        )
        await session.commit()

    prefix = "▶️" if result.success else "❌"
    await update.message.reply_text(
        f"{prefix} {_escape_html(result.message)}", parse_mode="HTML"
    )


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /info <url> — detailed status of one subscribed feed."""
    if not context.args:
        await update.message.reply_text("Usage: /info <rss_url>")
        return
    url = context.args[0]
    chat_id = str(update.effective_chat.id)

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        detail = await service.get_subscription_detail(
            platform="telegram", channel_id=chat_id, feed_url=url
        )

    if detail is None:
        await update.message.reply_text(
            f"⚠️ No subscription to <code>{_escape_html(url)}</code> in this chat.",
            parse_mode="HTML",
        )
        return

    sub = detail.subscription
    feed = detail.feed
    if not sub.is_active:
        state = "⏸ Paused"
    elif not feed.is_active:
        state = "🛑 Auto-disabled (10+ consecutive errors)"
    elif feed.error_count > 0:
        state = (
            f"⚠️ {feed.error_count} errors — retry "
            f"{time_until(feed.next_retry_at)}"
        )
    else:
        state = "✅ Healthy"

    lines = [
        f"📊 <b>{_escape_html(feed.title or 'Untitled Feed')}</b>",
        f"🔗 {_escape_html(feed.url)}",
        "",
        f"<b>State:</b> {state}",
        f"<b>Translation:</b> "
        f"{'On' if sub.translate else 'Off'} ({sub.target_language})",
        f"<b>Last OK fetch:</b> {relative_time(feed.last_successful_fetch_at)}",
        f"<b>Last attempt:</b> {relative_time(feed.last_fetched_at)}",
    ]
    if feed.last_error and feed.error_count > 0:
        err = feed.last_error
        if len(err) > 200:
            err = err[:200] + "…"
        lines.append(f"<b>Last error:</b> {_escape_html(err)}")

    if detail.recent_entries:
        lines.append("")
        lines.append("<b>Recent articles:</b>")
        for entry in detail.recent_entries:
            ts = (
                relative_time(entry.published_at)
                if entry.published_at
                else ""
            )
            title_line = entry.title[:80] + (
                "…" if len(entry.title) > 80 else ""
            )
            suffix = f" — {ts}" if ts else ""
            lines.append(
                f"• <a href=\"{_escape_html(entry.link)}\">"
                f"{_escape_html(title_line)}</a>{suffix}"
            )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def filter_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /filter <url> [show | clear | include=... exclude=...]

    Forms:
      /filter <url>                       → show current filter
      /filter <url> clear                 → remove filter
      /filter <url> include=a,b exclude=c → set filter
      /filter <url> include=a,b           → set include only
    """
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/filter &lt;url&gt; — show current filter\n"
            "/filter &lt;url&gt; clear — remove filter\n"
            "/filter &lt;url&gt; include=a,b exclude=c,d — set filter\n\n"
            "Matching is case-insensitive substring on title + summary.",
            parse_mode="HTML",
        )
        return

    url = context.args[0]
    chat_id = str(update.effective_chat.id)
    rest = context.args[1:]

    session_factory = get_session_factory()

    # Show
    if not rest:
        async with session_factory() as session:
            service = SubscriptionService(session)
            rule = await service.get_feed_filter(
                platform="telegram", channel_id=chat_id, feed_url=url
            )
        if rule is None:
            await update.message.reply_text(
                f"⚠️ No subscription to <code>{_escape_html(url)}</code> in this chat.",
                parse_mode="HTML",
            )
            return
        if rule.is_empty():
            await update.message.reply_text(
                "No filter set — every entry is delivered."
            )
            return
        lines = ["<b>Filter</b>"]
        if rule.include_keywords:
            lines.append(
                "<b>Include</b> (any of): "
                + ", ".join(
                    f"<code>{_escape_html(k)}</code>"
                    for k in rule.include_keywords
                )
            )
        if rule.exclude_keywords:
            lines.append(
                "<b>Exclude</b> (none of): "
                + ", ".join(
                    f"<code>{_escape_html(k)}</code>"
                    for k in rule.exclude_keywords
                )
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    # Clear
    if len(rest) == 1 and rest[0].lower() == "clear":
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.clear_feed_filter(
                platform="telegram", channel_id=chat_id, feed_url=url
            )
            await session.commit()
        prefix = "✅" if result.success else "❌"
        await update.message.reply_text(
            f"{prefix} {_escape_html(result.message)}", parse_mode="HTML"
        )
        return

    # Set: parse include=... exclude=... tokens
    include_csv: str | None = None
    exclude_csv: str | None = None
    for token in rest:
        if "=" not in token:
            await update.message.reply_text(
                f"❌ Can't parse <code>{_escape_html(token)}</code>. "
                "Expected <code>include=a,b</code> or <code>exclude=a,b</code>.",
                parse_mode="HTML",
            )
            return
        key, _, value = token.partition("=")
        key = key.lower()
        if key == "include":
            include_csv = value
        elif key == "exclude":
            exclude_csv = value
        else:
            await update.message.reply_text(
                f"❌ Unknown key <code>{_escape_html(key)}</code>. "
                "Use <code>include=</code> or <code>exclude=</code>.",
                parse_mode="HTML",
            )
            return

    include_kw = parse_keyword_csv(include_csv)
    exclude_kw = parse_keyword_csv(exclude_csv)

    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.set_feed_filter(
            platform="telegram",
            channel_id=chat_id,
            feed_url=url,
            include_keywords=include_kw,
            exclude_keywords=exclude_kw,
        )
        await session.commit()

    prefix = "✅" if result.success else "❌"
    await update.message.reply_text(
        f"{prefix} {_escape_html(result.message)}", parse_mode="HTML"
    )


async def setlang_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /setlang <url> <code> — per-feed translation language override."""
    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: /setlang <rss_url> <language_code>\n"
            "Example: /setlang https://example.com/feed zh-CN\n\n"
            "Sets the translation language for ONE feed. Use /language for "
            "the channel-wide default."
        )
        return

    url, code = context.args
    chat_id = str(update.effective_chat.id)

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.set_feed_language(
            platform="telegram", channel_id=chat_id, feed_url=url, language=code
        )
        await session.commit()

    prefix = "✅" if result.success else "❌"
    await update.message.reply_text(
        f"{prefix} {_escape_html(result.message)}", parse_mode="HTML"
    )


async def settrans_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /settrans <url> <on|off> — per-feed translation toggle."""
    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: /settrans <rss_url> <on|off>\n"
            "Example: /settrans https://example.com/feed off\n\n"
            "Toggles translation for ONE feed. Use /translate for the "
            "channel-wide default."
        )
        return

    url = context.args[0]
    enabled = context.args[1].lower() in ("on", "true", "yes", "1", "enable", "enabled")
    chat_id = str(update.effective_chat.id)

    if enabled and not get_settings().can_translate():
        await update.message.reply_text(
            "⚠️ <b>Translation Not Available</b>\n\n"
            "Translation is not configured on this bot instance.",
            parse_mode="HTML",
        )
        return

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.set_feed_translate(
            platform="telegram", channel_id=chat_id, feed_url=url, enabled=enabled
        )
        await session.commit()

    prefix = "✅" if result.success else "❌"
    await update.message.reply_text(
        f"{prefix} {_escape_html(result.message)}", parse_mode="HTML"
    )


async def export_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /export — send the subscription list as an OPML file."""
    import io

    chat_id = str(update.effective_chat.id)
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        opml_xml = await service.export_opml(
            platform="telegram", channel_id=chat_id
        )

    buf = io.BytesIO(opml_xml.encode("utf-8"))
    await update.message.reply_document(
        document=buf,
        filename=f"newsflow-{chat_id}.opml",
        caption="Your subscription list",
    )


async def _do_opml_import(
    update: Update, chat_id: str, user_id: str, opml_content: str
) -> None:
    """Shared core for /import with URL and document-upload handlers."""
    processing = await update.message.reply_text("⏳ Importing…")

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.import_opml(
            platform="telegram",
            user_id=user_id,
            channel_id=chat_id,
            opml_content=opml_content,
        )
        await session.commit()

    lines = [
        "<b>OPML Import Result</b>",
        f"✅ Added: <b>{len(result.added)}</b>",
        f"⏭️ Already subscribed: <b>{len(result.already_subscribed)}</b>",
        f"❌ Failed: <b>{len(result.failed)}</b>",
    ]
    if result.failed:
        lines.append("")
        lines.append("<b>Failures:</b>")
        for url, err in result.failed[:10]:
            lines.append(
                f"• <code>{_escape_html(url[:60])}</code>: "
                f"{_escape_html(err[:80])}"
            )
        if len(result.failed) > 10:
            lines.append(f"…and {len(result.failed) - 10} more")

    await processing.edit_text("\n".join(lines), parse_mode="HTML")


async def import_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /import <url> — fetch an OPML document from a URL and import.

    File-upload imports are handled by import_document below.
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: /import &lt;url&gt;\n\n"
            "Or upload an .opml file directly to this chat — I'll pick it up.",
            parse_mode="HTML",
        )
        return

    url = context.args[0]
    from newsflow.core import get_fetcher
    from newsflow.core.url_security import InvalidFeedURLError, validate_feed_url

    try:
        validate_feed_url(url)
    except InvalidFeedURLError as e:
        await update.message.reply_text(f"❌ Rejected URL: {e}")
        return

    try:
        fetcher = get_fetcher()
        client = await fetcher._get_session()
        async with client.get(url) as response:
            if response.status != 200:
                await update.message.reply_text(
                    f"❌ Failed to fetch OPML: HTTP {response.status}"
                )
                return
            data = await response.content.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                await update.message.reply_text(
                    "❌ OPML file too large (1 MB cap)"
                )
                return
            content = data.decode("utf-8", errors="replace")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch OPML: {e}")
        return

    await _do_opml_import(
        update,
        chat_id=str(update.effective_chat.id),
        user_id=str(update.effective_user.id),
        opml_content=content,
    )


async def import_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Auto-import when a user uploads an .opml / .xml file to the chat.

    Triggered by a document filter registered in TelegramAdapter.start,
    not by /import text command — PTB's CommandHandler doesn't inspect
    captions, and requiring `/import` as caption would be error-prone UX.
    """
    doc = update.message.document
    if doc is None:
        return
    name = (doc.file_name or "").lower()
    if not name.endswith((".opml", ".xml")):
        return
    if doc.file_size and doc.file_size > 1024 * 1024:
        await update.message.reply_text("❌ OPML file too large (1 MB cap)")
        return

    try:
        tg_file = await doc.get_file()
        data = await tg_file.download_as_bytearray()
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        await update.message.reply_text("❌ OPML file is not valid UTF-8")
        return
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to read OPML: {e}")
        return

    await _do_opml_import(
        update,
        chat_id=str(update.effective_chat.id),
        user_id=str(update.effective_user.id),
        opml_content=content,
    )


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /test command - test an RSS feed."""
    if not context.args:
        await update.message.reply_text("Usage: /test <rss_url>")
        return

    url = context.args[0]
    processing_msg = await update.message.reply_text("⏳ Testing feed...")

    from newsflow.core import get_fetcher

    fetcher = get_fetcher()
    result = await fetcher.fetch_feed(url)

    if result.success:
        desc = result.feed_description or ""
        if len(desc) > 200:
            desc = desc[:200] + "..."

        message = (
            f"✅ <b>Feed Test: Success</b>\n\n"
            f"<b>{result.feed_title or 'Untitled Feed'}</b>\n\n"
            f"Entries: {len(result.entries)}\n"
            f"URL: {url}"
        )
        if desc:
            message += f"\n\nDescription: {desc}"
    else:
        message = f"❌ <b>Feed Test: Failed</b>\n\nError: {result.error}\n\nURL: {url}"

    await processing_msg.edit_text(message, parse_mode="HTML")


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /language command."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /language <language_code>\n\n"
            "Examples:\n"
            "/language zh-CN (Simplified Chinese)\n"
            "/language ja (Japanese)\n"
            "/language ko (Korean)\n"
            "/language en (English)"
        )
        return

    language = context.args[0]
    chat_id = str(update.effective_chat.id)

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        success = await service.update_settings(
            platform="telegram",
            channel_id=chat_id,
            target_language=language,
        )
        await session.commit()

    if success:
        message = f"✅ <b>Language Updated</b>\n\nTranslation language set to: <b>{language}</b>"
    else:
        message = "⚠️ <b>No Subscriptions</b>\n\nNo feeds subscribed in this chat."

    await update.message.reply_text(message, parse_mode="HTML")


async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /translate command."""
    if not context.args:
        await update.message.reply_text("Usage: /translate <on/off>")
        return

    enabled = context.args[0].lower() in ("on", "true", "yes", "1")
    chat_id = str(update.effective_chat.id)

    settings = get_settings()
    if enabled and not settings.can_translate():
        await update.message.reply_text(
            "⚠️ <b>Translation Not Available</b>\n\n"
            "Translation is not configured on this bot instance.\n"
            "The bot owner needs to set up translation API keys.",
            parse_mode="HTML",
        )
        return

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        success = await service.update_settings(
            platform="telegram",
            channel_id=chat_id,
            translate=enabled,
        )
        await session.commit()

    status = "enabled" if enabled else "disabled"
    if success:
        message = f"✅ <b>Translation Updated</b>\n\nTranslation <b>{status}</b> for all feeds in this chat."
    else:
        message = "⚠️ <b>No Subscriptions</b>\n\nNo feeds subscribed in this chat."

    await update.message.reply_text(message, parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    chat_id = str(update.effective_chat.id)
    settings = get_settings()

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        subs = await service.get_channel_subscriptions(
            platform="telegram",
            channel_id=chat_id,
        )

    translation_status = "Available ✅" if settings.can_translate() else "Not configured ❌"

    message = (
        "📊 <b>NewsFlow Bot Status</b>\n\n"
        f"Translation: {translation_status}\n"
        f"Fetch Interval: {settings.fetch_interval_minutes} min\n"
        f"Chat Subscriptions: {len(subs)}\n"
        f"\n🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    await update.message.reply_text(message, parse_mode="HTML")


class TelegramAdapter(BaseAdapter):
    """Telegram adapter implementation."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.app: Application | None = None

    @property
    def platform_name(self) -> str:
        return "telegram"

    def is_connected(self) -> bool:
        """Application running + updater polling. PTB auto-reconnects on
        transient errors; this flag turns off only when the updater is
        actually stopped or never started."""
        if self.app is None:
            return False
        updater = self.app.updater
        return bool(updater is not None and updater.running)

    async def start(self) -> None:
        """Start the Telegram bot."""
        global _adapter
        _adapter = self

        # AIORateLimiter transparently queues send_message calls to stay
        # inside Telegram's 30/s global, 1/s per-chat, and 20/min per-group
        # broadcast limits. Needs python-telegram-bot[rate-limiter].
        self.app = (
            Application.builder()
            .token(self.token)
            .rate_limiter(AIORateLimiter())
            .build()
        )

        # Register handlers
        self.app.add_handler(CommandHandler("start", start_command))
        self.app.add_handler(CommandHandler("help", help_command))
        self.app.add_handler(CommandHandler("add", add_command))
        self.app.add_handler(CommandHandler("remove", remove_command))
        self.app.add_handler(CommandHandler("pause", pause_command))
        self.app.add_handler(CommandHandler("resume", resume_command))
        self.app.add_handler(CommandHandler("list", list_command))
        self.app.add_handler(CommandHandler("info", info_command))
        self.app.add_handler(CommandHandler("test", test_command))
        self.app.add_handler(CommandHandler("language", language_command))
        self.app.add_handler(CommandHandler("translate", translate_command))
        self.app.add_handler(CommandHandler("setlang", setlang_command))
        self.app.add_handler(CommandHandler("settrans", settrans_command))
        self.app.add_handler(CommandHandler("filter", filter_command))
        self.app.add_handler(CommandHandler("import", import_command))
        self.app.add_handler(CommandHandler("export", export_command))
        self.app.add_handler(CommandHandler("status", status_command))
        # Auto-import when user uploads an .opml/.xml file (no caption needed).
        self.app.add_handler(
            MessageHandler(
                filters.Document.FileExtension("opml")
                | filters.Document.FileExtension("xml"),
                import_document,
            )
        )

        logger.info("Starting Telegram bot...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

        # Register adapter with dispatcher (dispatch loop is managed by main.py)
        dispatcher = get_dispatcher()
        dispatcher.register_adapter("telegram", self)
        logger.info("Telegram adapter registered with dispatcher")

        logger.info("Telegram bot started successfully")

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        global _adapter

        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

        _adapter = None
        logger.info("Telegram bot stopped")

    async def send_message(self, channel_id: str, message: Message) -> bool:
        """Send a message to a Telegram chat."""
        if not self.app:
            return False

        try:
            text = self._format_message(message)
            await self.app.bot.send_message(
                chat_id=int(channel_id),
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
            return True
        except Exception as e:
            logger.exception(f"Failed to send message to {channel_id}: {e}")
            return False

    async def send_text(self, channel_id: str, text: str) -> bool:
        """Send plain text to a Telegram chat."""
        if not self.app:
            return False

        try:
            await self.app.bot.send_message(
                chat_id=int(channel_id),
                text=text,
            )
            return True
        except Exception as e:
            logger.exception(f"Failed to send text to {channel_id}: {e}")
            return False

    def _format_message(self, message: Message) -> str:
        """Format a Message for Telegram."""
        title = self._escape_html(message.display_title)
        summary = message.display_summary

        # Truncate summary
        if summary and len(summary) > 500:
            summary = summary[:497] + "..."

        parts = [
            f"<b>{title}</b>",
            "",
        ]

        if summary:
            parts.append(self._escape_html(summary))
            parts.append("")

        parts.extend([
            f"🔗 <a href=\"{message.link}\">Read more</a>",
            f"📰 {self._escape_html(message.source)}",
        ])

        if message.published_at:
            parts.append(f"🕐 {message.published_at.strftime('%Y-%m-%d %H:%M')}")

        return "\n".join(parts)

    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )


# Global app instance
_app: Application | None = None


async def start_telegram(token: str) -> None:
    """Start the Telegram bot."""
    global _app
    adapter = TelegramAdapter(token)
    await adapter.start()
    _app = adapter.app

    # Keep running
    while True:
        await asyncio.sleep(1)


async def stop_telegram() -> None:
    """Stop the Telegram bot."""
    global _app, _adapter
    if _adapter:
        await _adapter.stop()
    _app = None
