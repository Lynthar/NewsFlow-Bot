"""Tests for env-var-configurable AI prompt templates."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from newsflow.services.summarization.base import DigestArticle
from newsflow.services.summarization.openai import (
    SYSTEM_PROMPT_TEMPLATE,
    OpenAIDigestProvider,
)
from newsflow.services.translation.openai import (
    DEFAULT_TRANSLATION_PROMPT,
    OpenAIProvider as OpenAITranslationProvider,
)


# ===== Translation =====


def test_translation_default_prompt_used_when_none_provided():
    p = OpenAITranslationProvider(api_key="x", model="m")
    assert p.system_prompt_template == DEFAULT_TRANSLATION_PROMPT


def test_translation_custom_prompt_overrides_default():
    custom = "Translate to {target_name}, source: {source_desc}. Be terse."
    p = OpenAITranslationProvider(
        api_key="x", model="m", system_prompt_template=custom
    )
    assert p.system_prompt_template == custom


async def test_translation_custom_prompt_is_used_in_api_call():
    custom = "From {source_desc}. Target: {target_name}. Just translate."
    p = OpenAITranslationProvider(
        api_key="x", model="m", system_prompt_template=custom
    )

    # Mock OpenAI client
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = "translated"
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    p._client = fake_client

    await p.translate("hello", target_lang="zh-CN", source_lang="en")

    sent_system = fake_client.chat.completions.create.await_args.kwargs[
        "messages"
    ][0]["content"]
    assert sent_system == (
        "From English. Target: Simplified Chinese. Just translate."
    )


async def test_translation_broken_template_falls_back_to_default():
    broken = "This uses {nonexistent_placeholder}"
    p = OpenAITranslationProvider(
        api_key="x", model="m", system_prompt_template=broken
    )

    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = "ok"
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    p._client = fake_client

    result = await p.translate("hi", target_lang="zh-CN", source_lang="en")

    assert result.success is True
    sent_system = fake_client.chat.completions.create.await_args.kwargs[
        "messages"
    ][0]["content"]
    # Fell back to DEFAULT (which references source_desc + target_name)
    assert "English" in sent_system
    assert "Simplified Chinese" in sent_system
    assert "{nonexistent_placeholder}" not in sent_system


async def test_translation_auto_detect_source():
    p = OpenAITranslationProvider(api_key="x", model="m")

    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = "ok"
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    p._client = fake_client

    await p.translate("hi", target_lang="zh-CN", source_lang=None)

    sent = fake_client.chat.completions.create.await_args.kwargs["messages"][0][
        "content"
    ]
    assert "auto-detect" in sent


# ===== Digest =====


def test_digest_default_prompt_used_when_none_provided():
    p = OpenAIDigestProvider(api_key="x", model="m")
    assert p.system_prompt_template == SYSTEM_PROMPT_TEMPLATE


def test_digest_custom_prompt_overrides_default():
    custom = "You are a terse editor. Window: {window}. Output in {lang}."
    p = OpenAIDigestProvider(
        api_key="x", model="m", system_prompt_template=custom
    )
    assert p.system_prompt_template == custom


async def test_digest_custom_prompt_is_used_in_api_call():
    custom = "Summarize the past {window} in {lang}. Keep under 200 words."
    p = OpenAIDigestProvider(
        api_key="x", model="m", system_prompt_template=custom
    )

    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = "short digest"
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    p._client = fake_client

    article = DigestArticle(
        title="T", summary="S", link="https://x", source="X",
        published_at=datetime.now(timezone.utc),
    )
    await p.generate_digest([article], language="zh-CN", time_window_desc="past 24 hours")

    sent_system = fake_client.chat.completions.create.await_args.kwargs[
        "messages"
    ][0]["content"]
    assert sent_system == (
        "Summarize the past past 24 hours in Simplified Chinese. "
        "Keep under 200 words."
    )


async def test_digest_broken_template_falls_back_to_default():
    broken = "Something {missing_key}"
    p = OpenAIDigestProvider(
        api_key="x", model="m", system_prompt_template=broken
    )

    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = "ok"
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    p._client = fake_client

    article = DigestArticle(
        title="T", summary="S", link="https://x", source="X",
        published_at=None,
    )
    result = await p.generate_digest([article], language="en", time_window_desc="past day")

    assert result.success is True
    sent_system = fake_client.chat.completions.create.await_args.kwargs[
        "messages"
    ][0]["content"]
    assert "{missing_key}" not in sent_system
    # Default prompt mentions "news editor"
    assert "editor" in sent_system.lower()


# ===== Factory wiring =====


def test_translation_factory_passes_custom_prompt_from_settings():
    from newsflow.services.translation.factory import create_translation_provider

    with patch(
        "newsflow.services.translation.factory.get_settings"
    ) as mock_settings:
        fake = MagicMock()
        fake.can_translate.return_value = True
        fake.translation_provider = "openai"
        fake.openai_api_key = "test-key"
        fake.openai_model = "test-model"
        fake.openai_base_url = None
        fake.translation_system_prompt = "My custom prompt {target_name}"
        mock_settings.return_value = fake

        provider = create_translation_provider()

    assert provider is not None
    assert provider.system_prompt_template == "My custom prompt {target_name}"


def test_digest_factory_passes_custom_prompt_from_settings():
    from newsflow.services.summarization.factory import (
        get_summarizer,
        reset_summarizer,
    )

    reset_summarizer()
    with patch(
        "newsflow.services.summarization.factory.get_settings"
    ) as mock_settings:
        fake = MagicMock()
        fake.digest_provider = "openai"
        fake.openai_api_key = "test-key"
        fake.digest_model = "test-model"
        fake.openai_base_url = None
        fake.digest_system_prompt = "Editor. {window} in {lang}."
        mock_settings.return_value = fake

        provider = get_summarizer()

    assert provider is not None
    assert provider.system_prompt_template == "Editor. {window} in {lang}."
    reset_summarizer()
