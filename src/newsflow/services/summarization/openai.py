"""OpenAI (or compatible) digest provider.

Shares the `OPENAI_API_KEY` and `OPENAI_BASE_URL` with the translation
provider; model is configurable separately via `DIGEST_MODEL`. This lets
users point at DeepSeek / Qwen / local LLM endpoints by setting
OPENAI_BASE_URL the same way they do for translation.
"""

import logging
from typing import Any, Sequence

from newsflow.services.summarization.base import (
    DigestArticle,
    DigestResult,
    SummarizationProvider,
    language_name,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_TEMPLATE = """You are a news editor preparing a periodic briefing.

You will be given a list of articles that were delivered to a channel in the \
{window}. Your job is to produce a concise, reader-friendly digest in {lang}.

Rules:
1. Group articles into 3-5 coherent topic clusters. Don't force clusters if \
the set is small; fewer is fine.
2. For each cluster, write 2-4 sentences summarizing the key facts. Cite \
source articles inline by their number, e.g. [1][3].
3. Do NOT speculate. Do NOT add facts that aren't in the provided articles.
4. At the end, list every article you cited as:
   [N] Title — <link>
5. Open with one overview sentence; optionally close with one sentence of \
trend observation (not opinion).
6. Output plain Markdown. No preamble, no meta-commentary, just the digest.
7. The entire digest should be scannable in under 2 minutes."""


class OpenAIDigestProvider(SummarizationProvider):
    """OpenAI-compatible chat completion for digest generation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        system_prompt_template: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.system_prompt_template = (
            system_prompt_template or SYSTEM_PROMPT_TEMPLATE
        )
        self._client: Any = None

    @property
    def name(self) -> str:
        return "openai"

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as e:
                raise ImportError(
                    "openai package is required. Install with: "
                    "pip install 'newsflow-bot[translation-openai]'"
                ) from e
            kwargs: dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    def _format_articles(self, articles: Sequence[DigestArticle]) -> str:
        lines = []
        for idx, art in enumerate(articles, start=1):
            summary = art.summary.replace("\n", " ").strip()
            if len(summary) > 240:
                summary = summary[:237] + "..."
            published = (
                art.published_at.strftime("%Y-%m-%d %H:%M")
                if art.published_at
                else "unknown"
            )
            lines.append(
                f"[{idx}] source={art.source} | published={published} | "
                f"title={art.title} | summary={summary} | link={art.link}"
            )
        return "\n".join(lines)

    async def generate_digest(
        self,
        articles: Sequence[DigestArticle],
        language: str,
        time_window_desc: str,
    ) -> DigestResult:
        if not articles:
            return DigestResult(
                success=False, error="No articles supplied to digest provider"
            )

        lang = language_name(language)
        try:
            system_prompt = self.system_prompt_template.format(
                window=time_window_desc, lang=lang
            )
        except (KeyError, IndexError) as e:
            logger.warning(
                f"digest_system_prompt references unknown placeholder "
                f"{e}; falling back to default"
            )
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                window=time_window_desc, lang=lang
            )
        user_prompt = (
            f"Here are {len(articles)} articles from {time_window_desc}:\n\n"
            + self._format_articles(articles)
        )

        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=2000,
            )
            text = (response.choices[0].message.content or "").strip()
            if not text:
                return DigestResult(
                    success=False, error="LLM returned empty response"
                )
            return DigestResult(success=True, text=text)
        except Exception as e:
            logger.exception(f"OpenAI digest generation failed: {e}")
            return DigestResult(success=False, error=str(e))
