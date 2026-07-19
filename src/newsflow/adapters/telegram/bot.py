"""
Telegram bot adapter.

Implements Telegram-specific functionality using python-telegram-bot.
"""

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram import Message as TelegramMessage
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from newsflow.adapters.base import (
    BaseAdapter,
    ChannelGoneError,
    ChannelMigratedError,
    Message,
    TopicGoneError,
)
from newsflow.config import get_settings
from newsflow.core.filter import parse_filter_field
from newsflow.core.languages import LANGUAGE_CODE_EXAMPLES, normalize_language_code
from newsflow.core.message_template import (
    PLACEHOLDER_LIST,
    normalize_template,
    validate_template,
)
from newsflow.core.telegram_markdown import markdown_to_telegram_html
from newsflow.core.timeutil import relative_time, time_until
from newsflow.core.timezones import local_schedule_to_utc, parse_timezone
from newsflow.models.base import get_session_factory
from newsflow.models.subscription import Subscription
from newsflow.services import SubscriptionService, get_dispatcher
from newsflow.services.subscription_service import (
    SubscriptionActionResult,
    UnsubscribeResult,
)

LIST_PAGE_SIZE = 20

logger = logging.getLogger(__name__)

# Global adapter reference for command handlers
_adapter: "TelegramAdapter | None" = None

# Group-admin gate for state-changing commands. get_chat_member costs a Bot
# API round-trip, so verdicts are cached briefly per (chat, user); a
# promotion/demotion becomes visible after at most the TTL.
_ADMIN_CACHE_TTL_SECONDS = 60.0
_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}


async def _cached_is_admin(bot: Any, chat_id: int, user_id: int) -> bool:
    """Owner/administrator check with the shared TTL cache. Raises on
    lookup failure — callers decide the fail-closed reaction."""
    key = (chat_id, user_id)
    now = time.monotonic()
    cached = _admin_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    member = await bot.get_chat_member(chat_id, user_id)
    allowed = member.status in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    )
    _admin_cache[key] = (now + _ADMIN_CACHE_TTL_SECONDS, allowed)
    return allowed


