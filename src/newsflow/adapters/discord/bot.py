"""
Discord bot adapter.

Implements Discord-specific functionality using discord.py.
Uses Slash Commands (Application Commands) as recommended by Discord.
"""

import logging
import re
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from newsflow.adapters.base import BaseAdapter, ChannelGoneError, Message
from newsflow.config import get_settings
from newsflow.core.filter import parse_filter_field
from newsflow.core.languages import LANGUAGE_CODE_EXAMPLES, normalize_language_code
from newsflow.core.message_template import (
    PLACEHOLDER_LIST,
    normalize_template,
    validate_template,
)
from newsflow.core.timeutil import relative_time, time_until
from newsflow.core.timezones import local_schedule_to_utc, parse_timezone
from newsflow.models.base import get_session_factory
from newsflow.models.subscription import Subscription
from newsflow.services import SubscriptionService, get_dispatcher

# Max subscriptions per /feed list page. Discord embed description caps
# at 4096 chars; 20 entries × ~150 chars each leaves comfortable headroom.
LIST_PAGE_SIZE = 20

# Discord caps an autocomplete response at 25 choices and each choice's
# name/value at 100 chars. discord.py enforces neither — an oversized
# response is rejected wholesale by the API — so the callback must stay
# within both limits itself.
AUTOCOMPLETE_MAX_CHOICES = 25
AUTOCOMPLETE_MAX_LEN = 100

# Stored subscription.mention shapes — always produced from a native
# Role/User option pick, so anything else is treated as "ping nobody".
_ROLE_MENTION_RE = re.compile(r"^<@&(\d+)>$")
_USER_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def _mention_allowance(mention: str) -> discord.AllowedMentions:
    """AllowedMentions permitting exactly the configured mention target.

    The client-wide default is AllowedMentions.none() — feed-controlled
    text (titles/summaries flowing through templates as plain content)
    must never be able to ping. Delivery re-enables just the one
    role/user the channel explicitly configured.
    """
    match = _ROLE_MENTION_RE.match(mention)
    if match:
        return discord.AllowedMentions(
            everyone=False, users=False, roles=[discord.Object(int(match.group(1)))]
        )
    match = _USER_MENTION_RE.match(mention)
    if match:
        return discord.AllowedMentions(
            everyone=False, users=[discord.Object(int(match.group(1)))], roles=False
        )
    return discord.AllowedMentions.none()


def _sub_status_chip(sub: Subscription) -> str | None:
    """Return a one-line status chip when the sub needs user attention, else None.

    Priority: user-paused > feed auto-disabled > feed errored > silent.
    Faults outrank silent because they're actionable; silent is a
    deliberate user choice and only worth showing when nothing else is.
    Healthy non-silent subs get no chip to keep the list uncluttered.
    """
    feed = sub.feed
    if not sub.is_active:
        return "⏸ paused"
    if not feed.is_active:
        return "🛑 auto-disabled (too many errors)"
    if feed.error_count > 0:
        return f"⚠️ {feed.error_count} errors, retry {time_until(feed.next_retry_at)}"
    if sub.silent:
        return "🔇 silent (digest only)"
    return None


def _format_sub_line(sub: Subscription) -> str:
    """Format one subscription for the /feed list description."""
    feed = sub.feed
    title = feed.title or "Untitled"
    parts = [f"🌐 {sub.target_language}" if sub.translate else "📰 no translate"]
    chip = _sub_status_chip(sub)
    if chip:
        parts.append(chip)
    meta = " · ".join(parts)
    return f"**{title}** · {meta}\n{feed.url}"


def _build_import_embed(result) -> discord.Embed:  # type: ignore[no-untyped-def]
    """Summary embed for /feed import."""
    added = len(result.added)
    existing = len(result.already_subscribed)
    failed = len(result.failed)
    color = (
        discord.Color.green()
        if added and not failed
        else (discord.Color.orange() if added or existing else discord.Color.red())
    )
    lines = [
        f"✅ Added: **{added}**",
        f"⏭️ Already subscribed: **{existing}**",
        f"❌ Failed: **{failed}**",
    ]
    embed = discord.Embed(
        title="OPML Import Result",
        description="\n".join(lines),
        color=color,
    )
    if result.failed:
        fail_lines = []
        for url, err in result.failed[:10]:
            fail_lines.append(f"• `{url[:60]}` — {err[:80]}")
        if len(result.failed) > 10:
            fail_lines.append(f"…and {len(result.failed) - 10} more")
        value = "\n".join(fail_lines)
        embed.add_field(name="Failures", value=value[:1024], inline=False)
    return embed


def _build_status_embed(detail) -> discord.Embed:  # type: ignore[no-untyped-def]
    """Build the /feed status embed from a SubscriptionDetail."""
    sub = detail.subscription
    feed = detail.feed

    if not sub.is_active:
        state = "⏸ Paused"
        color = discord.Color.orange()
    elif not feed.is_active:
        state = "🛑 Auto-disabled (10+ consecutive errors)"
        color = discord.Color.red()
    elif feed.error_count > 0:
        state = f"⚠️ {feed.error_count} errors — retry {time_until(feed.next_retry_at)}"
        color = discord.Color.gold()
    else:
        state = "✅ Healthy"
        color = discord.Color.green()

    embed = discord.Embed(
        title=feed.title or "Untitled Feed",
        url=feed.url,
        description=feed.description[:300] + "…"
        if feed.description and len(feed.description) > 300
        else (feed.description or ""),
        color=color,
    )
    embed.add_field(name="State", value=state, inline=False)
    embed.add_field(
        name="Backlog",
        value=f"{detail.unsent_count} queued for this channel",
        inline=True,
    )
    embed.add_field(
        name="Translation",
        value=f"{'On' if sub.translate else 'Off'} ({sub.target_language})",
        inline=True,
    )
    embed.add_field(
        name="Last Successful Fetch",
        value=relative_time(feed.last_successful_fetch_at),
        inline=True,
    )
    embed.add_field(
        name="Last Fetch Attempt",
        value=relative_time(feed.last_fetched_at),
        inline=True,
    )
    if feed.last_error and feed.error_count > 0:
        err = feed.last_error
        if len(err) > 200:
            err = err[:200] + "…"
        embed.add_field(name="Last Error", value=err, inline=False)

    if detail.recent_entries:
        lines = []
        for entry in detail.recent_entries:
            ts = relative_time(entry.published_at) if entry.published_at else ""
            title_line = entry.title[:80] + ("…" if len(entry.title) > 80 else "")
            lines.append(f"• [{title_line}]({entry.link})" + (f" — {ts}" if ts else ""))
        val = "\n".join(lines)
        if len(val) > 1024:
            val = val[:1020] + "…"
        embed.add_field(name="Recent Articles", value=val, inline=False)

    return embed


