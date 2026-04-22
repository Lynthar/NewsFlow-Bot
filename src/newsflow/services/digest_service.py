"""DigestService: build periodic AI digests from what a channel received."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


def is_due(config: ChannelDigest, now: datetime) -> bool:
    """Whether `config` should generate a digest at `now`.

    The dispatcher loop runs every few minutes, so matching on `.hour`
    means we fire within that slot. The dedupe delta prevents double
    delivery when the loop wakes up twice inside the same slot.
    """
    if not config.enabled:
        return False
    if now.hour != config.delivery_hour_utc:
        return False

    if config.schedule == "weekly":
        if config.delivery_weekday is None:
            return False
        if now.weekday() != config.delivery_weekday:
            return False
        dedupe = _DEDUPE_DELTA_WEEKLY
    elif config.schedule == "daily":
        dedupe = _DEDUPE_DELTA_DAILY
    else:
        logger.warning(
            f"Unknown digest schedule {config.schedule!r} for "
            f"{config.platform}/{config.platform_channel_id}"
        )
        return False

    if config.last_delivered_at is not None:
        elapsed = now - config.last_delivered_at
        if elapsed < dedupe:
            return False

    return True


def _time_window_desc(
    config: ChannelDigest, now: datetime
) -> tuple[datetime, str]:
    """Return (since, description) for the digest input window.

    First-ever digest falls back to 24h / 7d; subsequent digests use the
    real "since last delivered" window to avoid gaps and overlap.
    """
    if config.last_delivered_at is None:
        if config.schedule == "weekly":
            return now - timedelta(days=7), "the past 7 days"
        return now - timedelta(hours=24), "the past 24 hours"
    if config.schedule == "weekly":
        return config.last_delivered_at, "the past week"
    return config.last_delivered_at, "the past day"


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
        now = now or datetime.now(timezone.utc)
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

        return await self.summarizer.generate_digest(
            articles=articles,
            language=config.language,
            time_window_desc=window_desc,
        )