async def _require_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the sender may run a state-changing command in this chat.

    Private chats are always allowed — the user manages their own feeds.
    In groups (TELEGRAM_ADMIN_ONLY, default on) only the owner and
    administrators pass, plus ADMIN_USER_IDS as a global bypass. Sends the
    denial reply itself; fails closed when the membership lookup errors.
    """
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return False
    if chat.type == ChatType.PRIVATE:
        return True
    settings = get_settings()
    if not settings.telegram_admin_only:
        return True
    # A message sent "as the group" (sender_chat == the chat itself) comes
    # from an anonymous admin — Telegram hides who, but only admins can post
    # that way, and get_chat_member can't resolve them anyway.
    if msg.sender_chat is not None and msg.sender_chat.id == chat.id:
        return True
    user = update.effective_user
    if user is None:
        return False
    if str(user.id) in settings.admin_user_ids:
        return True

    try:
        allowed = await _cached_is_admin(context.bot, chat.id, user.id)
    except Exception:
        logger.exception(f"get_chat_member({chat.id}, {user.id}) failed; denying")
        await msg.reply_text("⚠️ Couldn't verify your admin status — please try again.")
        return False
    if not allowed:
        await msg.reply_text("⛔ Only group admins can use this command here.")
    return allowed


# Channel reference accepted as a command's first argument in private chat:
# a public @username or a raw -100… chat id (private channels have no
# username; the id is visible in the channel's web-client URL).
_CHANNEL_REF_RE = re.compile(r"^(@\w{4,32}|-100\d+)$")


async def _resolve_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[str, list[str]] | None:
    """Resolve which chat a command targets — the private-chat channel
    binding (F4): channels can't host commands (no sender, and PTB's
    CommandHandler ignores channel_post anyway), so admins manage a
    channel by DM-ing the bot with a leading channel reference:

        /add @mychannel https://example.com/feed

    Requirements enforced here: the bot can see the channel (it must be
    added as a channel ADMIN to be able to post at all) and the caller
    is one of the channel's admins (ADMIN_USER_IDS bypasses, mirroring
    _require_group_admin; verdicts share the same TTL cache). Outside
    private chat — or without a leading channel ref — the current chat
    is the target and args pass through untouched.

    Returns (chat_id, remaining_args); None when channel validation
    failed (the error reply has already been sent).
    """
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None:
        return None
    args = list(context.args or [])
    if chat.type != ChatType.PRIVATE or not args or not _CHANNEL_REF_RE.match(args[0]):
        return str(chat.id), args

    ref = args[0]
    try:
        target = await context.bot.get_chat(ref if ref.startswith("@") else int(ref))
    except Exception:
        await msg.reply_text(
            f"⚠️ Can't access {ref}. Add the bot to that channel as an "
            "administrator first (it needs posting rights anyway), then retry."
        )
        return None
    if target.type != ChatType.CHANNEL:
        await msg.reply_text(f"⚠️ {ref} is not a channel. This form only targets channels.")
        return None
    if user is None:
        return None

    if str(user.id) not in get_settings().admin_user_ids:
        try:
            allowed = await _cached_is_admin(context.bot, target.id, user.id)
        except Exception:
            logger.exception(f"get_chat_member({target.id}, {user.id}) failed; denying")
            await msg.reply_text(
                "⚠️ Couldn't verify your admin status in that channel — please try again."
            )
            return None
        if not allowed:
            await msg.reply_text("⛔ Only that channel's admins can manage its feeds.")
            return None

    return str(target.id), args[1:]


WELCOME_TEXT = (
    "🗞️ <b>Welcome to NewsFlow Bot!</b>\n\n"
    "I can send you updates from RSS feeds.\n\n"
    "<b>Feed management:</b>\n"
    "/add &lt;url&gt; — Subscribe\n"
    "/remove &lt;url&gt; — Unsubscribe\n"
    "/pause &lt;url&gt; — Temporarily stop delivery\n"
    "/resume &lt;url&gt; — Resume delivery (/resume all — every paused feed)\n"
    "/list [page] — List subscribed feeds\n"
    "/manage — Per-feed buttons: pause/resume/silence/remove, no URL typing\n"
    "/info &lt;url&gt; — Detailed status of one feed\n"
    "/test &lt;url&gt; — Check if a URL is a valid feed\n\n"
    "<b>OPML:</b>\n"
    "/export — Download subscriptions as OPML\n"
    "/import &lt;url&gt; — Import from a hosted OPML URL\n"
    "(or just upload an .opml file to this chat)\n\n"
    "<b>Settings (channel-wide):</b>\n"
    "/language &lt;code&gt; — Default translation language\n"
    "/translate &lt;on/off&gt; — Default translation toggle\n"
    "/silent &lt;on/off&gt; — Channel-wide silent (digest-only)\n\n"
    "<b>Settings (per-feed overrides):</b>\n"
    "/setlang &lt;url&gt; &lt;code&gt; — Per-feed language\n"
    "/settrans &lt;url&gt; &lt;on/off&gt; — Per-feed translate\n"
    "/setsilent &lt;url&gt; &lt;on/off&gt; — Per-feed silent\n"
    "/setdisplay &lt;url&gt; &lt;summary|image&gt; &lt;on/off&gt; — Per-feed display (compact mode)\n"
    "/template &lt;url|all&gt; [text | reset] — Custom message layout ({title}, {url}, …)\n"
    "/settopic &lt;url|all&gt; [clear] — Deliver a feed to the current forum topic\n"
    "/filter &lt;url&gt; [show | clear | include=a,b exclude=c] — Keyword filter\n\n"
    "<b>AI Digest:</b>\n"
    "/digest show — Show current digest config\n"
    "/digest enable daily &lt;hour&gt; [lang] [tz] — Daily digest\n"
    "/digest enable weekly &lt;weekday&gt; &lt;hour&gt; [lang] [tz] — Weekly digest\n"
    "/digest disable — Turn off\n"
    "/digest now — Generate and send one immediately\n\n"
    "<b>Other:</b>\n"
    "/status — Bot status\n"
    "/help — This message\n\n"
    "<b>Channels:</b> add me to your channel as an administrator, then "
    "manage it from this private chat by putting the channel first:\n"
    "/add @channelusername &lt;url&gt; · /list @channelusername · "
    "/digest @channelusername enable …\n"
    "(private channels: use the -100… id instead of @username)"
)

# Commands surfaced in Telegram's command menu (the "/" list + Menu button).
# Curated to the common actions; the full set stays in WELCOME_TEXT / /help.
_MENU_COMMANDS: list[tuple[str, str]] = [
    ("add", "Subscribe to an RSS feed URL"),
    ("remove", "Unsubscribe from a feed"),
    ("list", "List subscribed feeds"),
    ("manage", "Manage feeds with buttons (no URL typing)"),
    ("info", "Detailed status of one feed"),
    ("pause", "Pause delivery for a feed"),
    ("resume", "Resume a paused feed"),
    ("test", "Check whether a URL is a valid feed"),
    ("language", "Set the default translation language"),
    ("translate", "Toggle translation on or off"),
    ("template", "Custom message layout for a feed"),
    ("settopic", "Deliver a feed to the current topic"),
    ("digest", "Configure the AI daily/weekly digest"),
    ("status", "Show bot status"),
    ("help", "Show the full command list"),
]


def _start_menu_keyboard() -> InlineKeyboardMarkup:
    """Quick-action buttons shown under /start and /help."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📰 My feeds", callback_data="menu:list"),
                InlineKeyboardButton("🛠 Manage", callback_data="menu:manage"),
            ],
            [
                InlineKeyboardButton("📊 Status", callback_data="menu:status"),
                InlineKeyboardButton("❓ Help", callback_data="menu:help"),
            ],
        ]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    msg = update.message
    if msg is None:
        return
    await msg.reply_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=_start_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await start_command(update, context)


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /add command."""
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None or user is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if not args:
        await msg.reply_text(
            "Usage: /add <rss_url>\n"
            "       /add @channelusername <rss_url> (manage a channel from DM)\n\n"
            "Example: /add https://example.com/feed.xml"
        )
        return

    url = args[0]
    user_id = str(user.id)
    # Record the forum topic the command ran in so entries deliver there.
    # is_topic_message guard: plain reply threads also carry a
    # message_thread_id and must NOT be recorded. Channel-bound calls come
    # from private chats, where is_topic_message is never set.
    thread_id = msg.message_thread_id if msg.is_topic_message else None

    # Send processing message
    processing_msg = await msg.reply_text("⏳ Adding feed...")

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)

        result = await service.subscribe(
            platform="telegram",
            user_id=user_id,
            channel_id=chat_id,
            feed_url=url,
            message_thread_id=thread_id,
        )

        await session.commit()

    # Deliver a preview entry in the background so the user sees content
    # without waiting a full fetch interval. spawn() keeps a strong ref so
    # the event loop can't GC the task mid-flight.
    if result.success and result.is_new and result.subscription:
        dispatcher = get_dispatcher()
        dispatcher.spawn(
            dispatcher.schedule_preview(result.subscription.id),
            name=f"preview:telegram:{result.subscription.id}",
        )

    # Escape everything user-/feed-controlled: a title containing "&" or a
    # URL with a query string would otherwise make Telegram reject the HTML
    # parse — leaving the user stuck on "Adding feed..." although the
    # subscription actually succeeded.
    if result.success:
        assert result.feed is not None  # success guarantees a resolved feed
        feed_title = _escape_html(result.feed.title or url)
        if result.is_new:
            message = f"✅ <b>Feed Added</b>\n\n<b>{feed_title}</b>\n\n{_escape_html(url)}"
        else:
            message = (
                f"✅ <b>Feed Added</b>\n\n<b>{feed_title}</b>\n\n{_escape_html(result.message)}"
            )
    else:
        message = (
            f"❌ <b>Failed to Add Feed</b>\n\n{_escape_html(result.message)}\n\n"
            f"URL: {_escape_html(url)}"
        )

    await processing_msg.edit_text(message, parse_mode="HTML")


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /remove command."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if not args:
        await msg.reply_text("Usage: /remove <rss_url>")
        return

    url = args[0]

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
        message = f"✅ <b>Feed Removed</b>\n\n{_escape_html(result.message)}"
    else:
        message = f"❌ <b>Failed to Remove Feed</b>\n\n{_escape_html(result.message)}"

    await msg.reply_text(message, parse_mode="HTML")


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _is_thread_gone(e: Exception) -> bool:
    """Telegram's marker for a send into a deleted forum topic — BadRequest
    "Message thread not found". The chat itself is alive (that would be a
    different error), so this maps to TopicGoneError, not ChannelGone."""
    from telegram.error import BadRequest

    return isinstance(e, BadRequest) and "message thread not found" in str(e).lower()


def _sub_status_chip(sub: Subscription) -> str | None:
    feed = sub.feed
    if not sub.is_active:
        return "⏸ paused"
    if not feed.is_active:
        return "🛑 auto-disabled"
    if feed.error_count > 0:
        return f"⚠️ {feed.error_count} errors, retry {time_until(feed.next_retry_at)}"
    if sub.silent:
        return "🔇 silent (digest only)"
    return None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_sub_line(sub: Subscription) -> str:
    feed = sub.feed
    # Clip BEFORE escaping so an entity can't be cut in half. Bounds matter:
    # feed.title is up to 512 chars and feed.url up to 2048 — unclipped, a
    # page of those would blow Telegram's 4096-char message cap and the whole
    # /list would 400. The full URL stays available via /export (OPML).
    title = _escape_html(_clip(feed.title or "Untitled", 80))
    # target_language is stored verbatim from user input — unescaped, a value
    # like `<b` breaks the HTML parse for every subsequent /list in the chat.
    parts = [f"🌐 {_escape_html(sub.target_language)}" if sub.translate else "📰 no translate"]
    chip = _sub_status_chip(sub)
    if chip:
        parts.append(chip)
    meta = " · ".join(parts)
    return f"<b>{title}</b> · {meta}\n{_escape_html(_clip(feed.url, 200))}"


# Telegram rejects messages over 4096 chars. Pages are packed greedily by
# character budget (with LIST_PAGE_SIZE as a secondary item cap), so a page
# can never exceed the limit even with worst-case escaped titles/URLs. The
# budget leaves headroom for the header line.
_LIST_CHAR_BUDGET = 3500


def _paginate_lines(lines: list[str]) -> list[list[str]]:
    """Deterministically pack rendered lines into pages. Same input order →
    same page boundaries, so prev/next navigation stays stable across
    renders (subscriptions are id-ordered upstream)."""
    pages: list[list[str]] = []
    current: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 2  # "\n\n" separator
        if current and (used + cost > _LIST_CHAR_BUDGET or len(current) >= LIST_PAGE_SIZE):
            pages.append(current)
            current, used = [], 0
        current.append(line)
        used += cost
    if current:
        pages.append(current)
    return pages


def _list_keyboard(
    page: int, total_pages: int, target: str | None = None
) -> InlineKeyboardMarkup | None:
    """Prev/Next row for the paginated feed list. None when there's one page.

    `target` carries a channel id when the list was requested via the
    private-chat channel binding (`/list @channel`), so pagination keeps
    rendering THAT channel's list instead of flipping to the DM's own.
    """
    if total_pages <= 1:
        return None
    suffix = f":{target}" if target else ""
    row: list[InlineKeyboardButton] = []
    if page > 1:
        row.append(InlineKeyboardButton("◀ Prev", callback_data=f"list:{page - 1}{suffix}"))
    if page < total_pages:
        row.append(InlineKeyboardButton("Next ▶", callback_data=f"list:{page + 1}{suffix}"))
    return InlineKeyboardMarkup([row])


async def _render_list(
    chat_id: str, page: int, target: str | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the paginated feed-list text + prev/next keyboard for a chat.

    Shared by /list and the pagination callback. Subscriptions are ordered by
    id in the repository, so the same page renders identically across calls
    and prev/next stays consistent.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        subs = list(
            await service.get_channel_subscriptions(
                platform="telegram",
                channel_id=chat_id,
                # Paused subs must stay listed (with the ⏸ chip) or their
                # URLs become unfindable and /resume impossible.
                include_inactive=True,
            )
        )

    if not subs:
        return (
            "📭 <b>No feeds subscribed</b>\n\nUse /add &lt;url&gt; to subscribe to an RSS feed.",
            None,
        )

    total = len(subs)
    pages = _paginate_lines([_format_sub_line(s) for s in subs])
    total_pages = len(pages)
    page = max(1, min(page, total_pages))

    header = f"📰 <b>Subscribed Feeds ({total})</b>"
    if total_pages > 1:
        header += f" — page {page}/{total_pages}"
    body = "\n\n".join(pages[page - 1])
    return header + "\n\n" + body, _list_keyboard(page, total_pages, target)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /list [page]. Paginated: LIST_PAGE_SIZE feeds per page, with
    inline prev/next buttons."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return

    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    try:
        page = int(args[0]) if args else 1
    except (ValueError, IndexError):
        page = 1

    # Carry the channel id through pagination buttons when the list was
    # requested via the private-chat channel binding.
    target = chat_id if chat_id != str(chat.id) else None
    text, keyboard = await _render_list(chat_id, page, target)
    await msg.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


# --- /manage: per-feed inline actions (no URL retyping) ---------------------
#
# Callback data formats (Telegram caps callback_data at 64 bytes, so views
# carry the subscription id, never the URL):
#   mg:p:<page>[:target]                 manage list page
#   mg:v:<sub_id>:<page>[:target]        one feed's action view
#   mg:a:<action>:<sub_id>:<page>[:target]  action: pause|resume|sil0|sil1|rm|rmc
# `target` is the -100… channel id when the panel was opened via the
# private-chat channel binding (/manage @channel).

MANAGE_PAGE_SIZE = 8


def _manage_chip(sub: Subscription) -> str:
    if not sub.is_active:
        return "⏸"
    if not sub.feed.is_active:
        return "🛑"
    if sub.silent:
        return "🔇"
    return "📰"


def _mg_suffix(target: str | None) -> str:
    return f":{target}" if target else ""


def _mg_target(parts: list[str], index: int) -> str | None:
    if len(parts) > index and re.fullmatch(r"-100\d+", parts[index]):
        return parts[index]
    return None


def _int_or(raw: str, default: int) -> int:
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


async def _load_channel_subs(chat_id: str) -> list[Subscription]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        return list(
            await service.get_channel_subscriptions(
                platform="telegram", channel_id=chat_id, include_inactive=True
            )
        )


async def _load_sub(sub_id: int) -> Subscription | None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        return await service.get_subscription_by_id(sub_id)


def _manage_list_view(
    subs: list[Subscription], page: int, target: str | None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """One button per subscription; tapping opens its action view."""
    if not subs:
        return (
            "📭 <b>No feeds subscribed</b>\n\nUse /add &lt;url&gt; to subscribe first.",
            None,
        )
    total_pages = max(1, (len(subs) + MANAGE_PAGE_SIZE - 1) // MANAGE_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * MANAGE_PAGE_SIZE
    suffix = _mg_suffix(target)
    rows = [
        [
            InlineKeyboardButton(
                f"{_manage_chip(s)} {_clip(s.feed.title or s.feed.url, 32)}",
                callback_data=f"mg:v:{s.id}:{page}{suffix}",
            )
        ]
        for s in subs[start : start + MANAGE_PAGE_SIZE]
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"mg:p:{page - 1}{suffix}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"mg:p:{page + 1}{suffix}"))
    if nav:
        rows.append(nav)
    text = f"🛠 <b>Manage feeds</b> — tap one ({len(subs)} total"
    if total_pages > 1:
        text += f", page {page}/{total_pages}"
    text += ")"
    return text, InlineKeyboardMarkup(rows)


def _manage_detail_view(
    sub: Subscription, page: int, target: str | None
) -> tuple[str, InlineKeyboardMarkup]:
    """Action buttons for one subscription."""
    feed = sub.feed
    chip = _sub_status_chip(sub)
    lines = [
        f"<b>{_escape_html(_clip(feed.title or 'Untitled', 80))}</b>",
        f"<code>{_escape_html(_clip(feed.url, 200))}</code>",
        f"State: {chip or '✅ active'}",
        f"Translate: {'on → ' + _escape_html(sub.target_language) if sub.translate else 'off'}",
    ]
    tail = f"{sub.id}:{page}{_mg_suffix(target)}"
    pause_btn = (
        InlineKeyboardButton("⏸ Pause", callback_data=f"mg:a:pause:{tail}")
        if sub.is_active
        else InlineKeyboardButton("▶️ Resume", callback_data=f"mg:a:resume:{tail}")
    )
    silent_btn = (
        InlineKeyboardButton("🔔 Unsilence", callback_data=f"mg:a:sil0:{tail}")
        if sub.silent
        else InlineKeyboardButton("🔇 Silence", callback_data=f"mg:a:sil1:{tail}")
    )
    rows = [
        [pause_btn, silent_btn],
        [InlineKeyboardButton("🗑 Remove…", callback_data=f"mg:a:rm:{tail}")],
        [InlineKeyboardButton("◀ Back", callback_data=f"mg:p:{page}{_mg_suffix(target)}")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _manage_confirm_view(
    sub: Subscription, page: int, target: str | None
) -> tuple[str, InlineKeyboardMarkup]:
    """Remove needs a second tap — it cascades filter + dedupe history."""
    tail = f"{sub.id}:{page}{_mg_suffix(target)}"
    text = (
        f"Remove <b>{_escape_html(_clip(sub.feed.title or sub.feed.url, 80))}</b>?\n\n"
        "This also deletes its keyword filter and delivery history "
        "(re-adding later starts fresh)."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 Yes, remove", callback_data=f"mg:a:rmc:{tail}")],
            [InlineKeyboardButton("◀ Cancel", callback_data=f"mg:v:{tail}")],
        ]
    )
    return text, keyboard


async def _callback_user_may_manage(
    sub: Subscription, chat: Any, user: Any, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Mutating manage-button gate — mirrors the command-side rules.

    A private chat manages its own subscriptions freely; a channel's
    subscriptions (panel opened via /manage @channel) require the presser
    to be that channel's admin; group subscriptions follow the
    TELEGRAM_ADMIN_ONLY rule. Fails closed on lookup errors.
    """
    settings = get_settings()
    if user is not None and str(user.id) in settings.admin_user_ids:
        return True
    if user is None:
        return False
    sub_chat = sub.platform_channel_id
    try:
        if chat.type == ChatType.PRIVATE:
            if sub_chat == str(chat.id):
                return True
            return await _cached_is_admin(context.bot, int(sub_chat), user.id)
        if sub_chat != str(chat.id):
            return False
        if not settings.telegram_admin_only:
            return True
        return await _cached_is_admin(context.bot, chat.id, user.id)
    except Exception:
        logger.exception(f"manage-button admin check failed for {sub_chat}; denying")
        return False


