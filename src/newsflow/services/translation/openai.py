"""
OpenAI translation provider.

Uses OpenAI's GPT models for translation with context understanding.
"""

import logging
from typing import Any

from newsflow.services.llm import chat_completions_create, fill_prompt, language_name, make_client
from newsflow.services.translation.base import TranslationProvider, TranslationResult

logger = logging.getLogger(__name__)

# Default translation prompt; override via TRANSLATION_SYSTEM_PROMPT. Both
# placeholders are always filled — {source_desc} collapses to an auto-detect
# phrase when source_lang is unknown.
DEFAULT_TRANSLATION_PROMPT = (
    "You are a professional translator. "
    "Translate the following text from {source_desc} to {target_name}. "
    "Preserve the original meaning and tone. "
    "Only output the translated text, nothing else."
)


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
        self.system_prompt_template = system_prompt_template or DEFAULT_TRANSLATION_PROMPT
        self._client: Any = None

    @property
    def name(self) -> str:
        return "openai"

    @property
    def cache_identity(self) -> str:
        return "\0".join((self.base_url or "", self.model, self.system_prompt_template))

    def _get_client(self) -> Any:
        if self._client is None:
            # The SDK default waits 600 s and retries twice; a hung endpoint would stall
            # the whole dispatch round for half an hour. A failure delivers the original.
            self._client = make_client(self.api_key, self.base_url, timeout=60)
        return self._client

    async def translate(
        self,
        text: str,
        target_lang: str,
        source_lang: str | None = None,
    ) -> TranslationResult:
        """Translate text using OpenAI API."""
        try:
            client = self._get_client()
            source_desc = (
                language_name(source_lang) if source_lang else "the source language (auto-detect)"
            )
            system_prompt = fill_prompt(
                self.system_prompt_template,
                DEFAULT_TRANSLATION_PROMPT,
                "TRANSLATION_SYSTEM_PROMPT",
                source_desc=source_desc,
                target_name=language_name(target_lang),
            )
            response = await chat_completions_create(
                client,
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0.3,
                max_completion_tokens=2000,
            )

            # content can be None on OpenAI-compatible endpoints (refusals,
            # reasoning models, empty completions). Treat empty as a failure
            # so we don't cache a blank translation — mirrors the digest path.
            translated = (response.choices[0].message.content or "").strip()
            if not translated:
                return TranslationResult(
                    success=False,
                    error="LLM returned empty translation",
                )

            return TranslationResult(
                success=True,
                translated_text=translated,
            )

        except ImportError as e:
            logger.error(f"OpenAI package not installed: {e}")
            return TranslationResult(success=False, error=str(e))
        except Exception as e:
            logger.exception(f"OpenAI translation error: {e}")
            return TranslationResult(
                success=False,
                error=str(e),
            )
