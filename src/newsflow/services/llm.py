"""Plumbing shared by the OpenAI-compatible LLM callers (translation and digests): the
client, prompt filling, language names and the max_tokens / max_completion_tokens shim."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Keys are lower-case; language_name() lowers its argument.
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


def language_name(code: str) -> str:
    """The language's English name for a prompt; an unknown code is returned as is."""
    return LANGUAGE_NAMES.get(code.lower(), code)


def make_client(api_key: str, base_url: str | None, *, timeout: float) -> Any:
    """An AsyncOpenAI client that gives up after `timeout` seconds and one retry.

    Raises:
        ImportError: the openai package is not installed; the message names the extra.
    """
    try:
        from openai import AsyncOpenAI
    except ImportError as e:
        raise ImportError(
            "openai package is required. Install with: "
            "pip install 'newsflow-bot[translation-openai]'"
        ) from e
    kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 1}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


def fill_prompt(template: str, default: str, setting: str, **values: str) -> str:
    """`template` with `values` filled in, else `default`: a configured prompt with an
    unknown placeholder or a stray brace must not stop every call that uses it."""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError) as e:
        logger.warning(f"{setting} cannot be filled ({type(e).__name__}: {e}); using the default")
        return default.format(**values)


# Newer models (gpt-5, o-series) reject max_tokens and older ones reject
# max_completion_tokens; compatible endpoints fall on either side by version.
async def chat_completions_create(client: Any, **kwargs: Any) -> Any:
    """`client.chat.completions.create(**kwargs)` with max_tokens /
    max_completion_tokens auto-compat.

    Callers may pass either parameter name; we normalise to the newer
    one first. If the server rejects that specifically, retry with the
    legacy one. Any other BadRequestError is re-raised as-is.
    """
    # Normalise to the new name for the first attempt.
    if "max_tokens" in kwargs and "max_completion_tokens" not in kwargs:
        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")

    # Import here so the module can be imported even when `openai` isn't
    # installed — the actual call will fail with the original ImportError
    # at the call site's `_get_client()` rather than here.
    from openai import BadRequestError

    try:
        return await client.chat.completions.create(**kwargs)
    except BadRequestError as e:
        err_msg = str(e)
        if (
            "max_completion_tokens" in err_msg
            and "unsupported_parameter" in err_msg
            and "max_completion_tokens" in kwargs
        ):
            # The server speaks the old dialect. Swap and retry.
            logger.info("OpenAI endpoint rejected max_completion_tokens; retrying with max_tokens")
            kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
            return await client.chat.completions.create(**kwargs)
        raise
