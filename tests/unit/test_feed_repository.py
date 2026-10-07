"""Tests for FeedRepository.create_entries_bulk — dedup + bulk insert."""

from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select

from newsflow.models.feed import FeedEntry
from newsflow.models.subscription import SentEntry, Subscription
from newsflow.repositories.feed_repository import FeedRepository

_LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"


async def test_create_entries_bulk_inserts_all_new(session):
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")

    data = [
        {"guid": "a", "title": "A", "link": "https://x/a"},
        {"guid": "b", "title": "B", "link": "https://x/b"},
    ]
    created = await repo.create_entries_bulk(feed.id, data)

    assert {e.guid for e in created} == {"a", "b"}


async def test_create_entries_bulk_skips_existing_guids(session):
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")

    await repo.create_entries_bulk(
        feed.id,
        [
            {"guid": "a", "title": "A", "link": "https://x/a"},
            {"guid": "b", "title": "B", "link": "https://x/b"},
        ],
    )
    created = await repo.create_entries_bulk(
        feed.id,
        [
            {"guid": "b", "title": "B2", "link": "https://x/b"},  # duplicate
            {"guid": "c", "title": "C", "link": "https://x/c"},  # new
        ],
    )

    assert [e.guid for e in created] == ["c"]


async def test_create_entries_bulk_empty_input(session):
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")

    result = await repo.create_entries_bulk(feed.id, [])

    assert result == []


async def test_create_entries_bulk_dedup_is_per_feed(session):
    """Same guid under a different feed is a different entry."""
    repo = FeedRepository(session)
    feed_a = await repo.create_feed(url="https://example.com/a")
    feed_b = await repo.create_feed(url="https://example.com/b")

    await repo.create_entries_bulk(
        feed_a.id, [{"guid": "shared", "title": "A", "link": "https://x/a"}]
    )
    created = await repo.create_entries_bulk(
        feed_b.id, [{"guid": "shared", "title": "B", "link": "https://x/b"}]
    )

    assert len(created) == 1
    assert created[0].feed_id == feed_b.id


async def test_create_entries_bulk_dedups_within_batch(session):
    """Two entries sharing a guid in ONE fetch must not both be inserted.

    Without in-batch dedup the second row violates the (feed_id, guid)
    unique index on flush, raising IntegrityError that poisons the whole
    dispatch session. The first occurrence wins; the duplicate is dropped.
    """
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")

    created = await repo.create_entries_bulk(
        feed.id,
        [
            {"guid": "dup", "title": "first", "link": "https://x/1"},
            {"guid": "dup", "title": "second", "link": "https://x/2"},
            {"guid": "c", "title": "C", "link": "https://x/c"},
        ],
    )

    assert [e.guid for e in created] == ["dup", "c"]
    assert next(e for e in created if e.guid == "dup").title == "first"

    # The flush succeeded and the row is persisted (no IntegrityError).
    again = await repo.create_entries_bulk(
        feed.id, [{"guid": "dup", "title": "third", "link": "https://x/3"}]
    )
    assert again == []


async def test_create_entries_bulk_degenerate_fallback_guids(session):
    """Entries with identical degenerate fallback guids (e.g. "-") don't crash."""
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")

    created = await repo.create_entries_bulk(
        feed.id,
        [
            {"guid": "-", "title": "Untitled", "link": "https://example.com/feed"},
            {"guid": "-", "title": "Untitled", "link": "https://example.com/feed"},
        ],
    )

    assert len(created) == 1


async def test_create_entries_bulk_clamps_far_future_dates_and_keeps_near_ones(session):
    """More than a day ahead is a broken clock or a hostile feed and is clamped
    to now; a timezone-skewed near-future date is stored as published."""
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")
    now = datetime.now(UTC)
    near = now + timedelta(hours=12)

    created = await repo.create_entries_bulk(
        feed.id,
        [
            {
                "guid": "far",
                "title": "F",
                "link": "https://x/f",
                "published_at": now + timedelta(hours=36),
            },
            {"guid": "near", "title": "N", "link": "https://x/n", "published_at": near},
        ],
    )

    by_guid = {e.guid: e.published_at for e in created}
    assert by_guid["far"] is not None and by_guid["far"] <= datetime.now(UTC)
    assert by_guid["near"] == near


