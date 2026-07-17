"""DigestService: build periodic AI digests from what a channel received."""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.core.content_processor import clean_html, get_source_name
from newsflow.models.digest import ChannelDigest
from newsflow.repositories.digest_repository import ChannelDigestRepository
from newsflow.services.summarization import (
    DigestArticle,
    DigestResult,
    SummarizationProvider,
)

logger = logging.getLogger(__name__)


# Safety margin: even if schedule matches and we're past the hour, don't
# re-fire within this span of the last successful delivery. Protects
# against clock skew / loop re-entry.
_DEDUPE_DELTA_DAILY = timedelta(hours=23)
_DEDUPE_DELTA_WEEKLY = timedelta(days=6)


@dataclass
class DigestDeliveryResult:
    success: bool
    article_count: int = 0
    message: str = ""


# Inline citation as taught by the digest prompt: [3] or [1][4].
_CITATION_RE = re.compile(r"\[(\d+)\]")
# A line of the OLD prompt's LLM-written source list: `[N] Title — <https://…>`.
# Only used to strip a disobedient (or custom-prompted) model's own trailing
# list so the code-built one below isn't duplicated.
_SOURCE_LINE_RE = re.compile(r"^\s*\[\d+\]\s.*<https?://\S+>\s*$")

# Header for the appended source list, keyed by primary language subtag.
_SOURCES_HEADERS = {"zh": "来源", "ja": "出典", "ko": "출처"}

_SOURCE_TITLE_MAX = 80


def _sources_header(language: str) -> str:
    return _SOURCES_HEADERS.get(language.split("-")[0].lower(), "Sources")


def strip_llm_source_list(text: str) -> str:
    """Drop a trailing model-written source list (old-format lines only).

    The prompt says not to write one, but a custom `digest_system_prompt`
    may still teach the old rule — appending ours on top would double the
    list. Only trailing lines in the exact taught format are removed;
    body text never ends with `<https://…>` so false positives are nil.
    When such lines were removed, a short heading right above them
    ("**Sources**", "来源:") goes too, so the code-built header isn't
    doubled either.
    """
    lines = text.rstrip().split("\n")
    removed = 0
    while lines and (_SOURCE_LINE_RE.match(lines[-1]) or not lines[-1].strip()):
        if lines[-1].strip():
            removed += 1
        lines.pop()
    if removed and lines:
        tail = lines[-1].strip()
        if len(tail) <= 40 and (tail.startswith(("**", "#")) or tail.endswith((":", "："))):
            lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines)


def build_source_list(text: str, articles: Sequence[DigestArticle]) -> str:
    """Deterministic source list for the article numbers `text` cites.

    The LLM only emits inline `[N]` citations; reproducing 50 URLs
    verbatim is exactly what models truncate and hallucinate, so the
    list itself is built here from the real article array. Numbers keep
    the prompt's enumeration so citations stay resolvable. If the model
    cited nothing recognizable, fall back to listing every input article
    rather than delivering a digest with no sources at all.
    """
    cited = sorted(
        {n for n in (int(m) for m in _CITATION_RE.findall(text)) if 1 <= n <= len(articles)}
    )
    indices = cited or list(range(1, len(articles) + 1))
    lines = []
    for n in indices:
        art = articles[n - 1]
        title = art.title
        if len(title) > _SOURCE_TITLE_MAX:
            title = title[: _SOURCE_TITLE_MAX - 1] + "…"
        # Angle brackets suppress Discord link previews; the Telegram
        # digest renderer unwraps them and disables previews API-side.
        lines.append(f"[{n}] {title} — <{art.link}>")
    return "\n".join(lines)


def append_source_list(text: str, articles: Sequence[DigestArticle], language: str) -> str:
    """Digest body + localized header + code-built source list."""
    body = strip_llm_source_list(text)
    sources = build_source_list(body, articles)
    return f"{body}\n\n**{_sources_header(language)}**\n{sources}"


def _most_recent_slot(config: ChannelDigest, now: datetime) -> datetime | None:
    """The latest scheduled delivery time ≤ `now` (UTC), or None when the
    schedule is unconfigurable (weekly without a weekday)."""
    slot = now.replace(hour=config.delivery_hour_utc, minute=0, second=0, microsecond=0)
    if config.schedule == "weekly":
        if config.delivery_weekday is None:
            return None
        slot -= timedelta(days=(now.weekday() - config.delivery_weekday) % 7)
        if slot > now:  # right weekday, but the hour hasn't arrived yet
            slot -= timedelta(days=7)
    else:  # daily
        if slot > now:
            slot -= timedelta(days=1)
    return slot


