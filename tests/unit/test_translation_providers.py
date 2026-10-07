"""What each translation provider hands its vendor SDK. The SDK client is the
network boundary, so a stand-in records the call instead of sending it."""

from typing import Any

import pytest

from newsflow.core.languages import LANGUAGE_CODE_EXAMPLES
from newsflow.services.summarization.openai import OpenAIDigestProvider
from newsflow.services.translation.deepl import DeepLProvider
from newsflow.services.translation.google import GoogleProvider
from newsflow.services.translation.openai import OpenAIProvider


class _RecordingGoogleClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def translate(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"translatedText": "x", "detectedSourceLanguage": "en"}


async def test_google_translates_plain_text_not_html():
    # As HTML, quotes and ampersands come back as entities and reach users verbatim.
    provider = GoogleProvider()
    client = _RecordingGoogleClient()
    provider._client = client

    result = await provider.translate('"Tom & Jerry"', "zh-CN")

    assert result.success is True
    assert client.calls[0]["format_"] == "text"


@pytest.mark.parametrize(
    ("code", "deepl"),
    [
        ("en", "EN-US"),
        ("pt", "PT-BR"),
        ("en-GB", "EN-GB"),
        ("zh", "ZH-HANS"),
        ("zh-CN", "ZH-HANS"),
        ("zh-TW", "ZH-HANT"),
        ("zh-HK", "ZH-HANT"),
    ],
)
def test_deepl_target_codes(code, deepl):
    assert DeepLProvider("k").normalize_language_code(code) == deepl


def test_deepl_accepts_every_language_the_commands_suggest():
    # The SDK refuses EN and PT outright, and DeepL has no ZH-TW.
    for code in (c.strip() for c in LANGUAGE_CODE_EXAMPLES.split(",")):
        assert DeepLProvider("k").normalize_language_code(code) not in {"EN", "PT", "ZH-TW"}


@pytest.mark.parametrize(
    ("provider", "ceiling_s"),
    [(OpenAIProvider(api_key="k"), 60), (OpenAIDigestProvider(api_key="k", model="m"), 300)],
)
def test_llm_calls_give_up_in_bounded_time(provider, ceiling_s):
    # The SDK default (600 s, two retries) lets one hung endpoint stall a round for 30 min.
    client = provider._get_client()
    assert client.timeout <= ceiling_s
    assert client.max_retries <= 1