logger = logging.getLogger(__name__)


async def _on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    """Central handler for exceptions raised inside slash-command callbacks.

    discord.py routes command-callback exceptions to ``CommandTree.on_error``,
    NOT to ``Client.on_error`` (which only covers event handlers), and its
    default implementation merely logs them. Every command in this adapter
    calls ``interaction.response.defer(ephemeral=True)`` up front, so an
    unhandled exception would otherwise leave the user staring at a perpetual
    "thinking…" with no feedback. Log the root cause and send a short
    ephemeral apology instead.
    """
    # CommandInvokeError wraps the real exception raised in the callback;
    # unwrap it so the log shows the actual cause, not the discord.py wrapper.
    original = getattr(error, "original", None) or error
    command = interaction.command.qualified_name if interaction.command else "?"
    logger.error("Unhandled error in /%s", command, exc_info=original)

    notice = "⚠️ Something went wrong running that command. Please try again later."
    try:
        # Normal path: the command already deferred, so the only way to reach
        # the user is a followup. Fall back to an initial response for the rare
        # command that errors before deferring.
        if interaction.response.is_done():
            await interaction.followup.send(notice, ephemeral=True)
        else:
            await interaction.response.send_message(notice, ephemeral=True)
    except discord.HTTPException:
        # The interaction token may have expired (15-min cap) or the channel
        # may be gone. The root cause is already logged; nothing more to do.
        logger.debug("Could not deliver error notice for /%s", command)


class NewsFlowBot(commands.Bot):
    """
    Discord bot with slash commands.
    """

    def __init__(self) -> None:
        # Default (non-privileged) intents only. This bot is slash-command
        # only — app commands arrive as interactions, which no intent gates —
        # so requesting message_content would force every operator through
        # the Developer Portal's privileged-intent toggle for nothing, and
        # crash-loop the process (PrivilegedIntentsRequired) when they
        # inevitably don't know to flip it.
        intents = discord.Intents.default()

        super().__init__(
            # No prefix commands are registered; when_mentioned is an inert
            # placeholder that also suppresses discord.py's "message content
            # intent is missing" startup warning (string prefixes trigger it).
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            # Ping-safe baseline: nothing pings unless a send explicitly
            # re-enables it. Feed titles/summaries flow into plain message
            # content on the template path, so an article containing
            # "@everyone" must stay inert; /feed mention deliveries pass
            # a per-send allowance for exactly the configured target.
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.settings = get_settings()

    async def setup_hook(self) -> None:
        """Called when the bot is ready to setup."""
        # Add cogs
        await self.add_cog(FeedCommands(self))
        await self.add_cog(SettingsCommands(self))
        await self.add_cog(DigestCommands(self))

        # Route every slash-command exception through one handler so failures
        # surface to the user instead of hanging on the deferred response.
        # (Client.on_error below only covers event handlers, not app commands.)
        self.tree.error(_on_app_command_error)

        # Sync slash commands
        logger.info("Syncing slash commands...")
        await self.tree.sync()
        logger.info("Slash commands synced")

    async def on_ready(self) -> None:
        """Called when bot is ready."""
        assert self.user is not None  # populated once the client has logged in
        logger.info(f"Discord bot logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guilds")

        # Set status
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="RSS feeds",
            )
        )

        # Register adapter with dispatcher (dispatch loop is managed by main.py)
        dispatcher = get_dispatcher()
        adapter = DiscordAdapter(self)
        dispatcher.register_adapter("discord", adapter)
        logger.info("Discord adapter registered with dispatcher")

    async def on_error(self, event: str, *args: object, **kwargs: object) -> None:
        """Handle errors."""
        logger.exception(f"Error in {event}")


