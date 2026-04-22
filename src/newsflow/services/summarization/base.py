"""Abstract summarization provider for periodic digests."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence


@dataclass(frozen=True)
class DigestArticle:
    """One input article for a digest prompt."""
    title: str
    summary: str
    link: str
    source: str
    published_at: datetime | None


@dataclass
class DigestResult:
    """Outcome of a digest generation call."""
    success: bool
    text: str = ""
    error: str | None = None


# Common human-readable language names for prompts. Keep minimal — provider
# sub-classes can override with wider lookups if needed.
LANGUAGE_NAMES = {
    "zh": "Simplified Chinese",
    "zh-cn": "Simplified Chinese",
    "zh-CN": "Simplified Chinese",
    "zh-hans": "Simplified Chinese",
    "zh-tw": "Traditional Chinese",
    "zh-TW": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "ru": "Russian",
}


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, LANGUAGE_NAMES.get(code.lower(), code))


class SummarizationProvider(ABC):
    """Generates a narrative digest from a batch of articles."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier for logs."""

    @abstractmethod
    async def generate_digest(
        self,
        articles: Sequence[DigestArticle],
        language: str,
        time_window_desc: str,
    ) -> DigestResult:
        """Produce a digest string from the given articles.

        Args:
            articles: Ordered list of articles, typically newest-last.
            language: Target language code (e.g. "zh-CN"). Output must be in
                this language regardless of article language.
            time_window_desc: Short human-readable window description used
                in the prompt, e.g. "past 24 hours", "past 7 days".
        """
