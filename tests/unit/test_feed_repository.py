"""Tests for FeedRepository.create_entries_bulk — dedup + bulk insert."""

from datetime import UTC, datetime, timedelta

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
