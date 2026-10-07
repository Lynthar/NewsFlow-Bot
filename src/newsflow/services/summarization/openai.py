"""OpenAI (or compatible) digest provider.

Shares the `OPENAI_API_KEY` and `OPENAI_BASE_URL` with the translation
provider; model is configurable separately via `DIGEST_MODEL`. This lets
users point at DeepSeek / Qwen / local LLM endpoints by setting
OPENAI_BASE_URL the same way they do for translation.
"""

import logging
from collections.abc import Sequence
from typing import Any

from newsflow.services.llm import chat_completions_create, fill_prompt, language_name, make_client
from newsflow.services.summarization.base import (
    DigestArticle,
    DigestResult,
    SummarizationProvider,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_TEMPLATE = """You are a news editor preparing a periodic briefing.

Articles will be provided from {window}. Produce a digest in {lang}.

Rules:
1. Cluster by specific topic — let the material decide how many. Prefer \
specific, targeted, key events or subjects from the articles over overly \
broad or general categories/domains.
2. Per cluster: 2-4 sentences of key facts, with inline citations like \
[1][3]. Every factual claim needs at least one citation.
3. Same event across multiple articles = one fact with combined citations \
[1][2][3]. Don't restate it across clusters.
4. No speculation. No facts beyond what the articles say.
5. Open with one overview sentence; optionally close with one factual \
cross-cluster pattern (no opinion).
6. Do NOT write a source list — one is appended automatically from the \
article numbers you cite. Never restate URLs in the text.
7. Plain Markdown only. No preamble, no meta-commentary.
8. Target 1500-3500 characters total. Scannable in under 2 minutes."""


class OpenAIDigestProvider(SummarizationProvider):
    """OpenAI-compatible chat completion for digest generation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        system_prompt_template: str | None = None,
        max_input_chars: int = 300,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.system_prompt_template = system_prompt_template or SYSTEM_PROMPT_TEMPLATE
        self.max_input_chars = max_input_chars
        self._client: Any = None

    @property
    def name(self) -> str:
        return "openai"

    def _get_client(self) -> Any:
        if self._client is None:
            # Bounded, unlike the SDK's 600 s × 3 attempts, yet room for a slow local
            # model to write a full digest; a failed run is retried on the next tick.
            self._client = make_client(self.api_key, self.base_url, timeout=300)
        return self._client

    def _format_articles(self, articles: Sequence[DigestArticle]) -> str:
        lines = []
        limit = max(8, self.max_input_chars)
        for idx, art in enumerate(articles, start=1):
            summary = art.summary.replace("\n", " ").strip()
            if len(summary) > limit:
                summary = summary[: limit - 3] + "..."
            published = (
                art.published_at.strftime("%Y-%m-%d %H:%M") if art.published_at else "unknown"
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
            return DigestResult(success=False, error="No articles supplied to digest provider")

        system_prompt = fill_prompt(
            self.system_prompt_template,
            SYSTEM_PROMPT_TEMPLATE,
            "DIGEST_SYSTEM_PROMPT",
            window=time_window_desc,
            lang=language_name(language),
        )
        user_prompt = (
            f"Here are {len(articles)} articles from {time_window_desc}:\n\n"
            + self._format_articles(articles)
        )

        try:
            client = self._get_client()
            response = await chat_completions_create(
                client,
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                # Body-only budget: the 50-entry source list is appended in code (DigestService),
                # and 3500 chars of CJK body alone can exceed 2000 tokens.
                max_completion_tokens=4000,
            )
            text = (response.choices[0].message.content or "").strip()
            if not text:
                return DigestResult(success=False, error="LLM returned empty response")
            return DigestResult(success=True, text=text)
        except Exception as e:
            logger.exception(f"OpenAI digest generation failed: {e}")
            # The class name only: `/digest now` shows this in the chat, and the
            # endpoint's own text can name internal hosts. The log above has it all.
            return DigestResult(success=False, error=type(e).__name__)
