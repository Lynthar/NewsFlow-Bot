"""Factory for the summarization provider. Lazy singleton, mirrors the
translation factory pattern."""

import logging

from newsflow.config import get_settings
from newsflow.services.summarization.base import SummarizationProvider

logger = logging.getLogger(__name__)


_provider: SummarizationProvider | None = None
_initialized: bool = False


def _build_provider() -> SummarizationProvider | None:
    settings = get_settings()
    # Digests need an LLM; today that means OpenAI-compatible. Future
    # providers plug in here.
    if settings.digest_provider == "openai":
        if not settings.openai_api_key:
            logger.info(
                "Digest disabled: digest_provider=openai but OPENAI_API_KEY not set"
            )
            return None
        from newsflow.services.summarization.openai import OpenAIDigestProvider

        return OpenAIDigestProvider(
            api_key=settings.openai_api_key,
            model=settings.digest_model,
            base_url=settings.openai_base_url,
            system_prompt_template=settings.digest_system_prompt,
        )
    logger.warning(
        f"Unknown digest provider: {settings.digest_provider!r}"
    )
    return None


def get_summarizer() -> SummarizationProvider | None:
    """Return the lazy-initialized digest provider, or None if unavailable."""
    global _provider, _initialized
    if not _initialized:
        _provider = _build_provider()
        _initialized = True
    return _provider


def reset_summarizer() -> None:
    """Test helper — clear the cached provider."""
    global _provider, _initialized
    _provider = None
    _initialized = False
