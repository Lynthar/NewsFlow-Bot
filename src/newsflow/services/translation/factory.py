"""
Translation service factory.

Creates the appropriate translation provider based on configuration.
"""

import logging

from newsflow.config import get_settings
from newsflow.services.cache import CacheBackend, get_cache
from newsflow.services.translation.base import (
    TranslationProvider,
    TranslationService,
)

logger = logging.getLogger(__name__)


def create_translation_provider() -> TranslationProvider | None:
    """
    Create a translation provider based on settings.

    Returns:
        TranslationProvider instance or None if translation is disabled.
    """
    settings = get_settings()

    if not settings.can_translate():
        logger.debug("Translation is disabled or not configured")
        return None

    provider = settings.translation_provider

    if provider == "deepl" and settings.deepl_api_key:
        from newsflow.services.translation.deepl import DeepLProvider

        logger.info("Using DeepL translation provider")
        return DeepLProvider(settings.deepl_api_key)

    elif provider == "openai" and settings.openai_api_key:
        from newsflow.services.translation.openai import OpenAIProvider

        logger.info("Using OpenAI translation provider")
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
        )

    elif provider == "google" and settings.google_credentials_path:
        from newsflow.services.translation.google import GoogleProvider

        logger.info("Using Google Cloud Translation provider")
        return GoogleProvider(
            credentials_path=settings.google_credentials_path,
            project_id=settings.google_project_id,
        )

    logger.warning(
        f"Translation provider '{provider}' is configured but API key/credentials are missing"
    )
    return None


def create_translation_service(
    cache: CacheBackend | None = None,
) -> TranslationService | None:
    """
    Create a translation service with optional caching.

    Args:
        cache: Optional cache backend. If None, uses global cache.

    Returns:
        TranslationService instance or None if translation is disabled.
    """
    provider = create_translation_provider()
    if not provider:
        return None

    # Use provided cache or global cache
    cache_backend = cache or get_cache()

    service = TranslationService(
        provider=provider,
        cache=cache_backend,
    )

    logger.info(
        f"Created translation service with {provider.name} provider"
        + (" (cached)" if cache_backend else "")
    )

    return service


# Global translation service instance
_translation_service: TranslationService | None = None
_initialized: bool = False


def get_translation_service() -> TranslationService | None:
    """
    Get the global translation service instance.

    Lazily initializes the service on first call.
    """
    global _translation_service, _initialized

    if not _initialized:
        _translation_service = create_translation_service()
        _initialized = True

    return _translation_service


def reset_translation_service() -> None:
    """Reset the global translation service (for testing)."""
    global _translation_service, _initialized
    _translation_service = None
    _initialized = False
