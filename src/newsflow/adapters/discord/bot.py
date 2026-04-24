"""
Discord bot adapter.

Implements Discord-specific functionality using discord.py.
Uses Slash Commands (Application Commands) as recommended by Discord.
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from newsflow.adapters.base import BaseAdapter, ChannelGoneError, Message
from newsflow.config import get_settings
from newsflow.core.filter import parse_keyword_csv
from newsflow.core.timeutil import relative_time, time_until
from newsflow.models.base import get_session_factory
from newsflow.models.subscription import Subscription
from newsflow.services import SubscriptionService, get_dispatcher

# Max subscriptions per /feed list page. Discord embed description caps
# at 4096 chars; 20 entries × ~150 chars each leaves comfortable headroom.
LIST_PAGE_SIZE = 20


def _sub_status_chip(sub: Subscription) -> str | None:
    """Return a one-line status chip when the sub needs user attention, else None.

    Priority: user-paused > feed auto-disabled > feed errored. Healthy subs
    get no chip to keep the list uncluttered.
    """
    feed = sub.feed
    if not sub.is_active:
        return "⏸ paused"
    if not feed.is_active:
        return "🛑 auto-disabled (too many errors)"
    if feed.error_count > 0:
        return f"⚠️ {feed.error_count} errors, retry {time_until(feed.next_retry_at)}"
    return None


def _format_sub_line(sub: Subscription) -> str:
    """Format one subscription for the /feed list description."""
    feed = sub.feed
    title = feed.title or "Untitled"
    parts = [
        f"🌐 {sub.target_language}" if sub.translate else "📰 no translate"
    ]
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


class NewsFlowBot(commands.Bot):
    """
    Discord bot with slash commands.
    """

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix="!",  # Fallback prefix
            intents=intents,
            help_command=None,
        )

        self.settings = get_settings()

    async def setup_hook(self) -> None:
        """Called when the bot is ready to setup."""
        # Add cogs
        await self.add_cog(FeedCommands(self))
        await self.add_cog(SettingsCommands(self))
        await self.add_cog(DigestCommands(self))

        # Sync slash commands
        logger.info("Syncing slash commands...")
        await self.tree.sync()
        logger.info("Slash commands synced")

    async def on_ready(self) -> None:
        """Called when bot is ready."""
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

    async def on_error(self, event: str, *args, **kwargs) -> None:
        """Handle errors."""
        logger.exception(f"Error in {event}")


class FeedCommands(commands.Cog):
    """Feed management commands."""

    def __init__(self, bot: NewsFlowBot) -> None:
        self.bot = bot

    feed_group = app_commands.Group(name="feed", description="Manage RSS feeds")

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
    async def feed_list(
        self, interaction: discord.Interaction, page: int = 1
    ) -> None:
        """List feeds for this channel, paginated."""
        await interaction.response.defer(ephemeral=True)

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            subscriptions = list(
                await service.get_channel_subscriptions(
                    platform="discord",
                    channel_id=str(interaction.channel_id),
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
    async def feed_pause(
        self, interaction: discord.Interaction, url: str
    ) -> None:
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
    @app_commands.describe(url="The RSS feed URL to resume")
    async def feed_resume(
        self, interaction: discord.Interaction, url: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
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

    @feed_group.command(name="status", description="Detailed status of one feed in this channel")
    @app_commands.describe(url="The RSS feed URL")
    async def feed_status(
        self, interaction: discord.Interaction, url: str
    ) -> None:
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
        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.set_feed_language(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
                language=code,
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
        description="Set a keyword filter for one feed (comma-separated lists)",
    )
    @app_commands.describe(
        url="The RSS feed URL",
        include="Entries must contain at least one of these (csv). Leave blank for no include filter.",
        exclude="Entries containing any of these are skipped (csv). Leave blank for no exclude filter.",
    )
    async def feed_filter_set(
        self,
        interaction: discord.Interaction,
        url: str,
        include: str = "",
        exclude: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        include_kw = parse_keyword_csv(include)
        exclude_kw = parse_keyword_csv(exclude)

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.set_feed_filter(
                platform="discord",
                channel_id=str(interaction.channel_id),
                feed_url=url,
                include_keywords=include_kw,
                exclude_keywords=exclude_kw,
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
    async def feed_filter_show(
        self, interaction: discord.Interaction, url: str
    ) -> None:
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
            if rule.include_keywords:
                lines.append(
                    f"**Include** (any of): "
                    + ", ".join(f"`{k}`" for k in rule.include_keywords)
                )
            if rule.exclude_keywords:
                lines.append(
                    f"**Exclude** (none of): "
                    + ", ".join(f"`{k}`" for k in rule.exclude_keywords)
                )
            embed = discord.Embed(
                title="Filter",
                description="\n".join(lines),
                color=discord.Color.blue(),
            )
            embed.set_footer(text="Matching is case-insensitive on title + summary")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @feed_group.command(
        name="filter-clear",
        description="Remove the keyword filter from one feed",
    )
    @app_commands.describe(url="The RSS feed URL")
    async def feed_filter_clear(
        self, interaction: discord.Interaction, url: str
    ) -> None:
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
            await interaction.followup.send(
                "⚠️ OPML file too large (1 MB cap).", ephemeral=True
            )
            return

        try:
            content = (await file.read()).decode("utf-8")
        except UnicodeDecodeError:
            await interaction.followup.send(
                "⚠️ OPML file is not valid UTF-8.", ephemeral=True
            )
            return

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            result = await service.import_opml(
                platform="discord",
                user_id=str(interaction.user.id),
                channel_id=str(interaction.channel_id),
                opml_content=content,
                guild_id=(
                    str(interaction.guild_id) if interaction.guild_id else None
                ),
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

        fetcher = get_fetcher()
        result = await fetcher.fetch_feed(url)

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


class SettingsCommands(commands.Cog):
    """Settings management commands."""

    def __init__(self, bot: NewsFlowBot) -> None:
        self.bot = bot

    settings_group = app_commands.Group(name="settings", description="Configure bot settings")

    @settings_group.command(name="language", description="Set translation target language")
    @app_commands.describe(language="Language code (e.g., zh-CN, ja, ko, en)")
    async def settings_language(
        self, interaction: discord.Interaction, language: str
    ) -> None:
        """Set translation language for all feeds in this channel."""
        await interaction.response.defer(ephemeral=True)

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            success = await service.update_settings(
                platform="discord",
                channel_id=str(interaction.channel_id),
                target_language=language,
            )
            await session.commit()

        if success:
            embed = discord.Embed(
                title="Language Updated",
                description=f"Translation language set to: **{language}**",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="No Subscriptions",
                description="No feeds subscribed in this channel.",
                color=discord.Color.orange(),
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @settings_group.command(name="translate", description="Enable or disable translation")
    @app_commands.describe(enabled="Enable translation")
    async def settings_translate(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
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
            success = await service.update_settings(
                platform="discord",
                channel_id=str(interaction.channel_id),
                translate=enabled,
            )
            await session.commit()

        status = "enabled" if enabled else "disabled"
        if success:
            embed = discord.Embed(
                title="Translation Updated",
                description=f"Translation **{status}** for all feeds in this channel.",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="No Subscriptions",
                description="No feeds subscribed in this channel.",
                color=discord.Color.orange(),
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
            )

        embed = discord.Embed(
            title="NewsFlow Bot Status",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
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
    )

    @digest_group.command(
        name="enable",
        description="Enable or update the periodic digest for this channel",
    )
    @app_commands.describe(
        schedule="How often to deliver: daily or weekly",
        hour_utc="Delivery hour in UTC, 0-23",
        weekday="Day of week for weekly schedule (0=Mon … 6=Sun)",
        language="Target language code (e.g. zh-CN, en)",
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
        hour_utc: app_commands.Range[int, 0, 23] = 9,
        weekday: app_commands.Range[int, 0, 6] | None = None,
        language: str = "zh-CN",
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

        from newsflow.repositories.digest_repository import (
            ChannelDigestRepository,
        )

        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            await repo.upsert(
                platform="discord",
                channel_id=str(interaction.channel_id),
                guild_id=(
                    str(interaction.guild_id) if interaction.guild_id else None
                ),
                enabled=True,
                schedule=schedule.value,
                delivery_hour_utc=int(hour_utc),
                delivery_weekday=(
                    int(weekday) if schedule.value == "weekly" else None
                ),
                language=language,
                include_filtered=bool(include_filtered),
                max_articles=int(max_articles),
            )
            await session.commit()

        lines = [
            f"✅ Digest enabled",
            f"**Schedule:** {schedule.value}"
            + (
                f" (weekday {weekday})"
                if schedule.value == "weekly"
                else ""
            ),
            f"**Delivery time:** {hour_utc:02d}:00 UTC",
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
    async def digest_disable(
        self, interaction: discord.Interaction
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        from newsflow.repositories.digest_repository import (
            ChannelDigestRepository,
        )

        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            config = await repo.get(
                "discord", str(interaction.channel_id)
            )
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
    async def digest_show(
        self, interaction: discord.Interaction
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        from newsflow.repositories.digest_repository import (
            ChannelDigestRepository,
        )

        session_factory = get_session_factory()
        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            config = await repo.get(
                "discord", str(interaction.channel_id)
            )

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
                + (
                    f" (weekday {config.delivery_weekday})"
                    if config.schedule == "weekly"
                    else ""
                ),
                f"**Delivery time:** {config.delivery_hour_utc:02d}:00 UTC",
                f"**Language:** {config.language}",
                f"**Max articles:** {config.max_articles}",
                f"**Include filtered:** "
                f"{'yes' if config.include_filtered else 'no'}",
                f"**Last delivered:** "
                f"{relative_time(config.last_delivered_at)}",
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
    async def digest_now(
        self, interaction: discord.Interaction
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        from datetime import datetime, timezone

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
        async with session_factory() as session:
            repo = ChannelDigestRepository(session)
            config = await repo.get(
                "discord", str(interaction.channel_id)
            )
            if config is None:
                await interaction.followup.send(
                    "No digest configured. Run `/digest enable` first.",
                    ephemeral=True,
                )
                return

            service = DigestService(session, summarizer)
            now = datetime.now(timezone.utc)
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
                prior_pin_id=config.last_pinned_message_id,
            )
            if chunks:
                await repo.mark_delivered(
                    config.id, now, pinned_message_id=new_pin_id
                )
                await session.commit()

        await interaction.followup.send(
            f"✅ Digest delivered ({chunks} message{'s' if chunks != 1 else ''}).",
            ephemeral=True,
        )


class DiscordAdapter(BaseAdapter):
    """Discord adapter implementation."""

    def __init__(self, bot_or_token) -> None:
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
        return (
            self.bot is not None
            and self.bot.is_ready()
            and not self.bot.is_closed()
        )

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

            if not channel or not isinstance(channel, discord.TextChannel):
                logger.warning(f"Channel {channel_id} not found or not a text channel")
                return False

            embed = self._create_embed(message)
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

            if not channel or not isinstance(channel, discord.TextChannel):
                return False

            await channel.send(text)
            return True

        except discord.NotFound as e:
            raise ChannelGoneError(channel_id, reason=str(e)) from e
        except Exception as e:
            logger.exception(f"Failed to send text to {channel_id}: {e}")
            return False

    async def send_text_pinned(
        self, channel_id: str, text: str
    ) -> tuple[bool, str | None]:
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
            if not channel or not isinstance(channel, discord.TextChannel):
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
                f"Cannot pin in channel {channel_id}: bot needs "
                f"'Manage Messages' permission"
            )
            return True, None
        except discord.HTTPException as e:
            # Most common: 30003 = max pins reached (50 per channel).
            logger.warning(f"Pin failed in channel {channel_id}: {e}")
            return True, None

    async def unpin_message(
        self, channel_id: str, message_id: str
    ) -> bool:
        """Unpin a previously-pinned message. Treats NotFound as success
        (the message is no longer around to unpin — goal achieved)."""
        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                channel = await self.bot.fetch_channel(int(channel_id))
            if not channel or not isinstance(channel, discord.TextChannel):
                return False
            msg = await channel.fetch_message(int(message_id))
            await msg.unpin()
            return True
        except discord.NotFound:
            return True
        except discord.Forbidden:
            logger.warning(
                f"Cannot unpin in channel {channel_id}: bot needs "
                f"'Manage Messages' permission"
            )
            return False
        except Exception as e:
            logger.warning(
                f"Unpin failed for message {message_id} in "
                f"{channel_id}: {e}"
            )
            return False

    def _create_embed(self, message: Message) -> discord.Embed:
        """Create a Discord embed from a Message."""
        embed = discord.Embed(
            description=f"[{message.display_title}]({message.link})",
            color=discord.Color.blue(),
            timestamp=message.published_at or datetime.now(timezone.utc),
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
