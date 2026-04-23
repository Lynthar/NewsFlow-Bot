"""OpenAI parameter compatibility shim.

Newer OpenAI models (gpt-5 series, o-series, reasoning models) accept
`max_completion_tokens` and reject the legacy `max_tokens`. Older models
(gpt-4o, gpt-4-turbo, gpt-3.5) still expect `max_tokens`. Some OpenAI-
compatible endpoints (DeepSeek, Qwen, self-hosted vLLM / llama.cpp) fall
on either side depending on version.

The SDK doesn't auto-translate, so a naive call that hard-codes one name
breaks for whichever model flavor the user picked next. This wrapper
tries `max_completion_tokens` first (future-proof) and, on an
"unsupported parameter" 400 naming that argument, swaps to `max_tokens`
and retries.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


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
            logger.info(
                "OpenAI endpoint rejected max_completion_tokens; "
                "retrying with max_tokens"
            )
            kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
            return await client.chat.completions.create(**kwargs)
        raise
