"""
OpenAI translation provider.

Uses OpenAI's GPT models for translation with context understanding.
"""

import logging
from typing import Any

from newsflow.services.translation.base import TranslationProvider, TranslationResult

logger = logging.getLogger(__name__)

# Default translation prompt. Users can override via
# Settings.translation_system_prompt (env: TRANSLATION_SYSTEM_PROMPT).
# Both placeholders are always filled — {source_desc} collapses to
# "the source language (auto-detect)" when source_lang is unknown.
DEFAULT_TRANSLATION_PROMPT = (
    "You are a professional translator. "
    "Translate the following text from {source_desc} to {target_name}. "
    "Preserve the original meaning and tone. "
    "Only output the translated text, nothing else."
)


# Language names for better prompts
LANGUAGE_NAMES = {
    "zh": "Simplified Chinese",
    "zh-cn": "Simplified Chinese",
    "zh-hans": "Simplified Chinese",
    "zh-tw": "Traditional Chinese",
    "zh-hant": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
    "ms": "Malay",
}


class OpenAIProvider(TranslationProvider):
    """OpenAI GPT translation provider."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5.4-nano",
        base_url: str | None = None,
        system_prompt_template: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.system_prompt_template = (
            system_prompt_template or DEFAULT_TRANSLATION_PROMPT
        )
        self._client: Any = None

    @property
    def name(self) -> str:
        return "openai"

    def _get_client(self) -> Any:
        """Lazy initialization of OpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI

                kwargs = {"api_key": self.api_key}
                if self.base_url:
                    kwargs["base_url"] = self.base_url

                self._client = AsyncOpenAI(**kwargs)
            except ImportError:
                raise ImportError(
                    "openai package is required for OpenAI translation. "
                    "Install it with: pip install openai"
                )
        return self._client

    def _get_language_name(self, lang_code: str) -> str:
        """Get human-readable language name."""
        code = lang_code.lower()
        return LANGUAGE_NAMES.get(code, lang_code)

    def supports_language(self, lang_code: str) -> bool:
        """OpenAI supports virtually all languages."""
        return True

    async def translate(
        self,
        text: str,
        target_lang: str,
        source_lang: str | None = None,
    ) -> TranslationResult:
        """Translate text using OpenAI API."""
        try:
            client = self._get_client()
            target_name = self._get_language_name(target_lang)

            source_desc = (
                self._get_language_name(source_lang)
                if source_lang
                else "the source language (auto-detect)"
            )
            try:
                system_prompt = self.system_prompt_template.format(
                    source_desc=source_desc, target_name=target_name
                )
            except (KeyError, IndexError) as e:
                logger.warning(
                    f"translation_system_prompt references unknown placeholder "
                    f"{e}; falling back to default"
                )
                system_prompt = DEFAULT_TRANSLATION_PROMPT.format(
                    source_desc=source_desc, target_name=target_name
                )

            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0.3,
                max_tokens=2000,
            )

            translated = response.choices[0].message.content.strip()

            return TranslationResult(
                success=True,
                translated_text=translated,
            )

        except ImportError as e:
            logger.error(f"OpenAI package not installed: {e}")
            return TranslationResult(
                success=False,
                error="OpenAI package not installed. Install with: pip install openai",
            )
        except Exception as e:
            logger.exception(f"OpenAI translation error: {e}")
            return TranslationResult(
                success=False,
                error=str(e),
            )
