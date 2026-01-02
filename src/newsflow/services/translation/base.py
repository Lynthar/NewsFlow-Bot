"""
Translation service abstraction.

Provides a common interface for translation providers.
"""

import asyncio
import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from newsflow.services.cache import CacheBackend

logger = logging.getLogger(__name__)


@dataclass
class TranslationResult:
    """Result of a translation request."""

    success: bool
    translated_text: str = ""
    source_language: str | None = None
    error: str | None = None
    from_cache: bool = False


class TranslationProvider(ABC):
    """Abstract base class for translation providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging and identification."""
        pass

    @abstractmethod
    async def translate(
        self,
        text: str,
        target_lang: str,
        source_lang: str | None = None,
    ) -> TranslationResult:
        """
        Translate text to the target language.

        Args:
            text: Text to translate.
            target_lang: Target language code (e.g., "zh-CN", "ja", "ko").
            source_lang: Optional source language code. If None, auto-detect.

        Returns:
            TranslationResult with translated text or error.
        """
        pass

    @abstractmethod
    def supports_language(self, lang_code: str) -> bool:
        """Check if the provider supports a language code."""
        pass

    def normalize_language_code(self, lang_code: str) -> str:
        """
        Normalize language code to provider-specific format.

        Override this method if the provider uses different codes.
        """
        return lang_code


class TranslationService:
    """
    Translation service with caching support.

    Wraps a translation provider with optional caching to reduce API calls.
    """

    def __init__(
        self,
        provider: TranslationProvider,
        cache: "CacheBackend | None" = None,
        cache_ttl: int = 86400 * 7,  # 7 days
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.cache_ttl = cache_ttl

    def _cache_key(self, text: str, target_lang: str) -> str:
        """Generate a cache key for a translation request."""
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        return f"trans:{self.provider.name}:{target_lang}:{text_hash}"

    async def translate(
        self,
        text: str,
        target_lang: str,
        source_lang: str | None = None,
    ) -> TranslationResult:
        """
        Translate text with caching.

        Args:
            text: Text to translate.
            target_lang: Target language code.
            source_lang: Optional source language code.

        Returns:
            TranslationResult with translated text or error.
        """
        if not text or not text.strip():
            return TranslationResult(success=True, translated_text="")

        # Check cache
        if self.cache:
            cache_key = self._cache_key(text, target_lang)
            cached = await self.cache.get(cache_key)
            if cached:
                logger.debug(f"Translation cache hit: {cache_key}")
                return TranslationResult(
                    success=True,
                    translated_text=cached,
                    from_cache=True,
                )

        # Call provider
        result = await self.provider.translate(text, target_lang, source_lang)

        # Cache successful translations
        if result.success and self.cache and result.translated_text:
            cache_key = self._cache_key(text, target_lang)
            await self.cache.set(cache_key, result.translated_text, ttl=self.cache_ttl)
            logger.debug(f"Translation cached: {cache_key}")

        return result

    async def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        source_lang: str | None = None,
        max_concurrent: int = 3,
    ) -> list[TranslationResult]:
        """
        Translate multiple texts concurrently.

        Args:
            texts: List of texts to translate.
            target_lang: Target language code.
            source_lang: Optional source language code.
            max_concurrent: Maximum concurrent translations (default: 3).

        Returns:
            List of TranslationResult objects in the same order as input.
        """
        if not texts:
            return []

        semaphore = asyncio.Semaphore(max_concurrent)

        async def translate_with_limit(text: str) -> TranslationResult:
            async with semaphore:
                return await self.translate(text, target_lang, source_lang)

        results = await asyncio.gather(
            *[translate_with_limit(text) for text in texts]
        )
        return list(results)

    def supports_language(self, lang_code: str) -> bool:
        """Check if the provider supports a language code."""
        return self.provider.supports_language(lang_code)
