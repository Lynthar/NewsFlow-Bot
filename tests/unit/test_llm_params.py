"""What the OpenAI-compatible callers send for each reasoning effort."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from newsflow.config import Settings
from newsflow.services.summarization.base import DigestArticle
from newsflow.services.summarization.factory import get_summarizer, reset_summarizer
from newsflow.services.summarization.openai import OpenAIDigestProvider
from newsflow.services.translation.factory import create_translation_provider
from newsflow.services.translation.openai import OpenAIProvider


def _fake_client() -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "ok"
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


async def _translation_request(provider: Any) -> dict[str, Any]:
    provider._client = _fake_client()
    result = await provider.translate("hi", target_lang="zh-CN", source_lang="en")
    assert result.success is True
    return dict(provider._client.chat.completions.create.await_args.kwargs)


async def _digest_request(provider: Any) -> dict[str, Any]:
    provider._client = _fake_client()
    article = DigestArticle(title="T", summary="S", link="https://x", source="X", published_at=None)
    result = await provider.generate_digest([article], language="en", time_window_desc="past day")
    assert result.success is True
    return dict(provider._client.chat.completions.create.await_args.kwargs)


async def test_effort_none_keeps_the_temperature_and_the_plain_token_cap():
    sent = await _translation_request(OpenAIProvider(api_key="k", reasoning_effort="none"))

    assert sent["reasoning_effort"] == "none"
    assert sent["temperature"] == 0.3
    assert sent["max_completion_tokens"] == 2000


async def test_an_empty_effort_sends_no_reasoning_parameter():
    # Endpoints that reject reasoning_effort get the request they got before it existed.
    sent = await _translation_request(OpenAIProvider(api_key="k", reasoning_effort=""))

    assert "reasoning_effort" not in sent
    assert sent["temperature"] == 0.3
    assert sent["max_completion_tokens"] == 2000


async def test_reasoning_drops_the_temperature_and_leaves_room_for_the_answer():
    # OpenAI refuses a non-default temperature once reasoning is on, and bills the
    # reasoning against max_completion_tokens before the body starts.
    provider = OpenAIDigestProvider(api_key="k", model="m", reasoning_effort="low")

    sent = await _digest_request(provider)

    assert sent["reasoning_effort"] == "low"
    assert "temperature" not in sent
    assert sent["max_completion_tokens"] > 4000


async def test_default_settings_send_each_model_an_effort_it_accepts(configure):
    # gpt-6-luna refuses temperature 0.3 at its default effort; gpt-6.1-sol refuses "none".
    configure(translation_enabled=True, translation_provider="openai", openai_api_key="k")
    reset_summarizer()
    try:
        translation = await _translation_request(create_translation_provider())
        digest = await _digest_request(get_summarizer())
    finally:
        reset_summarizer()

    assert translation["model"] == "gpt-6-luna"
    assert translation["reasoning_effort"] == "none"
    assert translation["temperature"] == 0.3
    assert digest["model"] == "gpt-6.1-sol"
    assert digest["reasoning_effort"] == "low"
    assert "temperature" not in digest


async def test_configured_efforts_reach_both_requests(configure):
    configure(
        translation_enabled=True,
        translation_provider="openai",
        openai_api_key="k",
        openai_reasoning_effort="",
        digest_reasoning_effort=" High ",
    )
    reset_summarizer()
    try:
        translation = await _translation_request(create_translation_provider())
        digest = await _digest_request(get_summarizer())
    finally:
        reset_summarizer()

    assert "reasoning_effort" not in translation
    assert digest["reasoning_effort"] == "high"


@pytest.mark.parametrize("field", ["openai_reasoning_effort", "digest_reasoning_effort"])
def test_an_unknown_effort_is_refused_at_startup(field):
    # Sent as is, a typo would be refused on every call.
    with pytest.raises(ValidationError, match=field):
        Settings(**{field: "nnoe"})


def test_cached_translations_are_not_shared_across_efforts():
    none = OpenAIProvider(api_key="k", reasoning_effort="none")
    low = OpenAIProvider(api_key="k", reasoning_effort="low")

    assert none.cache_identity != low.cache_identity