def is_due(config: ChannelDigest, now: datetime) -> bool:
    """Whether `config` should generate a digest at `now`.

    Slot-based with catch-up: due when the most recent scheduled slot has
    not been served yet — so a process that was down (or an adapter that
    wasn't registered) across the delivery hour delivers late instead of
    silently skipping to the next day/week. The dedupe delta additionally
    keeps a very recent delivery (e.g. a manual /digest now shortly before
    the slot) from double-firing inside the same period.

    First-ever delivery (last_delivered_at is None) intentionally keeps
    the old in-slot-hour behavior: enabling a digest at 14:00 for a 09:00
    slot waits for tomorrow 09:00 rather than firing immediately.
    """
    if not config.enabled:
        return False
    if config.schedule == "weekly":
        dedupe = _DEDUPE_DELTA_WEEKLY
    elif config.schedule == "daily":
        dedupe = _DEDUPE_DELTA_DAILY
    else:
        logger.warning(
            f"Unknown digest schedule {config.schedule!r} for "
            f"{config.platform}/{config.platform_channel_id}"
        )
        return False

    slot = _most_recent_slot(config, now)
    if slot is None:
        return False

    if config.last_delivered_at is None:
        if config.schedule == "weekly" and now.weekday() != config.delivery_weekday:
            return False
        return now.hour == config.delivery_hour_utc

    # SQLite + aiosqlite drops tzinfo on read even though the column
    # is DateTime(timezone=True). Treat naive values as UTC so the
    # comparisons below don't blow up the whole digest tick.
    last = config.last_delivered_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)

    if last >= slot:
        return False  # this slot was already served (incl. empty-window marks)
    return (now - last) >= dedupe


def _time_window_desc(config: ChannelDigest, now: datetime) -> tuple[datetime, str]:
    """Return (since, description) for the digest input window.

    First-ever digest falls back to 24h / 7d; subsequent digests use the
    real "since last delivered" window to avoid gaps and overlap.
    """
    if config.last_delivered_at is None:
        if config.schedule == "weekly":
            return now - timedelta(days=7), "the past 7 days"
        return now - timedelta(hours=24), "the past 24 hours"
    # See is_due() — SQLite drops tzinfo on read; re-attach UTC so
    # downstream comparisons stay consistent.
    last = config.last_delivered_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if config.schedule == "weekly":
        return last, "the past week"
    return last, "the past day"


class DigestService:
    def __init__(
        self,
        session: AsyncSession,
        summarizer: SummarizationProvider,
    ) -> None:
        self.session = session
        self.summarizer = summarizer
        self.repo = ChannelDigestRepository(session)

    async def generate(
        self,
        config: ChannelDigest,
        now: datetime | None = None,
    ) -> DigestResult | None:
        """Generate digest text. Returns None if there's nothing to say."""
        now = now or datetime.now(UTC)
        since, window_desc = _time_window_desc(config, now)

        entries = await self.repo.get_channel_articles(
            platform=config.platform,
            channel_id=config.platform_channel_id,
            since=since,
            until=now,
            include_filtered=config.include_filtered,
            limit=config.max_articles,
        )
        if not entries:
            logger.info(
                f"No articles in digest window for "
                f"{config.platform}/{config.platform_channel_id}; skipping"
            )
            return None

        # Build DigestArticle DTOs: HTML-strip summary for cleaner prompts.
        lang_hint = "zh" if config.language.startswith("zh") else "en"
        articles = []
        for e in entries:
            raw_body = e.content or e.summary or ""
            plain, _ = clean_html(raw_body)
            articles.append(
                DigestArticle(
                    title=e.title,
                    summary=plain,
                    link=e.link,
                    source=get_source_name(e.link, lang_hint),
                    published_at=e.published_at,
                )
            )

        result = await self.summarizer.generate_digest(
            articles=articles,
            language=config.language,
            time_window_desc=window_desc,
        )
        # The provider returns the digest body only; the source list is
        # appended here in code so URLs are never left to the model to
        # reproduce (they get truncated/hallucinated past a dozen links).
        if result.success and result.text:
            result.text = append_source_list(result.text, articles, config.language)
        return result
