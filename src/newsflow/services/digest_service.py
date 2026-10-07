"""DigestService: build periodic AI digests from what a channel received."""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.core.content_processor import clean_html, get_source_name
from newsflow.core.timezones import local_schedule_to_utc
from newsflow.models.base import get_session_factory
from newsflow.models.digest import WEEKLY_WINDOW, ChannelDigest
from newsflow.repositories.digest_repository import ChannelDigestRepository
from newsflow.services.summarization import (
    DigestArticle,
    DigestResult,
    SummarizationProvider,
)

if TYPE_CHECKING:
    from newsflow.services.dispatcher import Dispatcher

logger = logging.getLogger(__name__)


# Attempts at recording a delivered digest. Each waits out the busy timeout on a locked
# SQLite, and a record that never lands only costs an overlapping window next period.
_MARK_ATTEMPTS = 3


# What one digest run ended up doing. Callers phrase it for their platform —
# the scheduler logs it, the two /digest now handlers reply with it.
DigestRunStatus = Literal[
    "no_adapter",
    "no_config",
    "not_due",
    "no_articles",
    "generation_failed",
    "delivery_failed",
    "delivered",
]


@dataclass
class DigestMaterial:
    """One digest's input, read from the database before the LLM is called."""

    articles: list[DigestArticle]
    window_desc: str


@dataclass
class DigestDeliveryResult:
    """Outcome of one digest run: generate, deliver, record."""

    status: DigestRunStatus
    chunks: int = 0
    error: str | None = None
    # The digest landed but the delivery record did not; the next scheduled
    # run may therefore resend it.
    mark_failed: bool = False


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
    """Whether `config` should generate a digest at `now`: while its most recent slot is
    unserved, so a slot missed while the process was down is delivered late, not skipped.
    Only scheduled runs serve a slot; enabling marks the current one served."""
    if not config.enabled:
        return False
    if config.schedule not in ("daily", "weekly"):
        logger.warning(
            f"Unknown digest schedule {config.schedule!r} for "
            f"{config.platform}/{config.platform_channel_id}"
        )
        return False

    slot = _most_recent_slot(config, now)
    if slot is None:
        return False

    if config.last_slot_at is None:
        # A config enabled before slots were recorded and never delivered since.
        if config.schedule == "weekly" and now.weekday() != config.delivery_weekday:
            return False
        return now.hour == config.delivery_hour_utc

    return _as_utc(config.last_slot_at) < slot


def _as_utc(value: datetime) -> datetime:
    """SQLite + aiosqlite drops tzinfo on read even though the column is
    DateTime(timezone=True); naive values are UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _time_window_desc(config: ChannelDigest, now: datetime) -> tuple[datetime, str]:
    """Return (since, description) for the digest input window.

    First-ever digest falls back to 24h / 7d; subsequent digests use the
    real "since last delivered" window to avoid gaps and overlap.
    """
    if config.last_delivered_at is None:
        if config.schedule == "weekly":
            return now - WEEKLY_WINDOW, "the past 7 days"
        return now - timedelta(hours=24), "the past 24 hours"
    last = _as_utc(config.last_delivered_at)
    if config.schedule == "weekly":
        return last, "the past week"
    return last, "the past day"


async def _record_delivery(
    config_id: int, now: datetime, slot: datetime | None, pin_id: str | None
) -> bool:
    """Record a delivered digest, retrying in fresh sessions. Never raises: the digest is
    already on-platform, and dying here would lose that fact. Returns whether it landed."""
    for attempt in range(1, _MARK_ATTEMPTS + 1):
        try:
            async with get_session_factory()() as session:
                await ChannelDigestRepository(session).mark_delivered(
                    config_id, now, slot=slot, pinned_message_id=pin_id
                )
                await session.commit()
            return True
        except Exception:
            logger.warning(
                f"Recording digest {config_id} failed (attempt {attempt})", exc_info=True
            )
    return False


async def get_digest_config(
    session: AsyncSession, platform: str, channel_id: str
) -> ChannelDigest | None:
    return await ChannelDigestRepository(session).get(platform, channel_id)


async def enable_digest(
    session: AsyncSession,
    platform: str,
    channel_id: str,
    guild_id: str | None,
    *,
    schedule: str,
    local_hour: int,
    local_weekday: int | None,
    tz: tzinfo,
    **fields: Any,
) -> ChannelDigest:
    """Enable or reconfigure a channel's digest; `fields` not given keep their stored value.
    The local schedule becomes UTC once, here, so a later DST change shifts local delivery.
    The current slot counts as served: enabling never fires a delivery at once."""
    utc_hour, utc_weekday = local_schedule_to_utc(local_hour, local_weekday, tz)
    config = await ChannelDigestRepository(session).upsert(
        platform,
        channel_id,
        guild_id,
        enabled=True,
        schedule=schedule,
        delivery_hour_utc=utc_hour,
        delivery_weekday=utc_weekday,
        **fields,
    )
    config.last_slot_at = _most_recent_slot(config, datetime.now(UTC))
    await session.flush()
    return config


async def disable_digest(session: AsyncSession, platform: str, channel_id: str) -> bool:
    """Turn a channel's digest off, keeping its settings. False when none is configured."""
    config = await ChannelDigestRepository(session).get(platform, channel_id)
    if config is None:
        return False
    config.enabled = False
    await session.flush()
    return True


