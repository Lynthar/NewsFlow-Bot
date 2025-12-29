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

from newsflow.adapters.base import BaseAdapter, Message
from newsflow.config import get_settings
from newsflow.models.base import get_session_factory
from newsflow.services import SubscriptionService, get_dispatcher

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
        self._dispatch_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        """Called when the bot is ready to setup."""
        # Add cogs
        await self.add_cog(FeedCommands(self))
        await self.add_cog(SettingsCommands(self))

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

        # Register adapter with dispatcher
        dispatcher = get_dispatcher()
        adapter = DiscordAdapter(self)
        dispatcher.register_adapter("discord", adapter)

        # Start dispatch loop
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(
                dispatcher.run_dispatch_loop()
            )
            logger.info("Started dispatch loop")

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

    @feed_group.command(name="list", description="List all RSS feeds in this channel")
    async def feed_list(self, interaction: discord.Interaction) -> None:
        """List all feeds for this channel."""
        await interaction.response.defer(ephemeral=True)

        session_factory = get_session_factory()
        async with session_factory() as session:
            service = SubscriptionService(session)
            subscriptions = await service.get_channel_subscriptions(
                platform="discord",
                channel_id=str(interaction.channel_id),
            )

        if not subscriptions:
            embed = discord.Embed(
                title="Subscribed Feeds",
                description="No feeds subscribed yet.\nUse `/feed add <url>` to add one.",
                color=discord.Color.blue(),
            )
        else:
            embed = discord.Embed(
                title=f"Subscribed Feeds ({len(subscriptions)})",
                color=discord.Color.blue(),
            )
            for sub in subscriptions:
                feed = sub.feed
                translate_status = "On" if sub.translate else "Off"
                embed.add_field(
                    name=feed.title or "Untitled",
                    value=f"URL: {feed.url}\nTranslate: {translate_status} ({sub.target_language})",
                    inline=False,
                )

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

    async def send_message(self, channel_id: str, message: Message) -> bool:
        """Send a message to a Discord channel."""
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

        except discord.Forbidden:
            logger.warning(f"No permission to send to channel {channel_id}")
            return False
        except Exception as e:
            logger.exception(f"Failed to send message to {channel_id}: {e}")
            return False

    async def send_text(self, channel_id: str, text: str) -> bool:
        """Send plain text to a Discord channel."""
        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                channel = await self.bot.fetch_channel(int(channel_id))

            if not channel or not isinstance(channel, discord.TextChannel):
                return False

            await channel.send(text)
            return True

        except Exception as e:
            logger.exception(f"Failed to send text to {channel_id}: {e}")
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
