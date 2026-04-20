"""Tests for FeedRepository.create_entries_bulk — dedup + bulk insert."""

from newsflow.repositories.feed_repository import FeedRepository


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
