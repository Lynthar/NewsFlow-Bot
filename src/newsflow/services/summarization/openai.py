"""OpenAI (or compatible) digest provider.

Shares the `OPENAI_API_KEY` and `OPENAI_BASE_URL` with the translation
provider; model is configurable separately via `DIGEST_MODEL`. This lets
users point at DeepSeek / Qwen / local LLM endpoints by setting
OPENAI_BASE_URL the same way they do for translation.
"""

import logging
from collections.abc import Sequence
from typing import Any

from newsflow.services.llm import (
    chat_completions_create,
    fill_prompt,
    generation_params,
    language_name,
    make_client,
)
from newsflow.services.summarization.base import (
    DigestArticle,
    DigestResult,
    SummarizationProvider,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_TEMPLATE = """You are a senior editor writing a periodic briefing in {lang}.

Your readers receive every article in this channel but had no time to read \
them. The briefing is how they catch up, and the citations are how they dig \
into what interests them. Articles from {window} are provided below, \
numbered [N].

Your job is synthesis, not coverage. Do not try to mention every article — \
leave out anything that does not change the overall picture. A longer \
window calls for more altitude, not more length: a day is about what \
changed, a week is about where things are heading.

Structure:
1. Bottom line first: 2-3 sentences on what this period was really about \
and the one or two things that matter most.
2. Then 3-5 threads, fewer if the material is thin. A thread is a \
development or theme, never a single article. For each: what happened in \
one or two sentences, then why it matters — what it changes, for whom, and \
where it is heading. Cite the one or two articles worth reading on it, like \
[4][9]; the rest need not be cited.
3. Close with "What to watch": 2-4 open questions or upcoming developments \
that follow from the material.

Judgment is welcome, but keep it honest:
- Facts come only from the articles and carry [N] citations. Never invent \
details.
- Interpretation is yours. Mark it as the editor's read and never attribute \
it to a source.
- Be specific. A judgment that names nothing concrete ("the field is moving \
fast") is worthless; if you cannot say what changes for whom, say nothing.
- You see only titles and short excerpts. When evidence is thin or sources \
disagree, say so instead of smoothing it over.
- Several articles repeating one announcement are one observation, not \
several.

Plain Markdown with short headings. No source list — one is attached from \
the [N] you cite. Never write URLs. No preamble or meta-commentary. Target \
1500-3500 characters total."""


class OpenAIDigestProvider(SummarizationProvider):
    """OpenAI-compatible chat completion for digest generation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        system_prompt_template: str | None = None,
        max_input_chars: int = 300,
        reasoning_effort: str = "",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.reasoning_effort = reasoning_effort
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
            # One line per article: a newline in a feed's title could forge another [N].
            title, link = " ".join(art.title.split()), " ".join(art.link.split())
            lines.append(
                f"[{idx}] source={art.source} | published={published} | "
                f"title={title} | summary={summary} | link={link}"
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
                # Body-only budget: the 50-entry source list is appended in code (DigestService),
                # and 3500 chars of CJK body alone can exceed 2000 tokens.
                **generation_params(self.reasoning_effort, temperature=0.3, max_output=4000),
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