class FeedCommands(commands.Cog):
    """Feed management commands."""

    def __init__(self, bot: NewsFlowBot) -> None:
        self.bot = bot

    # Native permission gate: without it any member could remove feeds,
    # silence the channel, or rewrite filters. Discord can't set permissions
    # per SUBcommand, so the whole group defaults to Manage Server; server
    # admins can re-grant it per role/channel under Server Settings →
    # Integrations. DMs are unaffected (no member permissions there).
    feed_group = app_commands.Group(
        name="feed",
        description="Manage RSS feeds",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @feed_group.command(name="add", description="Add an RSS feed to this channel")
    @app_commands.describe(url="The RSS feed URL to add")
    async def feed_add(self, interaction: discord.Interaction, url: str) -> None:
        """Add a new RSS feed."""
        await interaction.response.defer(ephemeral=True)

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)

            result = await service.subscribe(
                platform="discord",
                user_id=str(interaction.user.id),
                channel_id=str(interaction.channel_id),
                feed_url=url,
                guild_id=str(interaction.guild_id) if interaction.guild_id else None,
            )

            await session.commit()

        # Deliver a preview entry in the background so the user sees content
        # without waiting a full fetch interval. spawn() keeps a strong ref
        # so the event loop can't GC the task mid-flight.
        if result.success and result.is_new and result.subscription:
            dispatcher = get_dispatcher()
            dispatcher.spawn(
                dispatcher.schedule_preview(result.subscription.id),
                name=f"preview:discord:{result.subscription.id}",
            )

        if result.success:
            assert result.feed is not None
            embed = discord.Embed(
                title="Feed Added",
                description=f"**{result.feed.title or url}**",
                color=discord.Color.green(),
            )
            embed.add_field(name="URL", value=url, inline=False)
            if result.is_new:
                embed.add_field(name="Status", value="New subscription created", inline=False)
            else:
                embed.add_field(name="Status", value=result.message, inline=False)
        else:
            embed = discord.Embed(
                title="Failed to Add Feed",
                description=result.message,
                color=discord.Color.red(),
            )
            embed.add_field(name="URL", value=url, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(name="remove", description="Remove an RSS feed from this channel")
    @app_commands.describe(url="The RSS feed URL to remove")
    async def feed_remove(self, interaction: discord.Interaction, url: str) -> None:
        """Remove an RSS feed."""
        await interaction.response.defer(ephemeral=True)

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)

            result = await service.unsubscribe(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
            )

            await session.commit()

        if result.success:
            embed = discord.Embed(
                title="Feed Removed",
                description=result.message,
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="Failed to Remove Feed",
                description=result.message,
                color=discord.Color.red(),
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(name="list", description="List RSS feeds in this channel")
    @app_commands.describe(page="Page number (20 feeds per page)")
    async def feed_list(self, interaction: discord.Interaction, page: int = 1) -> None:
        """List feeds for this channel, paginated."""
        await interaction.response.defer(ephemeral=True)

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            subscriptions = list(
                await service.get_channel_subscriptions(
                    platform="discord",
                    channel_id=str(interaction.channel_id),
                    # Paused subs must stay listed (with the ⏸ chip) or their
                    # URLs become unfindable and /feed resume impossible.
                    include_inactive=True,
                )
            )

        if not subscriptions:
            embed = discord.Embed(
                title="Subscribed Feeds",
                description="No feeds subscribed yet.\nUse `/feed add <url>` to add one.",
                color=discord.Color.blue(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        total = len(subscriptions)
        total_pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * LIST_PAGE_SIZE
        page_subs = subscriptions[start : start + LIST_PAGE_SIZE]

        description = "\n\n".join(_format_sub_line(sub) for sub in page_subs)

        embed = discord.Embed(
            title=f"Subscribed Feeds ({total})",
            description=description,
            color=discord.Color.blue(),
        )
        if total_pages > 1:
            embed.set_footer(
                text=(
                    f"Page {page}/{total_pages}"
                    + (f" — /feed list page:{page + 1} for next" if page < total_pages else "")
                )
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(name="pause", description="Stop delivering from this feed")
    @app_commands.describe(url="The RSS feed URL to pause")
    async def feed_pause(self, interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.pause_subscription(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
            )
            await session.commit()

        embed = discord.Embed(
            title="Paused" if result.success else "Failed to Pause",
            description=result.message,
            color=discord.Color.orange() if result.success else discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(name="resume", description="Resume delivery from a paused feed")
    @app_commands.describe(url="The RSS feed URL to resume, or 'all' for every paused feed")
    async def feed_resume(self, interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            if url.strip().lower() == "all":
                result = await service.resume_all_subscriptions(
                    platform="discord",
                    channel_id=str(interaction.channel_id),
                )
            else:
                result = await service.resume_subscription(
                    platform="discord",
                    channel_id=str(interaction.channel_id),
                    feed_url=url,
                )
            await session.commit()

        embed = discord.Embed(
            title="Resumed" if result.success else "Failed to Resume",
            description=result.message,
            color=discord.Color.green() if result.success else discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(
        name="silent",
        description="Don't push instant messages for this feed; entries still flow into the digest",
    )
    @app_commands.describe(
        url="The RSS feed URL",
        enabled="True = silent (digest only), False = back to instant push",
    )
    async def feed_silent(self, interaction: discord.Interaction, url: str, enabled: bool) -> None:
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.set_feed_silent(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
                silent=enabled,
            )
            await session.commit()

        embed = discord.Embed(
            title="Silent Mode Updated" if result.success else "Failed to Update",
            description=result.message,
            color=discord.Color.blurple() if result.success else discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(
        name="display",
        description="Per-feed display: hide the summary (title-only) and/or the image",
    )
    @app_commands.describe(
        url="The RSS feed URL",
        summary="Show the entry summary (False = title-only compact mode)",
        image="Show the entry image",
    )
    async def feed_display(
        self,
        interaction: discord.Interaction,
        url: str,
        summary: bool | None = None,
        image: bool | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if summary is None and image is None:
            await interaction.followup.send(
                "Nothing to change — pass `summary` and/or `image`.", ephemeral=True
            )
            return
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.set_feed_display(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
                show_summary=summary,
                show_image=image,
            )
            await session.commit()

        embed = discord.Embed(
            title="Display Updated" if result.success else "Failed to Update",
            description=result.message,
            color=discord.Color.blurple() if result.success else discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(
        name="template",
        description="Custom message layout: {title} {summary} {url} … placeholders, \\n for line break",
    )
    @app_commands.describe(
        url="The RSS feed URL, or 'all' to apply to every feed in this channel",
        template="Template text (\\n = line break). Omit to show the current template",
        reset="Clear the template and return to the default layout",
    )
    async def feed_template(
        self,
        interaction: discord.Interaction,
        url: str,
        template: str | None = None,
        reset: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        channel_id = str(interaction.channel_id)
        target_all = url.strip().lower() == "all"

        async def _reply(text: str) -> None:
            await interaction.followup.send(text, ephemeral=True)

        if reset:
            async with session_factory() as session:
                service = SubscriptionService(session)
                if target_all:
                    count = await service.set_channel_template("discord", channel_id, None)
                    text = (
                        f"✅ Template cleared on {count} subscription(s)."
                        if count
                        else "No subscriptions in this channel."
                    )
                else:
                    result = await service.set_feed_template("discord", channel_id, url, None)
                    text = ("✅ " if result.success else "⚠️ ") + result.message
                await session.commit()
            await _reply(text)
            return

        if template is None:
            # Show the current template.
            if target_all:
                await _reply("Pass `template` to apply one to every feed, or `reset: True`.")
                return
            async with session_factory() as session:
                service = SubscriptionService(session)
                detail = await service.get_subscription_detail(
                    platform="discord", channel_id=channel_id, feed_url=url, entry_limit=1
                )
            if detail is None:
                await _reply("No subscription to that URL in this channel.")
                return
            current = detail.subscription.message_template
            if not current:
                await _reply(
                    "No template set — default layout.\n"
                    f"Placeholders: `{PLACEHOLDER_LIST}`\n"
                    r"Example: `📌 **{title}**\n{summary}\n🔗 {url}`"
                )
                return
            # Show \n-escaped so the text can be pasted straight back into
            # this command's single-line option box.
            shown = current.replace("\n", "\\n")
            await _reply(f"Current template:\n```\n{shown}\n```")
            return

        normalized = normalize_template(template)
        if not normalized:
            await _reply("Template is empty — pass text, or use `reset: True` to clear.")
            return
        errors = validate_template(normalized)
        if errors:
            await _reply("⚠️ " + "\n".join(errors))
            return

        preview_entry = None
        preview_language: str | None = None
        async with session_factory() as session:
            service = SubscriptionService(session)
            if target_all:
                count = await service.set_channel_template("discord", channel_id, normalized)
                if not count:
                    await session.commit()
                    await _reply("No subscriptions in this channel.")
                    return
                header = f"Template applied to {count} subscription(s)."
            else:
                detail = await service.get_subscription_detail(
                    platform="discord", channel_id=channel_id, feed_url=url, entry_limit=1
                )
                if detail is None:
                    await _reply("No subscription to that URL in this channel.")
                    return
                result = await service.set_feed_template("discord", channel_id, url, normalized)
                if not result.success:
                    await _reply("⚠️ " + result.message)
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
        await _reply(f"✅ {header}\n\n**Preview** ({label}):\n{preview}")

    @feed_group.command(
        name="mention",
        description="Ping a role or user whenever this feed posts (or 'all' feeds)",
    )
    @app_commands.describe(
        url="The RSS feed URL, or 'all' for every feed in this channel",
        target="Role or user to ping on each new entry",
        clear="Remove the mention",
    )
    async def feed_mention(
        self,
        interaction: discord.Interaction,
        url: str,
        target: discord.Role | discord.Member | discord.User | None = None,
        clear: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        channel_id = str(interaction.channel_id)
        target_all = url.strip().lower() == "all"

        async def _reply(text: str) -> None:
            await interaction.followup.send(text, ephemeral=True)

        if clear:
            async with session_factory() as session:
                service = SubscriptionService(session)
                if target_all:
                    count = await service.set_channel_mention("discord", channel_id, None)
                    text = (
                        f"✅ Mention cleared on {count} subscription(s)."
                        if count
                        else "No subscriptions in this channel."
                    )
                else:
                    result = await service.set_feed_mention("discord", channel_id, url, None)
                    text = ("✅ " if result.success else "⚠️ ") + result.message
                await session.commit()
            await _reply(text)
            return

        if target is None:
            # Show the current mention.
            if target_all:
                await _reply("Pass `target` to set a mention on every feed, or `clear: True`.")
                return
            async with session_factory() as session:
                service = SubscriptionService(session)
                detail = await service.get_subscription_detail(
                    platform="discord", channel_id=channel_id, feed_url=url, entry_limit=1
                )
            if detail is None:
                await _reply("No subscription to that URL in this channel.")
                return
            current = detail.subscription.mention
            if not current:
                await _reply("No mention set — new entries don't ping anyone.")
                return
            # Ephemeral + the client-wide AllowedMentions.none() default:
            # echoing the mention renders the chip without pinging.
            await _reply(f"Current mention: {current} (pinged with every new entry)")
            return

        mention_str = target.mention
        async with session_factory() as session:
            service = SubscriptionService(session)
            if target_all:
                count = await service.set_channel_mention("discord", channel_id, mention_str)
                if not count:
                    await session.commit()
                    await _reply("No subscriptions in this channel.")
                    return
                text = (
                    f"✅ Mention applied to {count} subscription(s) — "
                    f"new entries will start with {mention_str}."
                )
            else:
                result = await service.set_feed_mention("discord", channel_id, url, mention_str)
                text = ("✅ " if result.success else "⚠️ ") + result.message
                if result.success:
                    text += f"\nNew entries will start with {mention_str}."
            await session.commit()
        await _reply(text)

    @feed_group.command(name="status", description="Detailed status of one feed in this channel")
    @app_commands.describe(url="The RSS feed URL")
    async def feed_status(self, interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            detail = await service.get_subscription_detail(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
            )

        if detail is None:
            embed = discord.Embed(
                title="Feed Not Found",
                description=f"No subscription to `{url}` in this channel.",
                color=discord.Color.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = _build_status_embed(detail)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(
        name="language",
        description="Set translation language for ONE feed (overrides channel default)",
    )
    @app_commands.describe(
        url="The RSS feed URL",
        code="Target language code (e.g. zh-CN, ja, ko, en)",
    )
    async def feed_language(
        self,
        interaction: discord.Interaction,
        url: str,
        code: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        normalized = normalize_language_code(code)
        if normalized is None:
            await interaction.followup.send(
                f"⚠️ `{code}` doesn't look like a language code. "
                f"Try one of: {LANGUAGE_CODE_EXAMPLES}.",
                ephemeral=True,
            )
            return

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.set_feed_language(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
                language=normalized,
            )
            await session.commit()

        embed = discord.Embed(
            title="Language Updated" if result.success else "Failed",
            description=result.message,
            color=discord.Color.green() if result.success else discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(
        name="translate",
        description="Toggle translation for ONE feed (overrides channel default)",
    )
    @app_commands.describe(
        url="The RSS feed URL",
        enabled="Whether to translate this feed",
    )
    async def feed_translate(
        self,
        interaction: discord.Interaction,
        url: str,
        enabled: bool,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.set_feed_translate(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
                enabled=enabled,
            )
            await session.commit()

        embed = discord.Embed(
            title="Translation Updated" if result.success else "Failed",
            description=result.message,
            color=discord.Color.green() if result.success else discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(
        name="export",
        description="Download this channel's subscriptions as an OPML file",
    )
    async def feed_export(self, interaction: discord.Interaction) -> None:
        import io

        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            opml_xml = await service.export_opml(
                platform="discord",
                channel_id=str(interaction.channel_id),
            )

        buf = io.BytesIO(opml_xml.encode("utf-8"))
        filename = f"newsflow-{interaction.channel_id}.opml"
        file = discord.File(buf, filename=filename)
        await interaction.followup.send(
            content="Subscriptions export attached:",
            file=file,
            ephemeral=True,
        )

    @feed_group.command(
        name="filter-set",
        description="Set a keyword or /regex/ filter for one feed",
    )
    @app_commands.describe(
        url="The RSS feed URL",
        include="Keep only matching entries: keywords (csv) or /regex/. Blank = no include filter.",
        exclude="Drop matching entries: keywords (csv) or /regex/. Blank = no exclude filter.",
    )
    async def feed_filter_set(
        self,
        interaction: discord.Interaction,
        url: str,
        include: str = "",
        exclude: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            include_kw, include_re = parse_filter_field(include)
            exclude_kw, exclude_re = parse_filter_field(exclude)
        except ValueError as e:
            embed = discord.Embed(
                title="Failed",
                description=f"⚠️ {e}",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.set_feed_filter(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
                include_keywords=include_kw,
                exclude_keywords=exclude_kw,
                include_regex=include_re,
                exclude_regex=exclude_re,
            )
            await session.commit()

        embed = discord.Embed(
            title="Filter Updated" if result.success else "Failed",
            description=result.message,
            color=discord.Color.green() if result.success else discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(
        name="filter-show",
        description="Show the current keyword filter on one feed",
    )
    @app_commands.describe(url="The RSS feed URL")
    async def feed_filter_show(self, interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            rule = await service.get_feed_filter(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
            )

        if rule is None:
            embed = discord.Embed(
                title="Filter",
                description=f"No subscription to `{url}` in this channel.",
                color=discord.Color.orange(),
            )
        elif rule.is_empty():
            embed = discord.Embed(
                title="Filter",
                description="No filter set — every entry is delivered.",
                color=discord.Color.blue(),
            )
        else:
            lines = []
            if rule.include_regex:
                lines.append(f"**Include** (regex): `/{rule.include_regex}/`")
            elif rule.include_keywords:
                lines.append(
                    "**Include** (any of): " + ", ".join(f"`{k}`" for k in rule.include_keywords)
                )
            if rule.exclude_regex:
                lines.append(f"**Exclude** (regex): `/{rule.exclude_regex}/`")
            elif rule.exclude_keywords:
                lines.append(
                    "**Exclude** (none of): " + ", ".join(f"`{k}`" for k in rule.exclude_keywords)
                )
            embed = discord.Embed(
                title="Filter",
                description="\n".join(lines),
                color=discord.Color.blue(),
            )
            embed.set_footer(
                text="Case-insensitive on cleaned title + summary + body. ASCII keywords "
                "match whole words; CJK matches substrings; /…/ is a regex"
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(
        name="filter-clear",
        description="Remove the keyword filter from one feed",
    )
    @app_commands.describe(url="The RSS feed URL")
    async def feed_filter_clear(self, interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.clear_feed_filter(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
            )
            await session.commit()

        embed = discord.Embed(
            title="Filter Cleared" if result.success else "Failed",
            description=result.message,
            color=discord.Color.green() if result.success else discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(
        name="import",
        description="Bulk-subscribe from an OPML file (from Feedly / Reeder / etc.)",
    )
    @app_commands.describe(
        file="The OPML file (.opml or .xml). Must be UTF-8 and under 1 MB.",
    )
    async def feed_import(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # Validate up-front, cheap checks before the network read.
        if not file.filename.lower().endswith((".opml", ".xml")):
            await interaction.followup.send(
                f"⚠️ Expected a .opml or .xml file, got `{file.filename}`.",
                ephemeral=True,
            )
            return
        if file.size and file.size > 1024 * 1024:
            await interaction.followup.send("⚠️ OPML file too large (1 MB cap).", ephemeral=True)
            return

        try:
            content = (await file.read()).decode("utf-8")
        except UnicodeDecodeError:
            await interaction.followup.send("⚠️ OPML file is not valid UTF-8.", ephemeral=True)
            return

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.import_opml(
                platform="discord",
                user_id=str(interaction.user.id),
                channel_id=str(interaction.channel_id),
                opml_content=content,
                guild_id=(str(interaction.guild_id) if interaction.guild_id else None),
            )
            await session.commit()

        embed = _build_import_embed(result)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(name="test", description="Test an RSS feed URL")
    @app_commands.describe(url="The RSS feed URL to test")
    async def feed_test(self, interaction: discord.Interaction, url: str) -> None:
        """Test if a feed URL is valid."""
        await interaction.response.defer(ephemeral=True)

        from newsflow.core import get_fetcher
        from newsflow.core.source_shortcuts import expand_source_shortcut

        fetcher = get_fetcher()
        # Expand gh:/yt:/… shortcuts so /test matches what /add would fetch.
        result = await fetcher.fetch_feed(expand_source_shortcut(url))

        if result.success:
            embed = discord.Embed(
                title="Feed Test: Success",
                description=f"**{result.feed_title or 'Untitled Feed'}**",
                color=discord.Color.green(),
            )
            embed.add_field(name="URL", value=url, inline=False)
            embed.add_field(name="Entries", value=str(len(result.entries)), inline=True)
            if result.feed_description:
                desc = result.feed_description
                if len(desc) > 200:
                    desc = desc[:200] + "..."
                embed.add_field(name="Description", value=desc, inline=False)
        else:
            embed = discord.Embed(
                title="Feed Test: Failed",
                description=f"Could not fetch feed: {result.error}",
                color=discord.Color.red(),
            )
            embed.add_field(name="URL", value=url, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_remove.autocomplete("url")
    @feed_pause.autocomplete("url")
    @feed_resume.autocomplete("url")
    @feed_silent.autocomplete("url")
    @feed_display.autocomplete("url")
    @feed_template.autocomplete("url")
    @feed_mention.autocomplete("url")
    @feed_status.autocomplete("url")
    @feed_language.autocomplete("url")
    @feed_translate.autocomplete("url")
    @feed_filter_set.autocomplete("url")
    @feed_filter_show.autocomplete("url")
    @feed_filter_clear.autocomplete("url")
    async def _url_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Suggest this channel's subscribed feed URLs for `url` parameters.

        The suggestion value is the STORED feed URL — what every per-feed
        command matches on — so users never retype a URL that add-time
        discovery may have rewritten (an HTML page URL is stored as its
        advertised feed URL). Deliberately not wired to /feed add or
        /feed test: both take URLs that are new by nature.

        Command-aware: pause suggests only active subs, resume only paused
        ones (plus the literal 'all'). Best-effort — the tree suppresses
        autocomplete exceptions instead of routing them to the command
        error handler, so any failure degrades to an empty list here.
        Choices are suggestions only; users can still paste anything,
        which also covers URLs too long for a choice value.
        """
        try:
            command = interaction.command.name if interaction.command else ""
            session_factory = get_session_factory()
            async with session_factory() as session:
                service = SubscriptionService(session)
                subs = await service.get_channel_subscriptions(
                    platform="discord",
                    channel_id=str(interaction.channel_id),
                    include_inactive=True,
                )

            if command == "pause":
                subs = [s for s in subs if s.is_active]
            elif command == "resume":
                subs = [s for s in subs if not s.is_active]

            needle = current.strip().lower()
            choices: list[app_commands.Choice[str]] = []
            if command == "resume" and subs and needle in "all":
                choices.append(
                    app_commands.Choice(name="all — resume every paused feed", value="all")
                )
            elif command in ("template", "mention") and subs and needle in "all":
                choices.append(
                    app_commands.Choice(
                        name="all — apply to every feed in this channel", value="all"
                    )
                )

            for sub in subs:
                url = sub.feed.url
                if len(url) > AUTOCOMPLETE_MAX_LEN:
                    continue
                title = sub.feed.title or "Untitled"
                if needle and needle not in title.lower() and needle not in url.lower():
                    continue
                name = f"{title} · {url}"
                if len(name) > AUTOCOMPLETE_MAX_LEN:
                    name = name[: AUTOCOMPLETE_MAX_LEN - 1] + "…"
                choices.append(app_commands.Choice(name=name, value=url))
                if len(choices) >= AUTOCOMPLETE_MAX_CHOICES:
                    break

            return choices
        except Exception:
            logger.exception("/feed url autocomplete failed")
            return []


class SettingsCommands(commands.Cog):
    """Settings management commands."""

    def __init__(self, bot: NewsFlowBot) -> None:
        self.bot = bot

    settings_group = app_commands.Group(
        name="settings",
        description="Configure bot settings",
        # All subcommands mutate channel-wide state; see feed_group's note.
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @settings_group.command(name="language", description="Set translation target language")
    @app_commands.describe(language="Language code (e.g., zh-CN, ja, ko, en)")
    async def settings_language(self, interaction: discord.Interaction, language: str) -> None:
        """Set translation language for all feeds in this channel."""
        await interaction.response.defer(ephemeral=True)

        normalized = normalize_language_code(language)
        if normalized is None:
            await interaction.followup.send(
                f"⚠️ `{language}` doesn't look like a language code. "
                f"Try one of: {LANGUAGE_CODE_EXAMPLES}.",
                ephemeral=True,
            )
            return
        language = normalized

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            updated = await service.update_settings(
                platform="discord",
                channel_id=str(interaction.channel_id),
                target_language=language,
            )
            await session.commit()

        embed = discord.Embed(
            title="Language Updated",
            description=f"Translation language set to: **{language}**\n"
            f"Saved as the channel default (new subscriptions inherit it); "
            f"{updated} existing subscription(s) updated.",
            color=discord.Color.green(),
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @settings_group.command(name="translate", description="Enable or disable translation")
    @app_commands.describe(enabled="Enable translation")
    async def settings_translate(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Toggle translation for all feeds in this channel."""
        await interaction.response.defer(ephemeral=True)

        settings = get_settings()
        if enabled and not settings.can_translate():
            embed = discord.Embed(
                title="Translation Not Available",
                description="Translation is not configured on this bot instance.\n"
                "The bot owner needs to set up translation API keys.",
                color=discord.Color.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            updated = await service.update_settings(
                platform="discord",
                channel_id=str(interaction.channel_id),
                translate=enabled,
            )
            await session.commit()

        status = "enabled" if enabled else "disabled"
        embed = discord.Embed(
            title="Translation Updated",
            description=f"Translation **{status}** — saved as the channel default; "
            f"{updated} existing subscription(s) updated.",
            color=discord.Color.green(),
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @settings_group.command(
        name="silent",
        description="Channel-wide: silence (digest-only) or unsilence every feed in this channel",
    )
    @app_commands.describe(
        enabled="True = digest only (no instant push), False = instant push enabled",
    )
    async def settings_silent(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Bulk-toggle silent on every subscription in this channel."""
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.set_channel_silent(
                platform="discord",
                channel_id=str(interaction.channel_id),
                silent=enabled,
            )
            await session.commit()

        embed = discord.Embed(
            title="Silent Mode Updated" if result.success else "Failed to Update",
            description=result.message,
            color=discord.Color.blurple() if result.success else discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="status", description="Show bot status")
    async def status(self, interaction: discord.Interaction) -> None:
        """Show bot status."""
        await interaction.response.defer(ephemeral=True)

        settings = get_settings()

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            subs = await service.get_channel_subscriptions(
                platform="discord",
                channel_id=str(interaction.channel_id),
                # Same counting basis as /feed list, which shows paused subs
                # too — otherwise the two views disagree when anything is
                # paused.
                include_inactive=True,
            )

        embed = discord.Embed(
            title="NewsFlow Bot Status",
            color=discord.Color.blue(),
            timestamp=datetime.now(UTC),
        )

        embed.add_field(name="Guilds", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(
            name="Translation",
            value="Available" if settings.can_translate() else "Not configured",
            inline=True,
        )
        embed.add_field(
            name="Fetch Interval",
            value=f"{settings.fetch_interval_minutes} min",
            inline=True,
        )
        embed.add_field(
            name="Channel Subscriptions",
            value=str(len(subs)),
            inline=True,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)


class DigestCommands(commands.Cog):
    """AI-generated daily / weekly digest configuration."""

    def __init__(self, bot: NewsFlowBot) -> None:
        self.bot = bot

    digest_group = app_commands.Group(
        name="digest",
        description="Daily / weekly AI digest of what was pushed to this channel",
        # enable/disable/now mutate state and spend LLM tokens; the group
        # gate necessarily takes /digest show along with them (Discord has
        # no per-subcommand permissions) — see feed_group's note.
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @digest_group.command(
        name="enable",
        description="Enable or update the periodic digest for this channel",
    )
    @app_commands.describe(
        schedule="How often to deliver: daily or weekly",
        hour="Delivery hour 0-23, in `timezone` (default UTC)",
        weekday="Day of week for weekly schedule (0=Mon … 6=Sun), in `timezone`",
        language="Target language code (e.g. zh-CN, en)",
        timezone="IANA name (Asia/Shanghai) or fixed offset (+8, -5:30). Default UTC",
        include_filtered="Include entries that matched filter out",
        max_articles="Cap articles per digest (default 50)",
    )
    @app_commands.choices(
        schedule=[
            app_commands.Choice(name="daily", value="daily"),
            app_commands.Choice(name="weekly", value="weekly"),
        ]
    )
    async def digest_enable(
        self,
        interaction: discord.Interaction,
        schedule: app_commands.Choice[str],
        hour: app_commands.Range[int, 0, 23] = 9,
        weekday: app_commands.Range[int, 0, 6] | None = None,
        language: str = "zh-CN",
        timezone: str = "UTC",
        include_filtered: bool = False,
        max_articles: app_commands.Range[int, 1, 200] = 50,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if schedule.value == "weekly" and weekday is None:
            await interaction.followup.send(
                "Weekly schedule requires a `weekday` (0=Mon … 6=Sun).",
                ephemeral=True,
            )
            return

        normalized_lang = normalize_language_code(language)
        if normalized_lang is None:
            await interaction.followup.send(
                f"⚠️ `{language}` doesn't look like a language code. "
                f"Try one of: {LANGUAGE_CODE_EXAMPLES}.",
                ephemeral=True,
            )
            return
        language = normalized_lang

        # The schedule is given in the user's timezone but stored as UTC —
        # converted once, here (see core/timezones.py for the DST caveat).
        tz = parse_timezone(timezone)
        if tz is None:
            await interaction.followup.send(
                f"Unrecognized timezone `{timezone}`. Use an IANA name like "
                "`Asia/Shanghai` or a fixed offset like `+8` / `-5:30`.",
                ephemeral=True,
            )
            return
        local_weekday = int(weekday) if schedule.value == "weekly" and weekday is not None else None
        utc_hour, utc_weekday = local_schedule_to_utc(int(hour), local_weekday, tz)

        from newsflow.repositories.digest_repository import (
            ChannelDigestRepository,
        )

        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            await repo.upsert(
                platform="discord",
                channel_id=str(interaction.channel_id),
                guild_id=(str(interaction.guild_id) if interaction.guild_id else None),
                enabled=True,
                schedule=schedule.value,
                delivery_hour_utc=utc_hour,
                delivery_weekday=utc_weekday,
                language=language,
                include_filtered=bool(include_filtered),
                max_articles=int(max_articles),
            )
            await session.commit()

        local_desc = f"{int(hour):02d}:00 {timezone}" + (
            f" (weekday {local_weekday})" if local_weekday is not None else ""
        )
        utc_desc = f"{utc_hour:02d}:00 UTC" + (
            f" (weekday {utc_weekday})" if utc_weekday is not None else ""
        )
        lines = [
            "✅ Digest enabled",
            f"**Schedule:** {schedule.value}",
            f"**Delivery time:** {local_desc}"
            + (f" = {utc_desc}" if utc_desc != local_desc else ""),
            f"**Language:** {language}",
            f"**Max articles:** {max_articles}",
            f"**Include filtered:** {'yes' if include_filtered else 'no'}",
        ]
        embed = discord.Embed(
            title="Digest Configured",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @digest_group.command(
        name="disable",
        description="Turn off the digest for this channel (config preserved)",
    )
    async def digest_disable(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        from newsflow.repositories.digest_repository import (
            ChannelDigestRepository,
        )

        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            config = await repo.get("discord", str(interaction.channel_id))
            if config is None:
                await interaction.followup.send(
                    "No digest configured for this channel.",
                    ephemeral=True,
                )
                return
            config.enabled = False
            await session.commit()

        await interaction.followup.send(
            "⏸ Digest disabled. Use `/digest enable` to turn it back on.",
            ephemeral=True,
        )

    @digest_group.command(
        name="show",
        description="Show the current digest configuration",
    )
    async def digest_show(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        from newsflow.repositories.digest_repository import (
            ChannelDigestRepository,
        )

        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            config = await repo.get("discord", str(interaction.channel_id))

        if config is None:
            embed = discord.Embed(
                title="Digest",
                description="No digest configured for this channel.\n"
                "Use `/digest enable` to set one up.",
                color=discord.Color.blue(),
            )
        else:
            lines = [
                f"**Enabled:** {'✅ yes' if config.enabled else '⏸ no'}",
                f"**Schedule:** {config.schedule}"
                + (f" (weekday {config.delivery_weekday})" if config.schedule == "weekly" else ""),
                f"**Delivery time:** {config.delivery_hour_utc:02d}:00 UTC",
                f"**Language:** {config.language}",
                f"**Max articles:** {config.max_articles}",
                f"**Include filtered:** {'yes' if config.include_filtered else 'no'}",
                f"**Last delivered:** {relative_time(config.last_delivered_at)}",
            ]
            embed = discord.Embed(
                title="Digest Configuration",
                description="\n".join(lines),
                color=discord.Color.blue(),
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @digest_group.command(
        name="now",
        description="Generate and deliver a digest immediately (for testing)",
    )
    async def digest_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        from datetime import datetime

        from newsflow.repositories.digest_repository import (
            ChannelDigestRepository,
        )
        from newsflow.services.digest_service import DigestService
        from newsflow.services.summarization import get_summarizer

        summarizer = get_summarizer()
        if summarizer is None:
            await interaction.followup.send(
                "⚠️ Digest not available: LLM provider is not configured "
                "(check `OPENAI_API_KEY` and `digest_provider` settings).",
                ephemeral=True,
            )
            return

        session_factory = get_session_factory()

        # Session 1: load config + generate digest text. Closes before
        # Discord IO so we're not holding a pooled connection across a
        # multi-second network round-trip. Capture scalar fields we
        # need later — ORM attribute access after session close is
        # fine here (expire_on_commit=False) but being explicit avoids
        # subtle detached-instance bugs.
        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            config = await repo.get("discord", str(interaction.channel_id))
            if config is None:
                await interaction.followup.send(
                    "No digest configured. Run `/digest enable` first.",
                    ephemeral=True,
                )
                return

            config_id = config.id
            prior_pin_id = config.last_pinned_message_id

            service = DigestService(session, summarizer)
            now = datetime.now(UTC)
            result = await service.generate(config, now=now)

        if result is None:
            await interaction.followup.send(
                "No articles in the current window — nothing to summarize.",
                ephemeral=True,
            )
            return
        if not result.success:
            await interaction.followup.send(
                f"❌ Digest generation failed: {result.error}",
                ephemeral=True,
            )
            return

        # Post into the channel (not ephemeral — this IS the digest).
        dispatcher = get_dispatcher()
        adapter = dispatcher._adapters.get("discord")
        if adapter is None:
            await interaction.followup.send(
                "Discord adapter not registered yet — try again.",
                ephemeral=True,
            )
            return

        chunks, new_pin_id = await dispatcher.deliver_digest(
            adapter,
            str(interaction.channel_id),
            dispatcher.apply_digest_header(result.text, "discord"),
            chunk_size=1900,
            prior_pin_id=prior_pin_id,
        )

        if chunks == 0:
            await interaction.followup.send(
                "❌ Digest generated but delivery failed.",
                ephemeral=True,
            )
            return

        # Session 2: persist delivery mark. Kept separate + tiny so it
        # doesn't contend with the dispatch loop's long write
        # transaction. If the UPDATE still fails under lock pressure,
        # the digest is already in the channel — surface a warning to
        # the user rather than letting the interaction die with "the
        # application did not respond".
        mark_failed = False
        try:
            async with session_factory() as session:
                repo = ChannelDigestRepository(session)
                await repo.mark_delivered(config_id, now, pinned_message_id=new_pin_id)
                await session.commit()
        except Exception:
            logger.exception(
                "digest_now: mark_delivered failed; digest was delivered "
                "but last_delivered_at/last_pinned_message_id are stale"
            )
            mark_failed = True

        msg = f"✅ Digest delivered ({chunks} message{'s' if chunks != 1 else ''})."
        if mark_failed:
            msg += (
                " ⚠️ Could not update delivery record (DB was busy). "
                "The scheduler may re-fire this slot."
            )
        await interaction.followup.send(msg, ephemeral=True)


class DiscordAdapter(BaseAdapter):
    """Discord adapter implementation."""

    def __init__(self, bot_or_token: NewsFlowBot | str) -> None:
        if isinstance(bot_or_token, NewsFlowBot):
            self.bot = bot_or_token
            self.token = None
        else:
            self.token = bot_or_token
            self.bot = NewsFlowBot()

    @property
    def platform_name(self) -> str:
        return "discord"

    async def start(self) -> None:
        """Start the Discord bot."""
        if self.token:
            await self.bot.start(self.token)

    async def stop(self) -> None:
        """Stop the Discord bot."""
        await self.bot.close()

    def is_connected(self) -> bool:
        """Ready + not closed. discord.py handles auto-reconnect internally
        but briefly reports not-ready during disconnect windows."""
        return self.bot is not None and self.bot.is_ready() and not self.bot.is_closed()

    async def send_message(self, channel_id: str, message: Message) -> bool:
        """Send a message to a Discord channel.

        Raises ChannelGoneError when the channel no longer exists
        (deleted by guild owner, or bot was removed from the guild —
        both surface as HTTP 404). Transient problems (403 Forbidden,
        network, rate-limit) still return False so the next dispatch
        cycle can retry.
        """
        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                channel = await self.bot.fetch_channel(int(channel_id))

            if not channel or not isinstance(channel, discord.abc.Messageable):
                logger.warning(
                    f"Channel {channel_id} not found or not messageable "
                    f"(type={type(channel).__name__})"
                )
                return False

            mention = message.mention

            if message.template_text is not None:
                # Custom template: plain content (Discord renders the
                # Markdown natively). show_image was already applied to
                # message.image_url by the dispatcher — when an image is
                # left, carry it in an image-only embed so the template
                # keeps full authority over the text. A configured mention
                # is prefixed unless the template already placed it via
                # {mention} (then the rendered text contains it verbatim).
                content = message.template_text
                if mention and mention not in content:
                    content = f"{mention}\n{content}"
                if len(content) > 2000:
                    content = content[:1999] + "…"
                allowed = _mention_allowance(mention) if mention else discord.AllowedMentions.none()
                if message.image_url:
                    image_embed = discord.Embed()
                    image_embed.set_image(url=message.image_url)
                    await channel.send(content=content, embed=image_embed, allowed_mentions=allowed)
                else:
                    await channel.send(content, allowed_mentions=allowed)
                return True

            embed = self._create_embed(message)
            if mention:
                await channel.send(
                    content=mention, embed=embed, allowed_mentions=_mention_allowance(mention)
                )
            else:
                await channel.send(embed=embed)
            return True

        except discord.NotFound as e:
            raise ChannelGoneError(channel_id, reason=str(e)) from e
        except discord.Forbidden:
            logger.warning(f"No permission to send to channel {channel_id}")
            return False
        except Exception as e:
            logger.exception(f"Failed to send message to {channel_id}: {e}")
            return False

    async def send_text(self, channel_id: str, text: str) -> bool:
        """Send plain text to a Discord channel. Raises ChannelGoneError
        when the channel no longer exists — see send_message."""
        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                channel = await self.bot.fetch_channel(int(channel_id))

            if not channel or not isinstance(channel, discord.abc.Messageable):
                return False

            await channel.send(text)
            return True

        except discord.NotFound as e:
            raise ChannelGoneError(channel_id, reason=str(e)) from e
        except Exception as e:
            logger.exception(f"Failed to send text to {channel_id}: {e}")
            return False

    async def send_text_pinned(self, channel_id: str, text: str) -> tuple[bool, str | None]:
        """Send text and pin the resulting message. Respects the
        `digest_auto_pin` setting: when disabled, this is equivalent to
        `send_text` (sends, doesn't pin, returns `(sent, None)`).

        Pin failures degrade to "sent but not pinned" — the digest still
        reaches the channel. Permissions and the Discord 50-pin cap are
        the common reasons for a pin to fail.
        """
        if not get_settings().digest_auto_pin:
            sent = await self.send_text(channel_id, text)
            return sent, None

        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                channel = await self.bot.fetch_channel(int(channel_id))
            if not channel or not isinstance(channel, discord.abc.Messageable):
                return False, None

            msg = await channel.send(text)
        except discord.NotFound as e:
            raise ChannelGoneError(channel_id, reason=str(e)) from e
        except discord.Forbidden:
            logger.warning(f"No permission to send to channel {channel_id}")
            return False, None
        except Exception as e:
            logger.exception(f"Failed to send text to {channel_id}: {e}")
            return False, None

        try:
            await msg.pin()
            return True, str(msg.id)
        except discord.Forbidden:
            logger.warning(
                f"Cannot pin in channel {channel_id}: bot needs 'Manage Messages' permission"
            )
            return True, None
        except discord.HTTPException as e:
            # Most common: 30003 = max pins reached (50 per channel).
            logger.warning(f"Pin failed in channel {channel_id}: {e}")
            return True, None

    async def unpin_message(self, channel_id: str, message_id: str) -> bool:
        """Unpin a previously-pinned message. Treats NotFound as success
        (the message is no longer around to unpin — goal achieved)."""
        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                channel = await self.bot.fetch_channel(int(channel_id))
            if not channel or not isinstance(channel, discord.abc.Messageable):
                return False
            msg = await channel.fetch_message(int(message_id))
            await msg.unpin()
            return True
        except discord.NotFound:
            return True
        except discord.Forbidden:
            logger.warning(
                f"Cannot unpin in channel {channel_id}: bot needs 'Manage Messages' permission"
            )
            return False
        except Exception as e:
            logger.warning(f"Unpin failed for message {message_id} in {channel_id}: {e}")
            return False

    def _create_embed(self, message: Message) -> discord.Embed:
        """Create a Discord embed from a Message."""
        # SQLite + aiosqlite drops tzinfo on read even for
        # DateTime(timezone=True) columns, so message.published_at
        # (sourced from FeedEntry.published_at) can come back naive
        # despite always being written as aware UTC. discord.py's
        # Embed setter calls .astimezone() on naive values, which
        # interprets them as the host's local time — on a non-UTC
        # host the displayed embed timestamp would shift by the host
        # offset. Pin to UTC explicitly. No-op on UTC hosts (prod).
        ts = message.published_at or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        embed = discord.Embed(
            description=f"[{message.display_title}]({message.link})",
            color=discord.Color.blue(),
            timestamp=ts,
        )

        # Add summary
        summary = message.display_summary
        if summary:
            if len(summary) > 1000:
                summary = summary[:997] + "..."
            embed.add_field(
                name="Summary",
                value=summary,
                inline=False,
            )

        # Add source and time
        footer_text = f"Source: {message.source}"
        embed.set_footer(text=footer_text)

        # Add image if available
        if message.image_url:
            embed.set_image(url=message.image_url)

        return embed


# Global bot instance
_bot: NewsFlowBot | None = None


async def start_discord(token: str) -> None:
    """Start the Discord bot."""
    global _bot
    _bot = NewsFlowBot()
    await _bot.start(token)


async def stop_discord() -> None:
    """Stop the Discord bot."""
    global _bot
    if _bot:
        await _bot.close()
        _bot = None


def get_discord_bot() -> NewsFlowBot | None:
    """Get the Discord bot instance."""
    return _bot