async def test_create_entries_bulk_stores_an_offset_date_as_the_same_instant(session):
    """SQLite keeps the wall-clock time and drops the offset, so a +08:00 date read
    back as UTC would land eight hours late."""
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")
    published = datetime(2026, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    await repo.create_entries_bulk(
        feed.id, [{"guid": "g", "title": "T", "link": "https://x/g", "published_at": published}]
    )
    session.expire_all()

    stored = (await session.execute(select(FeedEntry.published_at))).scalar_one()
    assert stored.replace(tzinfo=stored.tzinfo or UTC) == published


async def test_update_feed_metadata_stores_both_validators(session):
    """ETag and Last-Modified are what the next fetch sends back as
    If-None-Match / If-Modified-Since; dropping either forfeits the 304 path."""
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")

    await repo.update_feed_metadata(feed.id, etag='"v1"', last_modified=_LAST_MODIFIED)

    await session.refresh(feed)
    assert feed.etag == '"v1"'
    assert feed.last_modified == _LAST_MODIFIED


async def test_create_feed_caps_metadata_like_update_path(session):
    """A 513-char remote title used to pass create_feed uncapped and fail
    the very first INSERT on Postgres (update_feed_metadata already
    truncated) — both paths must cap to the column width."""
    repo = FeedRepository(session)
    feed = await repo.create_feed(
        url="https://example.com/longmeta",
        title="T" * 600,
        site_url="https://example.com/" + "p" * 3000,
    )
    assert feed.title is not None and len(feed.title) == 512
    assert feed.site_url is not None and len(feed.site_url) == 2048


async def test_update_entry_translation_caps_title(session):
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed-tr")
    (entry,) = await repo.create_entries_bulk(
        feed.id, [{"guid": "g", "title": "T", "link": "https://x/g"}]
    )
    await repo.update_entry_translation(entry.id, "译" * 2000, "summary", "zh-CN")
    await session.flush()
    refreshed = await repo.get_entry_by_guid(feed.id, "g")
    assert refreshed is not None
    assert refreshed.title_translated is not None
    assert len(refreshed.title_translated) == 1024


async def test_create_entries_bulk_stores_text_no_backend_rejects(session):
    # A lone surrogate has no UTF-8 encoding and fails the INSERT; NUL fails it on
    # Postgres. A pair split into two code points (surrogatepass decoding) rejoins.
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")

    [entry] = await repo.create_entries_bulk(
        feed.id,
        [
            {
                "guid": "g\ud83d",
                "title": "cut \ud83d emoji",
                "link": "https://x/a",
                "summary": "Breaking\x00 news",
                "author": "\ud83d\ude00",
            }
        ],
    )
    await session.commit()

    assert (entry.guid, entry.title) == ("g\ufffd", "cut \ufffd emoji")
    assert (entry.summary, entry.author) == ("Breaking news", "\U0001f600")


async def test_create_entries_bulk_matches_a_mended_guid_on_the_next_fetch(session):
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")
    item = {"guid": "g\ud83d", "title": "T", "link": "https://x/a"}

    assert len(await repo.create_entries_bulk(feed.id, [item])) == 1
    assert await repo.create_entries_bulk(feed.id, [item]) == []


async def test_cleanup_keeps_an_old_entry_processed_within_the_digest_window(session):
    """An entry stored 11 days ago but processed 3 days ago (its subscription was
    paused, then resumed) is still the next weekly digest's material; entries whose
    processing is older, that were never processed, or that were only seeded at
    subscribe time, go on the usual schedule."""
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")
    sub = Subscription(
        platform="telegram", platform_user_id="u", platform_channel_id="c", feed_id=feed.id
    )
    session.add(sub)
    now = datetime.now(UTC)
    stored = now - timedelta(days=11)
    for guid in ("late", "stale", "unsent", "seeded"):
        session.add(
            FeedEntry(feed_id=feed.id, guid=guid, title=guid, link="https://x/", created_at=stored)
        )
    await session.flush()
    for guid, sent_ago, seeded in (("late", 3, False), ("stale", 8, False), ("seeded", 3, True)):
        session.add(
            SentEntry(
                subscription_id=sub.id,
                feed_id=feed.id,
                guid=guid,
                sent_at=now - timedelta(days=sent_ago),
                seeded=seeded,
            )
        )
    await session.commit()

    deleted = await repo.cleanup_old_entries(10)
    await session.commit()

    assert deleted == 3
    assert list(await session.scalars(select(FeedEntry.guid))) == ["late"]


async def test_long_guids_sharing_a_prefix_stay_distinct(session):
    """Cutting a guid to the column width used to merge two that differ only past it,
    and the second article was dropped as a duplicate on every fetch."""
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")
    prefix = "g" * 2048

    def item(suffix: str) -> dict:
        return {"guid": prefix + suffix, "title": suffix, "link": "https://x/" + suffix}

    same_batch = await repo.create_entries_bulk(feed.id, [item("A"), item("B")])
    next_fetch = await repo.create_entries_bulk(feed.id, [item("C")])
    refetch = await repo.create_entries_bulk(feed.id, [item("A"), item("B"), item("C")])

    assert [e.title for e in same_batch] == ["A", "B"]
    assert [e.title for e in next_fetch] == ["C"]
    assert refetch == []


async def test_guid_keys_fit_the_column_and_the_index(session):
    # 1,000 CJK characters are 3,000 UTF-8 bytes, past Postgres's btree entry limit.
    repo = FeedRepository(session)
    feed = await repo.create_feed(url="https://example.com/feed")
    short = "https://example.com/a?id=1"

    created = await repo.create_entries_bulk(
        feed.id,
        [
            {"guid": "文" * 1000, "title": "cjk", "link": "https://x/1"},
            {"guid": short, "title": "short", "link": "https://x/2"},
        ],
    )

    assert len(created[0].guid.encode()) <= 2048
    assert created[1].guid == short  # a guid that fits is stored unchanged
