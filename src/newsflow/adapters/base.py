"""
Base adapter class for messaging platforms.

All platform adapters (Discord, Telegram, etc.) should inherit from this.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Message:
    """
    Platform-agnostic message format.

    This is the common format used by all adapters.
    """

    title: str
    summary: str
    link: str
    source: str
    published_at: datetime | None = None
    image_url: str | None = None

    # Optional translated versions
    title_translated: str | None = None
    summary_translated: str | None = None

    @property
    def display_title(self) -> str:
        """Get title, preferring translated version."""
        return self.title_translated or self.title

    @property
    def display_summary(self) -> str:
        """Get summary, preferring translated version."""
        return self.summary_translated or self.summary


class BaseAdapter(ABC):
    """
    Abstract base class for messaging platform adapters.

    Each platform (Discord, Telegram, Webhook) implements this interface.
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Return the platform name (e.g., 'discord', 'telegram')."""
        pass

    @abstractmethod
    async def start(self) -> None:
        """
        Start the adapter.

        This should connect to the platform and begin listening for commands.
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop the adapter gracefully.

        This should disconnect from the platform and cleanup resources.
        """
        pass

    @abstractmethod
    async def send_message(
        self,
        channel_id: str,
        message: Message,
    ) -> bool:
        """
        Send a message to a channel.

        Args:
            channel_id: Platform-specific channel identifier
            message: Message to send

        Returns:
            True if sent successfully, False otherwise
        """
        pass

    @abstractmethod
    async def send_text(
        self,
        channel_id: str,
        text: str,
    ) -> bool:
        """
        Send a plain text message to a channel.

        Args:
            channel_id: Platform-specific channel identifier
            text: Text to send

        Returns:
            True if sent successfully, False otherwise
        """
        pass

    async def on_ready(self) -> None:
        """Called when the adapter is ready and connected."""
        pass

    async def on_error(self, error: Exception) -> None:
        """Called when an error occurs."""
        pass