async def manage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /manage [page] — button-driven per-feed operations.

    Opening the panel is read-only (same information as /list); every
    mutating button press is gated in _callback_user_may_manage.
    """
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    try:
        page = int(args[0]) if args else 1
    except (ValueError, IndexError):
        page = 1

    target = chat_id if chat_id != str(chat.id) else None
    subs = await _load_channel_subs(chat_id)
    text, keyboard = _manage_list_view(subs, page, target)
    await msg.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pause <url>."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if not args:
        await msg.reply_text("Usage: /pause <rss_url>")
        return
    url = args[0]

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.pause_subscription(
            platform="telegram", channel_id=chat_id, feed_url=url
        )
        await session.commit()

    prefix = "⏸" if result.success else "❌"
    await msg.reply_text(f"{prefix} {_escape_html(result.message)}", parse_mode="HTML")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume <url> (or `/resume all` for every paused feed)."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if not args:
        await msg.reply_text("Usage: /resume <rss_url> (or: /resume all)")
        return
    url = args[0]

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        if url.strip().lower() == "all":
            result = await service.resume_all_subscriptions(platform="telegram", channel_id=chat_id)
        else:
            result = await service.resume_subscription(
                platform="telegram", channel_id=chat_id, feed_url=url
            )
        await session.commit()

    prefix = "▶️" if result.success else "❌"
    await msg.reply_text(f"{prefix} {_escape_html(result.message)}", parse_mode="HTML")


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /info <url> — detailed status of one subscribed feed."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if not args:
        await msg.reply_text("Usage: /info <rss_url>")
        return
    url = args[0]

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        detail = await service.get_subscription_detail(
            platform="telegram", channel_id=chat_id, feed_url=url
        )

    if detail is None:
        await msg.reply_text(
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
        state = f"⚠️ {feed.error_count} errors — retry {time_until(feed.next_retry_at)}"
    else:
        state = "✅ Healthy"

    lines = [
        f"📊 <b>{_escape_html(feed.title or 'Untitled Feed')}</b>",
        f"🔗 {_escape_html(feed.url)}",
        "",
        f"<b>State:</b> {state}",
        f"<b>Translation:</b> "
        f"{'On' if sub.translate else 'Off'} ({_escape_html(sub.target_language)})",
        f"<b>Last OK fetch:</b> {relative_time(feed.last_successful_fetch_at)}",
        f"<b>Last attempt:</b> {relative_time(feed.last_fetched_at)}",
        f"<b>Backlog:</b> {detail.unsent_count} entr"
        f"{'y' if detail.unsent_count == 1 else 'ies'} queued for this chat",
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
            ts = relative_time(entry.published_at) if entry.published_at else ""
            title_line = entry.title[:80] + ("…" if len(entry.title) > 80 else "")
            suffix = f" — {ts}" if ts else ""
            lines.append(
                f'• <a href="{_escape_html(entry.link)}">{_escape_html(title_line)}</a>{suffix}'
            )

    await msg.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


_WEEKDAY_NAMES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def _parse_digest_enable_args(rest: list[str]) -> tuple[str, int, int | None, str, str]:
    """Parse `/digest enable` args → (mode, hour, weekday, language, tz).

    Forms: `daily <hour>` / `weekly <weekday> <hour>`, then up to two
    optional trailing tokens in either order — a language code and/or a
    timezone. A token counts as a timezone only when it parses as one
    (Region/City, ±offset, or "utc"), so language codes can't be eaten.
    hour/weekday are LOCAL to the timezone (default UTC). Raises
    ValueError with a user-facing message.
    """
    mode = rest[0].lower()
    if mode == "daily":
        if len(rest) < 2:
            raise ValueError("hour required")
        hour = int(rest[1])
        weekday: int | None = None
        tail = rest[2:]
    elif mode == "weekly":
        if len(rest) < 3:
            raise ValueError("weekday and hour required")
        wd_raw = rest[1].lower()
        if wd_raw in _WEEKDAY_NAMES:
            weekday = _WEEKDAY_NAMES[wd_raw]
        else:
            weekday = int(wd_raw)
            if not 0 <= weekday <= 6:
                raise ValueError("weekday must be 0-6 or name")
        hour = int(rest[2])
        tail = rest[3:]
    else:
        raise ValueError(f"unknown schedule '{mode}'")

    if not 0 <= hour <= 23:
        raise ValueError("hour must be 0-23")
    if len(tail) > 2:
        raise ValueError("too many arguments")

    language = "zh-CN"
    tz_raw = "UTC"
    lang_seen = tz_seen = False
    for token in tail:
        if not tz_seen and parse_timezone(token) is not None:
            tz_raw = token
            tz_seen = True
        elif not lang_seen:
            language = token
            lang_seen = True
        else:
            raise ValueError(f"can't parse extra argument '{token}'")
    return mode, hour, weekday, language, tz_raw


async def digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /digest <subcommand> …

    Forms:
      /digest show
      /digest disable
      /digest now
      /digest enable daily <hour> [lang] [tz]
      /digest enable weekly <weekday> <hour> [lang] [tz]
    """
    from datetime import datetime

    from newsflow.repositories.digest_repository import (
        ChannelDigestRepository,
    )
    from newsflow.services.digest_service import DigestService
    from newsflow.services.summarization import get_summarizer

    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, dargs = resolved
    if not dargs:
        await msg.reply_text(
            "Usage:\n"
            "/digest show\n"
            "/digest enable daily &lt;hour&gt; [lang] [tz]\n"
            "/digest enable weekly &lt;weekday&gt; &lt;hour&gt; [lang] [tz]\n"
            "/digest disable\n"
            "/digest now",
            parse_mode="HTML",
        )
        return

    sub = dargs[0].lower()
    rest = dargs[1:]
    # `show` is read-only and stays open; the mutating subcommands need
    # group-admin rights (channel targeting already verified channel
    # admin inside _resolve_target).
    if sub in ("enable", "disable", "now") and not await _require_group_admin(update, context):
        return
    session_factory = get_session_factory()

    if sub == "show":
        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            config = await repo.get("telegram", chat_id)
        if config is None:
            await msg.reply_text("No digest configured. Use /digest enable to set one up.")
            return
        lines = [
            "<b>Digest Configuration</b>",
            f"Enabled: {'✅ yes' if config.enabled else '⏸ no'}",
            f"Schedule: {config.schedule}"
            + (f" (weekday {config.delivery_weekday})" if config.schedule == "weekly" else ""),
            f"Delivery time: {config.delivery_hour_utc:02d}:00 UTC",
            f"Language: {_escape_html(config.language)}",
            f"Max articles: {config.max_articles}",
            f"Include filtered: {'yes' if config.include_filtered else 'no'}",
            f"Last delivered: {relative_time(config.last_delivered_at)}",
        ]
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if sub == "disable":
        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            config = await repo.get("telegram", chat_id)
            if config is None:
                await msg.reply_text("No digest configured for this chat.")
                return
            config.enabled = False
            await session.commit()
        await msg.reply_text("⏸ Digest disabled. Use /digest enable to turn it back on.")
        return

    if sub == "now":
        summarizer = get_summarizer()
        if summarizer is None:
            await msg.reply_text(
                "⚠️ Digest not available: LLM provider not configured (check OPENAI_API_KEY)."
            )
            return

        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            config = await repo.get("telegram", chat_id)
            if config is None:
                await msg.reply_text("No digest configured. Use /digest enable first.")
                return

            service = DigestService(session, summarizer)
            now = datetime.now(UTC)
            result = await service.generate(config, now=now)
            if result is None:
                await msg.reply_text("No articles in the current window — nothing to summarize.")
                return
            if not result.success:
                await msg.reply_text(f"❌ Digest generation failed: {result.error}")
                return

            dispatcher = get_dispatcher()
            adapter = dispatcher._adapters.get("telegram")
            if adapter is None:
                await msg.reply_text("Telegram adapter not registered yet — try again.")
                return
            chunks, new_pin_id = await dispatcher.deliver_digest(
                adapter,
                chat_id,
                dispatcher.apply_digest_header(result.text, "telegram"),
                chunk_size=3800,
                prior_pin_id=config.last_pinned_message_id,
            )
            if chunks:
                await repo.mark_delivered(config.id, now, pinned_message_id=new_pin_id)
                await session.commit()
        return

    if sub == "enable":
        # Forms:
        #   enable daily <hour> [lang] [tz]
        #   enable weekly <weekday> <hour> [lang] [tz]
        if not rest:
            await msg.reply_text(
                "Usage: /digest enable daily &lt;hour&gt; [lang] [tz]  OR\n"
                "/digest enable weekly &lt;weekday&gt; &lt;hour&gt; [lang] [tz]\n\n"
                "tz: IANA name (Asia/Shanghai) or offset (+8); default UTC.",
                parse_mode="HTML",
            )
            return

        try:
            mode, hour, local_weekday, language, tz_raw = _parse_digest_enable_args(rest)
        except ValueError as e:
            await msg.reply_text(f"❌ {e}")
            return

        normalized_lang = normalize_language_code(language)
        if normalized_lang is None:
            await msg.reply_text(
                f"❌ <code>{_escape_html(language)}</code> doesn't look like a "
                f"language code. Try one of: {LANGUAGE_CODE_EXAMPLES}.",
                parse_mode="HTML",
            )
            return
        language = normalized_lang

        # Given in the user's timezone, stored as UTC — converted once,
        # here (see core/timezones.py for the DST caveat).
        tz = parse_timezone(tz_raw)
        assert tz is not None  # _parse_digest_enable_args validated it
        utc_hour, utc_weekday = local_schedule_to_utc(hour, local_weekday, tz)

        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            await repo.upsert(
                platform="telegram",
                channel_id=chat_id,
                guild_id=None,
                enabled=True,
                schedule=mode,
                delivery_hour_utc=utc_hour,
                delivery_weekday=utc_weekday,
                language=language,
            )
            await session.commit()

        local_desc = f"{hour:02d}:00 {tz_raw}" + (
            f" (weekday {local_weekday})" if local_weekday is not None else ""
        )
        utc_desc = f"{utc_hour:02d}:00 UTC" + (
            f" (weekday {utc_weekday})" if utc_weekday is not None else ""
        )
        desc = f"{mode} at {local_desc}"
        if utc_desc != local_desc:
            desc += f" = {utc_desc}"
        await msg.reply_text(f"✅ Digest enabled — {desc}, language {language}.")
        return

    await msg.reply_text(
        f"Unknown subcommand <code>{_escape_html(sub)}</code>. Use /digest for help.",
        parse_mode="HTML",
    )