class DigestService:
    def __init__(
        self,
        session: AsyncSession,
        summarizer: SummarizationProvider,
    ) -> None:
        self.session = session
        self.summarizer = summarizer
        self.repo = ChannelDigestRepository(session)

    @staticmethod
    async def run_now(
        dispatcher: "Dispatcher",
        platform: str,
        channel_id: str,
        summarizer: SummarizationProvider,
        now: datetime,
        *,
        scheduled: bool,
    ) -> DigestDeliveryResult:
        """Generate one channel's digest, deliver it, record the delivery — the
        single entry for the scheduler and both /digest now handlers.

        A `scheduled` run re-checks that its slot is still due once it holds the
        channel, and an empty window consumes the slot. A manual run is a preview:
        no mention, no pin, nothing recorded, so the scheduled digest still comes.

        Raises:
            ChannelGoneError, ChannelMigratedError: propagated from delivery so
                the scheduler can deactivate or repoint the channel.
        """
        adapter = dispatcher.get_adapter(platform)
        if adapter is None:
            return DigestDeliveryResult("no_adapter")

        session_factory = get_session_factory()
        # Two runs for one channel would read the same window and both deliver it,
        # and the slower one would write its older time back over the newer.
        async with dispatcher.digest_lock(platform, channel_id):
            # The window is read while no delivery is in flight: a sent-mark flushed
            # but not committed is invisible here, and the window would then move
            # past it for good. Sessions end before the LLM call and the send.
            async with dispatcher.delivery_lock, session_factory() as session:
                service = DigestService(session, summarizer)
                config = await service.repo.get(platform, channel_id)
                if config is None:
                    return DigestDeliveryResult("no_config")
                slot = _most_recent_slot(config, now)
                if scheduled and (
                    not is_due(config, now) or dispatcher.digest_slot_served(config.id, slot)
                ):
                    return DigestDeliveryResult("not_due")

                config_id = config.id
                prior_pin_id = config.last_pinned_message_id
                language = config.language
                material = await service.collect(config, now)
                if material is None:
                    if scheduled:
                        await service.repo.mark_delivered(config_id, now, slot=slot)
                        await session.commit()
                    return DigestDeliveryResult("no_articles")

            result = await service.summarize(language, material)
            if not result.success:
                return DigestDeliveryResult("generation_failed", error=result.error)
            digest_text = dispatcher.apply_digest_header(result.text, platform, mention=scheduled)

            chunks, new_pin_id = await dispatcher.deliver_digest(
                adapter,
                channel_id,
                digest_text,
                chunk_size=adapter.digest_chunk_size,
                prior_pin_id=prior_pin_id,
                pin=scheduled,
            )
            if chunks == 0:
                return DigestDeliveryResult("delivery_failed")
            if not scheduled:
                return DigestDeliveryResult("delivered", chunks=chunks)

            # Served even if the record below never lands: the next tick must not resend.
            dispatcher.record_digest_slot(config_id, slot)
            mark_failed = not await _record_delivery(config_id, now, slot, new_pin_id)
            if mark_failed:
                logger.error(
                    f"mark_delivered failed for {platform}/{channel_id}; the digest was "
                    f"delivered ({chunks} chunks) but the stored state is stale"
                )

        return DigestDeliveryResult("delivered", chunks=chunks, mark_failed=mark_failed)

    async def collect(self, config: ChannelDigest, now: datetime) -> DigestMaterial | None:
        """The articles `config`'s channel received in its window up to `now`, ready
        for the summarizer, or None if there's nothing to say."""
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
        return DigestMaterial(articles=articles, window_desc=window_desc)

    async def summarize(self, language: str, material: DigestMaterial) -> DigestResult:
        """The digest text for `material`, from the LLM. Touches no database."""
        result = await self.summarizer.generate_digest(
            articles=material.articles,
            language=language,
            time_window_desc=material.window_desc,
        )
        # The provider returns the digest body only; the source list is
        # appended here in code so URLs are never left to the model to
        # reproduce (they get truncated/hallucinated past a dozen links).
        if result.success and result.text:
            result.text = append_source_list(result.text, material.articles, language)
        return result
