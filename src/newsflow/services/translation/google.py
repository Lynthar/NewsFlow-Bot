"""
Google Cloud Translation provider.

Uses Google Cloud Translation API.
"""

import logging
from typing import Any

from newsflow.services.translation.base import TranslationProvider, TranslationResult

logger = logging.getLogger(__name__)


class GoogleProvider(TranslationProvider):
    """Google Cloud Translation provider."""

    def __init__(
        self,
        credentials_path: str | None = None,
        project_id: str | None = None,
    ) -> None:
        self.credentials_path = credentials_path
        self.project_id = project_id
        self._client: Any = None

    @property
    def name(self) -> str:
        return "google"

    def _get_client(self) -> Any:
        """Lazy initialization of Google Cloud Translation client."""
        if self._client is None:
            try:
                from google.cloud import translate_v2 as translate

                if self.credentials_path:
                    import os

                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.credentials_path

                self._client = translate.Client()
            except ImportError:
                raise ImportError(
                    "google-cloud-translate package is required for Google translation. "
                    "Install it with: pip install google-cloud-translate"
                )
        return self._client

    def normalize_language_code(self, lang_code: str) -> str:
        """Normalize language code for Google."""
        code = lang_code.lower()
        # Google uses 'zh-CN' and 'zh-TW' format
        if code == "zh-cn" or code == "zh-hans":
            return "zh-CN"
        elif code == "zh-tw" or code == "zh-hant":
            return "zh-TW"
        return code

    async def translate(
        self,
        text: str,
        target_lang: str,
        source_lang: str | None = None,
    ) -> TranslationResult:
        """Translate text using Google Cloud Translation API."""
        try:
            client = self._get_client()
            target = self.normalize_language_code(target_lang)

            # Google's translate is sync, run in executor
            import asyncio

            loop = asyncio.get_event_loop()

            kwargs = {
                "values": text,
                "target_language": target,
            }
            if source_lang:
                kwargs["source_language"] = self.normalize_language_code(source_lang)

            result = await loop.run_in_executor(
                None,
                lambda: client.translate(**kwargs),
            )

            # Result is a dict for single text
            if isinstance(result, dict):
                return TranslationResult(
                    success=True,
                    translated_text=result["translatedText"],
                    source_language=result.get("detectedSourceLanguage"),
                )
            else:
                return TranslationResult(
                    success=False,
                    error="Unexpected response format from Google API",
                )

        except ImportError as e:
            logger.error(f"Google Cloud Translation package not installed: {e}")
            return TranslationResult(
                success=False,
                error="google-cloud-translate package not installed. "
                "Install with: pip install google-cloud-translate",
            )
        except Exception as e:
            logger.exception(f"Google translation error: {e}")
            return TranslationResult(
                success=False,
                error=str(e),
            )