async def settopic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /settopic <url|all> [clear] — deliver a feed to the forum
    topic this command is issued in.

    Deliberately no channel binding (channels have no topics): the command
    is about "deliver where I'm typing". Inside a topic it points delivery
    there; in General, a non-forum chat, or with `clear` it returns
    delivery to the default view. Always mutating → always gated.
    """
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    args = list(context.args or [])
    if not args:
        await msg.reply_text(
            "Usage (run inside the target topic):\n"
            "/settopic &lt;url&gt; — deliver that feed to this topic\n"
            "/settopic all — deliver every feed in this group here\n"
            "/settopic &lt;url|all&gt; clear — back to General\n\n"
            "New subscriptions made inside a topic pick it up automatically.",
            parse_mode="HTML",
        )
        return

    url = args[0]
    target_all = url.lower() == "all"
    clear = len(args) > 1 and args[1].lower() == "clear"
    thread_id = None if clear else (msg.message_thread_id if msg.is_topic_message else None)
    chat_id = str(chat.id)

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        if target_all:
            count = await service.set_channel_thread("telegram", chat_id, thread_id)
            if count:
                where = "this topic" if thread_id else "the default topic (General)"
                text = f"✅ {count} subscription(s) will deliver to {where}."
            else:
                text = "No subscriptions in this chat."
        else:
            result = await service.set_feed_thread("telegram", chat_id, url, thread_id)
            text = ("✅ " if result.success else "⚠️ ") + result.message
        await session.commit()
    await msg.reply_text(text)


async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /template <url|all> [template text | reset].

    Forms:
      /template <url>            → show current template
      /template <url|all> reset  → back to the default layout
      /template <url|all> <text> → set a custom layout (multiline OK)
    """
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, fargs = resolved
    if not fargs:
        await msg.reply_text(
            "Usage:\n"
            "/template &lt;url&gt; — show current template\n"
            "/template &lt;url|all&gt; reset — back to the default layout\n"
            "/template &lt;url|all&gt; &lt;template text&gt; — set a custom layout\n\n"
            f"Placeholders: {PLACEHOLDER_LIST}\n"
            "{title}/{summary} prefer the translation; the original_/translated_ "
            "variants make bilingual layouts. **bold** and [text](url) Markdown "
            "work. A line whose placeholders all come up empty is dropped. "
            "Multiline is fine; \\n also works as a line break.",
            parse_mode="HTML",
        )
        return

    url = fargs[0]
    rest = fargs[1:]
    # Bare `/template <url>` just shows the template; set/reset mutate.
    if rest and not await _require_group_admin(update, context):
        return
    target_all = url.lower() == "all"
    session_factory = get_session_factory()

    # Show
    if not rest:
        if target_all:
            await msg.reply_text("Pass a template after all, or /template all reset to clear.")
            return
        async with session_factory() as session:
            service = SubscriptionService(session)
            detail = await service.get_subscription_detail(
                platform="telegram", channel_id=chat_id, feed_url=url, entry_limit=1
            )
        if detail is None:
            await msg.reply_text(
                f"⚠️ No subscription to <code>{_escape_html(url)}</code> in this chat.",
                parse_mode="HTML",
            )
            return
        current = detail.subscription.message_template
        if not current:
            await msg.reply_text(
                f"No template set — default layout.\nPlaceholders: {PLACEHOLDER_LIST}"
            )
            return
        await msg.reply_text(
            f"<b>Template</b>\n<pre>{_escape_html(current)}</pre>",
            parse_mode="HTML",
        )
        return

    # Clear
    if len(rest) == 1 and rest[0].lower() == "reset":
        async with session_factory() as session:
            service = SubscriptionService(session)
            if target_all:
                count = await service.set_channel_template("telegram", chat_id, None)
                text = (
                    f"✅ Template cleared on {count} subscription(s)."
                    if count
                    else "No subscriptions in this chat."
                )
            else:
                result = await service.set_feed_template("telegram", chat_id, url, None)
                text = ("✅ " if result.success else "⚠️ ") + result.message
            await session.commit()
        await msg.reply_text(text)
        return

    # Set: recover the raw text after the url token so newlines survive
    # (context.args flattens all whitespace). _resolve_target consumed the
    # command token plus possibly one channel-target token ahead of fargs.
    raw = msg.text or ""
    consumed = 1 + (len(context.args or []) - len(fargs)) + 1
    parts = raw.split(None, consumed)
    template_raw = parts[consumed] if len(parts) > consumed else ""
    normalized = normalize_template(template_raw)
    if not normalized:
        await msg.reply_text("Template is empty — see /template for usage.")
        return
    errors = validate_template(normalized)
    if errors:
        await msg.reply_text("⚠️ " + "\n".join(errors))
        return

    preview_entry = None
    preview_language: str | None = None
    async with session_factory() as session:
        service = SubscriptionService(session)
        if target_all:
            count = await service.set_channel_template("telegram", chat_id, normalized)
            if not count:
                await session.commit()
                await msg.reply_text("No subscriptions in this chat.")
                return
            header = f"Template applied to {count} subscription(s)."
        else:
            detail = await service.get_subscription_detail(
                platform="telegram", channel_id=chat_id, feed_url=url, entry_limit=1
            )
            if detail is None:
                await msg.reply_text(
                    f"⚠️ No subscription to <code>{_escape_html(url)}</code> in this chat.",
                    parse_mode="HTML",
                )
                return
            result = await service.set_feed_template("telegram", chat_id, url, normalized)
            if not result.success:
                await msg.reply_text("⚠️ " + result.message)
                return
            header = result.message
            if detail.recent_entries:
                preview_entry = detail.recent_entries[0]
            if detail.subscription.translate:
                preview_language = detail.subscription.target_language
        await session.commit()

    preview = SubscriptionService.build_template_preview(
        normalized, preview_entry, preview_language
    )
    if len(preview) > 1500:
        preview = preview[:1499] + "…"
    label = "latest entry" if preview_entry is not None else "sample data"
    reply_md = f"✅ {header}\n\nPreview ({label}):\n{preview}"

    from telegram.error import BadRequest

    try:
        await msg.reply_text(
            markdown_to_telegram_html(reply_md),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except BadRequest:
        # Converter blind spot — a plain preview beats no confirmation.
        await msg.reply_text(reply_md, disable_web_page_preview=True)


async def filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /filter <url> [show | clear | include=... exclude=...]

    Forms:
      /filter <url>                       → show current filter
      /filter <url> clear                 → remove filter
      /filter <url> include=a,b exclude=c → set filter
      /filter <url> include=a,b           → set include only
    """
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, fargs = resolved
    if not fargs:
        await msg.reply_text(
            "Usage:\n"
            "/filter &lt;url&gt; — show current filter\n"
            "/filter &lt;url&gt; clear — remove filter\n"
            "/filter &lt;url&gt; include=a,b exclude=c,d — set filter\n"
            "/filter &lt;url&gt; include=/regex/ — whole field as one regex\n\n"
            "Matching is case-insensitive on the cleaned title + summary + "
            "article body. ASCII keywords match whole words (ai no longer "
            "hits brain); CJK keywords match substrings; /.../ is a regex "
            "(no spaces — use \\s).",
            parse_mode="HTML",
        )
        return

    url = fargs[0]
    rest = fargs[1:]
    # Bare `/filter <url>` just shows the filter; the set/clear forms mutate.
    if rest and not await _require_group_admin(update, context):
        return

    session_factory = get_session_factory()

    # Show
    if not rest:
        async with session_factory() as session:
            service = SubscriptionService(session)
            rule = await service.get_feed_filter(
                platform="telegram", channel_id=chat_id, feed_url=url
            )
        if rule is None:
            await msg.reply_text(
                f"⚠️ No subscription to <code>{_escape_html(url)}</code> in this chat.",
                parse_mode="HTML",
            )
            return
        if rule.is_empty():
            await msg.reply_text("No filter set — every entry is delivered.")
            return
        lines = ["<b>Filter</b>"]
        if rule.include_regex:
            lines.append(
                f"<b>Include</b> (regex): <code>/{_escape_html(rule.include_regex)}/</code>"
            )
        elif rule.include_keywords:
            lines.append(
                "<b>Include</b> (any of): "
                + ", ".join(f"<code>{_escape_html(k)}</code>" for k in rule.include_keywords)
            )
        if rule.exclude_regex:
            lines.append(
                f"<b>Exclude</b> (regex): <code>/{_escape_html(rule.exclude_regex)}/</code>"
            )
        elif rule.exclude_keywords:
            lines.append(
                "<b>Exclude</b> (none of): "
                + ", ".join(f"<code>{_escape_html(k)}</code>" for k in rule.exclude_keywords)
            )
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
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
        await msg.reply_text(f"{prefix} {_escape_html(result.message)}", parse_mode="HTML")
        return

    # Set: parse include=... exclude=... tokens
    include_csv: str | None = None
    exclude_csv: str | None = None
    for token in rest:
        if "=" not in token:
            await msg.reply_text(
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
            await msg.reply_text(
                f"❌ Unknown key <code>{_escape_html(key)}</code>. "
                "Use <code>include=</code> or <code>exclude=</code>.",
                parse_mode="HTML",
            )
            return

    try:
        include_kw, include_re = parse_filter_field(include_csv)
        exclude_kw, exclude_re = parse_filter_field(exclude_csv)
    except ValueError as e:
        await msg.reply_text(f"❌ {_escape_html(str(e))}", parse_mode="HTML")
        return

    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.set_feed_filter(
            platform="telegram",
            channel_id=chat_id,
            feed_url=url,
            include_keywords=include_kw,
            exclude_keywords=exclude_kw,
            include_regex=include_re,
            exclude_regex=exclude_re,
        )
        await session.commit()

    prefix = "✅" if result.success else "❌"
    await msg.reply_text(f"{prefix} {_escape_html(result.message)}", parse_mode="HTML")


async def setlang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setlang <url> <code> — per-feed translation language override."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if len(args) != 2:
        await msg.reply_text(
            "Usage: /setlang <rss_url> <language_code>\n"
            "Example: /setlang https://example.com/feed zh-CN\n\n"
            "Sets the translation language for ONE feed. Use /language for "
            "the channel-wide default."
        )
        return

    url, code = args

    normalized = normalize_language_code(code)
    if normalized is None:
        await msg.reply_text(
            f"❌ <code>{_escape_html(code)}</code> doesn't look like a language "
            f"code. Try one of: {LANGUAGE_CODE_EXAMPLES}.",
            parse_mode="HTML",
        )
        return

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.set_feed_language(
            platform="telegram", channel_id=chat_id, feed_url=url, language=normalized
        )
        await session.commit()

    prefix = "✅" if result.success else "❌"
    await msg.reply_text(f"{prefix} {_escape_html(result.message)}", parse_mode="HTML")


async def settrans_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /settrans <url> <on|off> — per-feed translation toggle."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if len(args) != 2:
        await msg.reply_text(
            "Usage: /settrans <rss_url> <on|off>\n"
            "Example: /settrans https://example.com/feed off\n\n"
            "Toggles translation for ONE feed. Use /translate for the "
            "channel-wide default."
        )
        return

    url = args[0]
    enabled = args[1].lower() in ("on", "true", "yes", "1", "enable", "enabled")

    if enabled and not get_settings().can_translate():
        await msg.reply_text(
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
    await msg.reply_text(f"{prefix} {_escape_html(result.message)}", parse_mode="HTML")


async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /silent <on|off> — channel-wide silent mode toggle.

    Silent channels don't get instant feed pushes, but entries still flow
    into the digest pipeline. Use /setsilent <url> for per-feed control.
    """
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if not args:
        await msg.reply_text(
            "Usage: /silent <on|off>\n\n"
            "Channel-wide: silences every feed in this chat. Entries still "
            "go into the digest. Use /setsilent <url> <on|off> for one feed."
        )
        return

    enabled = args[0].lower() in ("on", "true", "yes", "1", "enable", "enabled")

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.set_channel_silent(
            platform="telegram", channel_id=chat_id, silent=enabled
        )
        await session.commit()

    prefix = "✅" if result.success else "❌"
    await msg.reply_text(f"{prefix} {_escape_html(result.message)}", parse_mode="HTML")


async def setsilent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setsilent <url> <on|off> — per-feed silent toggle."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if len(args) != 2:
        await msg.reply_text(
            "Usage: /setsilent <rss_url> <on|off>\n"
            "Example: /setsilent https://example.com/feed on\n\n"
            "Silent feeds don't push instant messages but still feed the "
            "digest. Use /silent for the channel-wide toggle."
        )
        return

    url = args[0]
    enabled = args[1].lower() in ("on", "true", "yes", "1", "enable", "enabled")

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.set_feed_silent(
            platform="telegram", channel_id=chat_id, feed_url=url, silent=enabled
        )
        await session.commit()

    prefix = "✅" if result.success else "❌"
    await msg.reply_text(f"{prefix} {_escape_html(result.message)}", parse_mode="HTML")


async def setdisplay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setdisplay <url> <summary|image> <on|off> — per-feed display."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    aspect = args[1].lower() if len(args) == 3 else ""
    if len(args) != 3 or aspect not in ("summary", "image"):
        await msg.reply_text(
            "Usage: /setdisplay <rss_url> <summary|image> <on|off>\n"
            "Example: /setdisplay https://example.com/feed summary off\n\n"
            "summary off = title-only compact pushes; image off = no picture."
        )
        return

    url = args[0]
    enabled = args[2].lower() in ("on", "true", "yes", "1", "enable", "enabled")

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.set_feed_display(
            platform="telegram",
            channel_id=chat_id,
            feed_url=url,
            show_summary=enabled if aspect == "summary" else None,
            show_image=enabled if aspect == "image" else None,
        )
        await session.commit()

    prefix = "✅" if result.success else "❌"
    await msg.reply_text(f"{prefix} {_escape_html(result.message)}", parse_mode="HTML")


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /export — send the subscription list as an OPML file."""
    import io

    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, _args = resolved
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        opml_xml = await service.export_opml(platform="telegram", channel_id=chat_id)

    buf = io.BytesIO(opml_xml.encode("utf-8"))
    await msg.reply_document(
        document=buf,
        filename=f"newsflow-{chat_id}.opml",
        caption="Your subscription list",
    )


async def _do_opml_import(update: Update, chat_id: str, user_id: str, opml_content: str) -> None:
    """Shared core for /import with URL and document-upload handlers."""
    msg = update.message
    if msg is None:
        return
    # Same topic capture as /add: an import run inside a forum topic
    # delivers all its feeds there.
    thread_id = msg.message_thread_id if msg.is_topic_message else None
    processing = await msg.reply_text("⏳ Importing…")

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result = await service.import_opml(
            platform="telegram",
            user_id=user_id,
            channel_id=chat_id,
            opml_content=opml_content,
            message_thread_id=thread_id,
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
            lines.append(f"• <code>{_escape_html(url[:60])}</code>: {_escape_html(err[:80])}")
        if len(result.failed) > 10:
            lines.append(f"…and {len(result.failed) - 10} more")

    await processing.edit_text("\n".join(lines), parse_mode="HTML")


async def import_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /import <url> — fetch an OPML document from a URL and import.

    File-upload imports are handled by import_document below.
    """
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None or user is None:
        return
    if not await _require_group_admin(update, context):
        return
    if not context.args:
        await msg.reply_text(
            "Usage: /import &lt;url&gt;\n\n"
            "Or upload an .opml file directly to this chat — I'll pick it up.",
            parse_mode="HTML",
        )
        return

    url = context.args[0]
    from urllib.parse import urljoin

    from newsflow.core import get_fetcher
    from newsflow.core.feed_fetcher import MAX_REDIRECTS, REDIRECT_STATUSES
    from newsflow.core.url_security import InvalidFeedURLError, validate_feed_url

    try:
        validate_feed_url(url)
    except InvalidFeedURLError as e:
        await msg.reply_text(f"❌ Rejected URL: {e}")
        return

    # Follow redirects manually, re-validating each hop, so a redirect can't
    # smuggle the OPML fetch into a private address — the same SSRF guard the
    # core feed fetcher applies.
    try:
        fetcher = get_fetcher()
        client = await fetcher._get_session()
        current = url
        content = None
        for _hop in range(MAX_REDIRECTS + 1):
            async with client.get(current, allow_redirects=False) as response:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        await msg.reply_text(
                            f"❌ Failed to fetch OPML: HTTP {response.status} "
                            "redirect without Location"
                        )
                        return
                    current = urljoin(current, location)
                    try:
                        validate_feed_url(current)
                    except InvalidFeedURLError as e:
                        await msg.reply_text(f"❌ Rejected redirect target: {e}")
                        return
                    continue
                if response.status != 200:
                    await msg.reply_text(f"❌ Failed to fetch OPML: HTTP {response.status}")
                    return
                data = await response.content.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    await msg.reply_text("❌ OPML file too large (1 MB cap)")
                    return
                content = data.decode("utf-8", errors="replace")
                break
        else:
            await msg.reply_text("❌ Failed to fetch OPML: too many redirects")
            return
    except Exception as e:
        await msg.reply_text(f"❌ Failed to fetch OPML: {e}")
        return

    await _do_opml_import(
        update,
        chat_id=str(chat.id),
        user_id=str(user.id),
        opml_content=content,
    )


async def import_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-import when a user uploads an .opml / .xml file to the chat.

    Triggered by a document filter registered in TelegramAdapter.start,
    not by /import text command — PTB's CommandHandler doesn't inspect
    captions, and requiring `/import` as caption would be error-prone UX.
    """
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user
    if msg is None or chat is None or user is None:
        return
    doc = msg.document
    if doc is None:
        return
    if not await _require_group_admin(update, context):
        return
    name = (doc.file_name or "").lower()
    if not name.endswith((".opml", ".xml")):
        return
    if doc.file_size and doc.file_size > 1024 * 1024:
        await msg.reply_text("❌ OPML file too large (1 MB cap)")
        return

    try:
        tg_file = await doc.get_file()
        data = await tg_file.download_as_bytearray()
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        await msg.reply_text("❌ OPML file is not valid UTF-8")
        return
    except Exception as e:
        await msg.reply_text(f"❌ Failed to read OPML: {e}")
        return

    await _do_opml_import(
        update,
        chat_id=str(chat.id),
        user_id=str(user.id),
        opml_content=content,
    )


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /test command - test an RSS feed."""
    msg = update.message
    if msg is None:
        return
    if not context.args:
        await msg.reply_text("Usage: /test <rss_url>")
        return

    url = context.args[0]
    processing_msg = await msg.reply_text("⏳ Testing feed...")

    from newsflow.core import get_fetcher
    from newsflow.core.source_shortcuts import expand_source_shortcut

    fetcher = get_fetcher()
    # Expand gh:/yt:/… shortcuts so /test matches what /add would actually fetch.
    result = await fetcher.fetch_feed(expand_source_shortcut(url))

    if result.success:
        desc = result.feed_description or ""
        if len(desc) > 200:
            desc = desc[:200] + "..."

        message = (
            f"✅ <b>Feed Test: Success</b>\n\n"
            f"<b>{_escape_html(result.feed_title or 'Untitled Feed')}</b>\n\n"
            f"Entries: {len(result.entries)}\n"
            f"URL: {_escape_html(url)}"
        )
        if desc:
            message += f"\n\nDescription: {_escape_html(desc)}"
    else:
        message = (
            f"❌ <b>Feed Test: Failed</b>\n\nError: "
            f"{_escape_html(result.error or 'unknown')}\n\nURL: {_escape_html(url)}"
        )

    await processing_msg.edit_text(message, parse_mode="HTML")


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /language command."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if not args:
        await msg.reply_text(
            "Usage: /language <language_code>\n\n"
            "Examples:\n"
            "/language zh-CN (Simplified Chinese)\n"
            "/language ja (Japanese)\n"
            "/language ko (Korean)\n"
            "/language en (English)"
        )
        return

    language = args[0]

    normalized = normalize_language_code(language)
    if normalized is None:
        await msg.reply_text(
            f"❌ <code>{_escape_html(language)}</code> doesn't look like a "
            f"language code. Try one of: {LANGUAGE_CODE_EXAMPLES}.",
            parse_mode="HTML",
        )
        return

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        updated = await service.update_settings(
            platform="telegram",
            channel_id=chat_id,
            target_language=normalized,
        )
        await session.commit()

    message = (
        f"✅ <b>Language Updated</b>\n\n"
        f"Translation language set to: <b>{_escape_html(normalized)}</b>\n"
        f"Saved as the channel default (new subscriptions inherit it); "
        f"{updated} existing subscription(s) updated."
    )

    await msg.reply_text(message, parse_mode="HTML")


async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /translate command."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    if not await _require_group_admin(update, context):
        return
    resolved = await _resolve_target(update, context)
    if resolved is None:
        return
    chat_id, args = resolved
    if not args:
        await msg.reply_text("Usage: /translate <on/off>")
        return

    enabled = args[0].lower() in ("on", "true", "yes", "1")

    settings = get_settings()
    if enabled and not settings.can_translate():
        await msg.reply_text(
            "⚠️ <b>Translation Not Available</b>\n\n"
            "Translation is not configured on this bot instance.\n"
            "The bot owner needs to set up translation API keys.",
            parse_mode="HTML",
        )
        return

    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        updated = await service.update_settings(
            platform="telegram",
            channel_id=chat_id,
            translate=enabled,
        )
        await session.commit()

    status = "enabled" if enabled else "disabled"
    message = (
        f"✅ <b>Translation Updated</b>\n\n"
        f"Translation <b>{status}</b> — saved as the channel default; "
        f"{updated} existing subscription(s) updated."
    )

    await msg.reply_text(message, parse_mode="HTML")


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Application-wide error handler: any exception escaping a command
    handler lands here instead of dying silently in PTB's default logger.

    Mirrors the Discord adapter's _on_app_command_error contract — the
    user always gets *some* acknowledgement instead of a bot that appears
    to have read the command and ignored it. Reply is plain text (no
    parse_mode) so this path can't itself fail on markup.
    """
    logger.error("Unhandled error in Telegram handler", exc_info=context.error)

    message = getattr(update, "effective_message", None)
    if message is None:
        return
    try:
        await message.reply_text(
            "⚠️ Something went wrong handling that command. "
            "Please check the syntax (/help) and try again."
        )
    except Exception:  # noqa: BLE001 — never let the error handler raise
        logger.exception("Failed to send error notice to user")


async def _render_status(chat_id: str) -> str:
    """Build the /status text for a chat. Shared by /status and the menu button."""
    settings = get_settings()
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        subs = await service.get_channel_subscriptions(
            platform="telegram",
            channel_id=chat_id,
            # Same counting basis as /list, which shows paused subs too —
            # otherwise the two views disagree whenever anything is paused.
            include_inactive=True,
        )
    translation_status = "Available ✅" if settings.can_translate() else "Not configured ❌"
    return (
        "📊 <b>NewsFlow Bot Status</b>\n\n"
        f"Translation: {translation_status}\n"
        f"Fetch Interval: {settings.fetch_interval_minutes} min\n"
        f"Chat Subscriptions: {len(subs)}\n"
        f"\n🕐 {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    msg = update.message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    await msg.reply_text(await _render_status(str(chat.id)), parse_mode="HTML")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route inline-keyboard button presses.

    ``list:<page>`` edits the feed list in place (pagination); ``menu:<view>``
    posts the list/status/help view as a new message. Display-only — no
    subscription state is changed here, so it's safe for any group member to
    press.
    """
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return
    data = query.data

    if data.startswith("mg:"):
        # Manage buttons answer the query themselves (toasts / alerts).
        await _on_manage_callback(query, chat, context, data)
        return

    await query.answer()

    if data.startswith("list:"):
        parts = data.split(":")
        try:
            page = int(parts[1])
        except (ValueError, IndexError):
            page = 1
        # Optional third segment: the bound channel id a private-chat
        # /list @channel targeted. Validated by shape; the buttons only
        # exist in the DM of someone who passed the admin check at /list
        # time, and the view is display-only.
        target = parts[2] if len(parts) > 2 and re.fullmatch(r"-100\d+", parts[2]) else None
        text, keyboard = await _render_list(target or str(chat.id), page, target)
        from telegram.error import BadRequest

        try:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except TypeError:
            # PTB 20.8: editing a message older than 48h (InaccessibleMessage)
            # raises TypeError("Cannot edit an inaccessible message"). Fall
            # back to a fresh message, like the menu: branch does.
            await context.bot.send_message(
                chat_id=chat.id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except BadRequest as e:
            # Double-tapping the same page → "Message is not modified"; ignore.
            if "not modified" not in str(e).lower():
                raise
        return

    if data.startswith("menu:"):
        action = data.split(":", 1)[1]
        if action == "manage":
            subs = await _load_channel_subs(str(chat.id))
            text, keyboard = _manage_list_view(subs, 1, None)
            await context.bot.send_message(
                chat_id=chat.id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        elif action == "list":
            text, keyboard = await _render_list(str(chat.id), 1)
            await context.bot.send_message(
                chat_id=chat.id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        elif action == "status":
            await context.bot.send_message(
                chat_id=chat.id,
                text=await _render_status(str(chat.id)),
                parse_mode="HTML",
            )
        elif action == "help":
            await context.bot.send_message(
                chat_id=chat.id,
                text=WELCOME_TEXT,
                parse_mode="HTML",
            )


async def _on_manage_callback(
    query: Any, chat: Any, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    """Route mg:* button presses (see the format comment at MANAGE_PAGE_SIZE).

    Navigation (list page / detail view) is display-only and open like the
    other callbacks; the mutating actions re-check admin rights on every
    press via _callback_user_may_manage — button visibility is not access
    control (anyone in a group can see the panel someone else opened).
    """
    from telegram.error import BadRequest

    async def render(text: str, keyboard: InlineKeyboardMarkup | None) -> None:
        try:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except TypeError:
            # >48h-old message (InaccessibleMessage) — send fresh instead.
            await context.bot.send_message(
                chat_id=chat.id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    parts = data.split(":")
    kind = parts[1] if len(parts) > 1 else ""

    if kind == "p":  # mg:p:<page>[:target]
        await query.answer()
        page = _int_or(parts[2] if len(parts) > 2 else "", 1)
        target = _mg_target(parts, 3)
        chat_id = target or str(chat.id)
        subs = await _load_channel_subs(chat_id)
        text, keyboard = _manage_list_view(subs, page, target)
        await render(text, keyboard)
        return

    if kind == "v":  # mg:v:<sub_id>:<page>[:target]
        await query.answer()
        sub_id = _int_or(parts[2] if len(parts) > 2 else "", 0)
        page = _int_or(parts[3] if len(parts) > 3 else "", 1)
        target = _mg_target(parts, 4)
        chat_id = target or str(chat.id)
        sub = await _load_sub(sub_id)
        if sub is None or sub.platform != "telegram" or sub.platform_channel_id != chat_id:
            subs = await _load_channel_subs(chat_id)
            text, keyboard = _manage_list_view(subs, page, target)
            await render(text, keyboard)
            return
        text, keyboard = _manage_detail_view(sub, page, target)
        await render(text, keyboard)
        return

    if kind != "a" or len(parts) < 5:
        await query.answer()
        return

    # mg:a:<action>:<sub_id>:<page>[:target]
    action = parts[2]
    sub_id = _int_or(parts[3], 0)
    page = _int_or(parts[4], 1)
    target = _mg_target(parts, 5)
    chat_id = target or str(chat.id)

    sub = await _load_sub(sub_id)
    if sub is None or sub.platform != "telegram" or sub.platform_channel_id != chat_id:
        await query.answer("That subscription no longer exists.", show_alert=True)
        subs = await _load_channel_subs(chat_id)
        text, keyboard = _manage_list_view(subs, page, target)
        await render(text, keyboard)
        return

    if not await _callback_user_may_manage(sub, chat, query.from_user, context):
        await query.answer("⛔ Only admins can use these buttons here.", show_alert=True)
        return

    if action == "rm":  # confirmation step, nothing mutated yet
        await query.answer()
        text, keyboard = _manage_confirm_view(sub, page, target)
        await render(text, keyboard)
        return

    feed_url = sub.feed.url
    session_factory = get_session_factory()
    async with session_factory() as session:
        service = SubscriptionService(session)
        result: SubscriptionActionResult | UnsubscribeResult
        if action == "pause":
            result = await service.pause_subscription("telegram", chat_id, feed_url)
        elif action == "resume":
            result = await service.resume_subscription("telegram", chat_id, feed_url)
        elif action in ("sil0", "sil1"):
            result = await service.set_feed_silent(
                "telegram", chat_id, feed_url, silent=(action == "sil1")
            )
        elif action == "rmc":
            result = await service.unsubscribe("telegram", chat_id, feed_url)
        else:
            await query.answer()
            return
        await session.commit()

    prefix = "✅ " if result.success else "❌ "
    await query.answer((prefix + result.message)[:190])

    if action == "rmc" and result.success:
        subs = await _load_channel_subs(chat_id)
        text, keyboard = _manage_list_view(subs, page, target)
        await render(text, keyboard)
        return

    fresh = await _load_sub(sub_id)
    if fresh is None:
        subs = await _load_channel_subs(chat_id)
        text, keyboard = _manage_list_view(subs, page, target)
    else:
        text, keyboard = _manage_detail_view(fresh, page, target)
    await render(text, keyboard)


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
        self.app = Application.builder().token(self.token).rate_limiter(AIORateLimiter()).build()

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
        self.app.add_handler(CommandHandler("silent", silent_command))
        self.app.add_handler(CommandHandler("setsilent", setsilent_command))
        self.app.add_handler(CommandHandler("setdisplay", setdisplay_command))
        self.app.add_handler(CommandHandler("template", template_command))
        self.app.add_handler(CommandHandler("settopic", settopic_command))
        self.app.add_handler(CommandHandler("filter", filter_command))
        self.app.add_handler(CommandHandler("digest", digest_command))
        self.app.add_handler(CommandHandler("import", import_command))
        self.app.add_handler(CommandHandler("export", export_command))
        self.app.add_handler(CommandHandler("status", status_command))
        # Inline-keyboard callbacks: /list pagination + /start quick-menu.
        self.app.add_handler(CommandHandler("manage", manage_command))
        self.app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(list|menu|mg):"))
        # Auto-import when user uploads an .opml/.xml file (no caption needed).
        self.app.add_handler(
            MessageHandler(
                filters.Document.FileExtension("opml") | filters.Document.FileExtension("xml"),
                import_document,
            )
        )
        # Without this, a handler exception is logged by PTB and the user
        # gets nothing — every failure looks like the bot ignored them.
        self.app.add_error_handler(_on_error)

        logger.info("Starting Telegram bot...")
        await self.app.initialize()
        await self.app.start()
        # Register the "/" command menu (best-effort; a failed call must not
        # abort startup). Done explicitly here because this adapter drives the
        # PTB lifecycle manually rather than via run_polling()'s post_init hook.
        try:
            await self.app.bot.set_my_commands([BotCommand(c, d) for c, d in _MENU_COMMANDS])
        except Exception:
            logger.warning("Failed to register Telegram command menu", exc_info=True)
        updater = self.app.updater
        assert updater is not None  # polling bot always has an updater
        await updater.start_polling()

        # Register adapter with dispatcher (dispatch loop is managed by main.py)
        dispatcher = get_dispatcher()
        dispatcher.register_adapter("telegram", self)
        logger.info("Telegram adapter registered with dispatcher")

        logger.info("Telegram bot started successfully")

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        global _adapter

        if self.app:
            updater = self.app.updater
            if updater is not None:
                await updater.stop()
            await self.app.stop()
            await self.app.shutdown()

        _adapter = None
        logger.info("Telegram bot stopped")

    @staticmethod
    def _is_chat_gone(e: Exception) -> bool:
        """True if a Telegram exception means the chat is permanently
        unreachable — user deleted the chat, group was nuked, or the
        bot was kicked/blocked. Matches conservatively so ambiguous
        errors (flood wait, rate limit, parse error) don't accidentally
        disable subscriptions.
        """
        from telegram.error import BadRequest, Forbidden

        msg = str(e).lower()
        if isinstance(e, BadRequest):
            # "Chat not found" = chat id invalid or deleted.
            # "PEER_ID_INVALID" = same, newer MTProto phrasing.
            return "chat not found" in msg or "peer_id_invalid" in msg
        if isinstance(e, Forbidden):
            # Standard phrasings from Bot API:
            #   "Forbidden: bot was kicked from the supergroup chat"
            #   "Forbidden: bot was blocked by the user"
            #   "Forbidden: bot is not a member of the channel chat"
            #   "Forbidden: user is deactivated"  (account deleted)
            return (
                "was kicked" in msg
                or "was blocked" in msg
                or "is not a member" in msg
                or "is deactivated" in msg
            )
        return False

    @staticmethod
    def _migrated_chat_id(e: Exception) -> str | None:
        """New chat id when `e` is Telegram's group→supergroup migration
        signal, else None. ChatMigrated subclasses TelegramError directly
        (not BadRequest/Forbidden), so _is_chat_gone never matches it —
        without this check it would fall through to the generic handler
        and the old chat id would be retried forever.
        """
        from telegram.error import ChatMigrated

        if isinstance(e, ChatMigrated):
            return str(e.new_chat_id)
        return None

    async def send_message(self, channel_id: str, message: Message) -> bool:
        """Send a message to a Telegram chat. Raises ChannelGoneError
        when the chat is permanently unreachable (deleted, bot kicked,
        bot blocked); transient errors return False so the next
        dispatch cycle retries."""
        if not self.app:
            return False

        try:
            if message.template_text is not None:
                await self._send_template_message(
                    channel_id, message.template_text, message.thread_id
                )
                return True
            text = self._format_message(message)
            await self.app.bot.send_message(
                chat_id=int(channel_id),
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=False,
                message_thread_id=message.thread_id,
            )
            return True
        except Exception as e:
            if message.thread_id is not None and _is_thread_gone(e):
                raise TopicGoneError(channel_id, message.thread_id, reason=str(e)) from e
            new_id = self._migrated_chat_id(e)
            if new_id is not None:
                raise ChannelMigratedError(channel_id, new_id, reason=str(e)) from e
            if self._is_chat_gone(e):
                raise ChannelGoneError(channel_id, reason=str(e)) from e
            logger.exception(f"Failed to send message to {channel_id}: {e}")
            return False

    async def send_text(self, channel_id: str, text: str) -> bool:
        """Send plain text to a Telegram chat. See send_message for
        the ChannelGoneError contract."""
        if not self.app:
            return False

        try:
            await self.app.bot.send_message(
                chat_id=int(channel_id),
                text=text,
            )
            return True
        except Exception as e:
            new_id = self._migrated_chat_id(e)
            if new_id is not None:
                raise ChannelMigratedError(channel_id, new_id, reason=str(e)) from e
            if self._is_chat_gone(e):
                raise ChannelGoneError(channel_id, reason=str(e)) from e
            logger.exception(f"Failed to send text to {channel_id}: {e}")
            return False

    async def send_text_pinned(self, channel_id: str, text: str) -> tuple[bool, str | None]:
        """Send text and pin the resulting message. Respects the
        `digest_auto_pin` setting — when disabled, equivalent to
        `send_text` (no pin, returns `(sent, None)`).

        Pin is silent (`disable_notification=True`) to avoid stacking a
        "bot pinned a message" system alert on top of the digest header
        itself. Pin failures degrade to "sent but not pinned": the
        digest still delivers, old pin (if any) stays in place.
        """
        if not self.app:
            return False, None
        if not get_settings().digest_auto_pin:
            sent = await self.send_text(channel_id, text)
            return sent, None

        try:
            msg = await self.app.bot.send_message(
                chat_id=int(channel_id),
                text=text,
            )
        except Exception as e:
            new_id = self._migrated_chat_id(e)
            if new_id is not None:
                raise ChannelMigratedError(channel_id, new_id, reason=str(e)) from e
            if self._is_chat_gone(e):
                raise ChannelGoneError(channel_id, reason=str(e)) from e
            logger.exception(f"Failed to send text to {channel_id}: {e}")
            return False, None

        try:
            await self.app.bot.pin_chat_message(
                chat_id=int(channel_id),
                message_id=msg.message_id,
                disable_notification=True,
            )
            return True, str(msg.message_id)
        except Exception as e:
            # Most common: BadRequest "not enough rights to pin", or the
            # bot isn't an admin in a group. Don't fail the delivery.
            logger.warning(f"Telegram pin failed in {channel_id}: {e}")
            return True, None

    async def send_digest_text(self, channel_id: str, text: str) -> bool:
        """Digest-specific send: Markdown rendered to Telegram HTML with
        link previews disabled (otherwise the last source URL grows a
        random preview card). See send_message for the ChannelGoneError /
        ChannelMigratedError contract."""
        if not self.app:
            return False
        try:
            await self._send_digest_message(channel_id, text)
            return True
        except Exception as e:
            new_id = self._migrated_chat_id(e)
            if new_id is not None:
                raise ChannelMigratedError(channel_id, new_id, reason=str(e)) from e
            if self._is_chat_gone(e):
                raise ChannelGoneError(channel_id, reason=str(e)) from e
            logger.exception(f"Failed to send digest to {channel_id}: {e}")
            return False

    async def send_digest_text_pinned(self, channel_id: str, text: str) -> tuple[bool, str | None]:
        """Digest counterpart of send_text_pinned: identical pin
        semantics, digest rendering (HTML + previews off) for the
        message itself."""
        if not self.app:
            return False, None
        if not get_settings().digest_auto_pin:
            sent = await self.send_digest_text(channel_id, text)
            return sent, None

        try:
            msg = await self._send_digest_message(channel_id, text)
        except Exception as e:
            new_id = self._migrated_chat_id(e)
            if new_id is not None:
                raise ChannelMigratedError(channel_id, new_id, reason=str(e)) from e
            if self._is_chat_gone(e):
                raise ChannelGoneError(channel_id, reason=str(e)) from e
            logger.exception(f"Failed to send digest to {channel_id}: {e}")
            return False, None

        try:
            await self.app.bot.pin_chat_message(
                chat_id=int(channel_id),
                message_id=msg.message_id,
                disable_notification=True,
            )
            return True, str(msg.message_id)
        except Exception as e:
            logger.warning(f"Telegram pin failed in {channel_id}: {e}")
            return True, None

    async def _send_digest_message(self, channel_id: str, text: str) -> TelegramMessage:
        """Render digest Markdown to HTML and send. If Telegram rejects
        the rendered entities (converter blind spot or pathological LLM
        output), fall back to the raw text — a plain-looking digest
        beats a lost one. Previews stay off in both paths."""
        assert self.app is not None
        from telegram.error import BadRequest

        html = markdown_to_telegram_html(text)
        try:
            sent: TelegramMessage = await self.app.bot.send_message(
                chat_id=int(channel_id),
                text=html,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except BadRequest as e:
            if "parse entities" not in str(e).lower():
                raise
            logger.warning(
                f"Digest HTML rejected by Telegram for {channel_id}; "
                f"falling back to plain text: {e}"
            )
            sent = await self.app.bot.send_message(
                chat_id=int(channel_id),
                text=text,
                disable_web_page_preview=True,
            )
        return sent

    async def _send_template_message(
        self, channel_id: str, template_text: str, thread_id: int | None = None
    ) -> None:
        """Send a template-rendered entry: Markdown → Telegram HTML with a
        plain-text fallback when Telegram rejects the entities. Link
        previews stay ON, matching the default entry layout. Raises on
        chat-level failures so send_message's gone/migrated/topic handling
        applies unchanged."""
        assert self.app is not None
        from telegram.error import BadRequest

        text = template_text
        if len(text) > 3500:
            text = text[:3499] + "…"
        html = markdown_to_telegram_html(text)
        if len(html) > 4096:
            # Entity escaping can outgrow Telegram's hard cap even when the
            # Markdown fits; the trimmed plain text always fits.
            await self.app.bot.send_message(
                chat_id=int(channel_id),
                text=text,
                disable_web_page_preview=False,
                message_thread_id=thread_id,
            )
            return
        try:
            await self.app.bot.send_message(
                chat_id=int(channel_id),
                text=html,
                parse_mode="HTML",
                disable_web_page_preview=False,
                message_thread_id=thread_id,
            )
        except BadRequest as e:
            if "parse entities" not in str(e).lower():
                raise
            logger.warning(
                f"Template HTML rejected by Telegram for {channel_id}; "
                f"falling back to plain text: {e}"
            )
            await self.app.bot.send_message(
                chat_id=int(channel_id),
                text=text,
                disable_web_page_preview=False,
                message_thread_id=thread_id,
            )

    async def unpin_message(self, channel_id: str, message_id: str) -> bool:
        """Unpin a specific message. Returns False on error; the caller
        should treat that as "old pin might still be there" and move on.
        """
        if not self.app:
            return False
        try:
            await self.app.bot.unpin_chat_message(
                chat_id=int(channel_id),
                message_id=int(message_id),
            )
            return True
        except Exception as e:
            logger.warning(f"Telegram unpin failed for message {message_id} in {channel_id}: {e}")
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

        # Link needs HTML-escape too: RSS URLs often contain `&` in query
        # strings, which Telegram's HTML parser rejects as an invalid entity
        # and fails the whole message send.
        parts.extend(
            [
                f'🔗 <a href="{self._escape_html(message.link)}">Read more</a>',
                f"📰 {self._escape_html(message.source)}",
            ]
        )

        if message.published_at:
            parts.append(f"🕐 {message.published_at.strftime('%Y-%m-%d %H:%M')}")

        return "\n".join(parts)

    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
