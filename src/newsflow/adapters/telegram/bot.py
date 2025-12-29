"""
Telegram bot adapter.

Implements Telegram-specific functionality using python-telegram-bot.
"""

import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from newsflow.adapters.base import BaseAdapter, Message
from newsflow.config import get_settings
from newsflow.models.base import get_session_factory
from newsflow.services import SubscriptionService, get_dispatcher

logger = logging.getLogger(__name__)

# Global adapter reference for command handlers
_adapter: "TelegramAdapter | None" = None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "🗞️ <b>Welcome to NewsFlow Bot!</b>\n\n"
        "I can send you updates from RSS feeds.\n\n"
        "<b>Commands:</b>\n"
        "/add &lt;url&gt; - Add an RSS feed\n"
        "/remove &lt;url&gt; - Remove an RSS feed\n"
        "/list - List subscribed feeds\n"
        "/test &lt;url&gt; - Test an RSS feed\n"
        "/language &lt;code&gt; - Set translation language\n"
        "/translate &lt;on/off&gt; - Toggle translation\n"
        "/status - Show bot status\n"
        "/help - Show this help message",
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


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /list command."""
    chat_id = str(update.effective_chat.id)

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        subscriptions = await service.get_channel_subscriptions(
            platform="telegram",
            channel_id=chat_id,
        )

    if not subscriptions:
        await update.message.reply_text(
            "📭 <b>No feeds subscribed</b>\n\n"
            "Use /add <url> to subscribe to an RSS feed.",
            parse_mode="HTML",
        )
        return

    lines = [f"📰 <b>Subscribed Feeds ({len(subscriptions)})</b>\n"]

    for sub in subscriptions:
        feed = sub.feed
        translate_status = "🌐 On" if sub.translate else "Off"
        lines.append(
            f"\n<b>{feed.title or 'Untitled'}</b>\n"
            f"URL: {feed.url}\n"
            f"Translate: {translate_status} ({sub.target_language})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
        self._dispatch_task: asyncio.Task | None = None

    @property
    def platform_name(self) -> str:
        return "telegram"

    async def start(self) -> None:
        """Start the Telegram bot."""
        global _adapter
        _adapter = self

        self.app = Application.builder().token(self.token).build()

        # Register handlers
        self.app.add_handler(CommandHandler("start", start_command))
        self.app.add_handler(CommandHandler("help", help_command))
        self.app.add_handler(CommandHandler("add", add_command))
        self.app.add_handler(CommandHandler("remove", remove_command))
        self.app.add_handler(CommandHandler("list", list_command))
        self.app.add_handler(CommandHandler("test", test_command))
        self.app.add_handler(CommandHandler("language", language_command))
        self.app.add_handler(CommandHandler("translate", translate_command))
        self.app.add_handler(CommandHandler("status", status_command))

        logger.info("Starting Telegram bot...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

        # Register adapter with dispatcher
        dispatcher = get_dispatcher()
        dispatcher.register_adapter("telegram", self)

        # Start dispatch loop
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(
                dispatcher.run_dispatch_loop()
            )
            logger.info("Started dispatch loop")

        logger.info("Telegram bot started successfully")

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        global _adapter

        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

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
