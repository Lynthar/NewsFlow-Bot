"""
DeepL translation provider.

Uses the DeepL API for high-quality translations.
"""

import logging
from typing import Any

from newsflow.services.translation.base import TranslationProvider, TranslationResult

logger = logging.getLogger(__name__)

# DeepL supported languages
DEEPL_LANGUAGES = {
    # Target languages
    "bg": "BG",  # Bulgarian
    "cs": "CS",  # Czech
    "da": "DA",  # Danish
    "de": "DE",  # German
    "el": "EL",  # Greek
    "en": "EN",  # English (unspecified)
    "en-gb": "EN-GB",  # British English
    "en-us": "EN-US",  # American English
    "es": "ES",  # Spanish
    "et": "ET",  # Estonian
    "fi": "FI",  # Finnish
    "fr": "FR",  # French
    "hu": "HU",  # Hungarian
    "id": "ID",  # Indonesian
    "it": "IT",  # Italian
    "ja": "JA",  # Japanese
    "ko": "KO",  # Korean
    "lt": "LT",  # Lithuanian
    "lv": "LV",  # Latvian
    "nb": "NB",  # Norwegian Bokmål
    "nl": "NL",  # Dutch
    "pl": "PL",  # Polish
    "pt": "PT",  # Portuguese (unspecified)
    "pt-br": "PT-BR",  # Brazilian Portuguese
    "pt-pt": "PT-PT",  # European Portuguese
    "ro": "RO",  # Romanian
    "ru": "RU",  # Russian
    "sk": "SK",  # Slovak
    "sl": "SL",  # Slovenian
    "sv": "SV",  # Swedish
    "tr": "TR",  # Turkish
    "uk": "UK",  # Ukrainian
    "zh": "ZH",  # Chinese (simplified)
    "zh-cn": "ZH",  # Chinese Simplified
    "zh-hans": "ZH",  # Chinese Simplified
}


class DeepLProvider(TranslationProvider):
    """DeepL translation provider."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._translator: Any = None

    @property
    def name(self) -> str:
        return "deepl"

    def _get_translator(self) -> Any:
        """Lazy initialization of DeepL translator."""
        if self._translator is None:
            try:
                import deepl

                self._translator = deepl.Translator(self.api_key)
            except ImportError:
                raise ImportError(
                    "deepl package is required for DeepL translation. "
                    "Install it with: pip install deepl"
                )
        return self._translator

    def normalize_language_code(self, lang_code: str) -> str:
        """Convert language code to DeepL format."""
        code = lang_code.lower()
        return DEEPL_LANGUAGES.get(code, code.upper())

    def supports_language(self, lang_code: str) -> bool:
        """Check if DeepL supports the language."""
        return lang_code.lower() in DEEPL_LANGUAGES

    async def translate(
        self,
        text: str,
        target_lang: str,
        source_lang: str | None = None,
    ) -> TranslationResult:
        """Translate text using DeepL API."""
        try:
            translator = self._get_translator()
            target = self.normalize_language_code(target_lang)

            source = None
            if source_lang:
                source = self.normalize_language_code(source_lang)

            # DeepL's translate_text is sync, run in executor
            import asyncio

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: translator.translate_text(
                    text,
                    target_lang=target,
                    source_lang=source,
                ),
            )

            return TranslationResult(
                success=True,
                translated_text=result.text,
                source_language=result.detected_source_lang,
            )

        except ImportError as e:
            logger.error(f"DeepL package not installed: {e}")
            return TranslationResult(
                success=False,
                error="DeepL package not installed. Install with: pip install deepl",
            )
        except Exception as e:
            logger.exception(f"DeepL translation error: {e}")
            return TranslationResult(
                success=False,
                error=str(e),
            )
