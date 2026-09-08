"""DigestService.run_now — the one orchestration behind every digest.

The scheduled tick and the two /digest now handlers used to carry a copy each,
which is how the mention header once fired only on scheduled runs. These pin
the parts that differed: whether an empty window consumes the schedule slot,
where the chunk budget comes from, and what each failure reports back.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.services.digest_service import DigestService

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)


class _SessionCtx:
    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.commit = AsyncMock()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


def _adapter(chunk_size: int = 1900):
    adapter = MagicMock()
    adapter.digest_chunk_size = chunk_size
    return adapter


def _dispatcher(adapter=None):
    dispatcher = MagicMock()
    dispatcher.get_adapter.return_value = adapter
    dispatcher.apply_digest_header.side_effect = lambda text, platform: f"[{platform}] {text}"
    dispatcher.deliver_digest = AsyncMock(return_value=(2, "pin-new"))
    return dispatcher


def _repo(config=...):
    if config is ...:
        config = MagicMock(id=5, last_pinned_message_id="pin-old")
    repo = MagicMock()
    repo.get = AsyncMock(return_value=config)
    repo.mark_delivered = AsyncMock()
    return repo


def _generated(text="body", success=True, error=None):
    return MagicMock(text=text, success=success, error=error)


async def _call(dispatcher, repo, generate_result, *, mark_empty_delivered, platform="discord"):
    p1 = patch.multiple(
        "newsflow.services.digest_service",
        get_session_factory=MagicMock(return_value=lambda: _SessionCtx()),
        ChannelDigestRepository=MagicMock(return_value=repo),
    )
    p2 = patch.object(DigestService, "generate", AsyncMock(return_value=generate_result))
    with p1, p2:
        return await DigestService.run_now(
            dispatcher,
            platform,
            "chan-1",
            MagicMock(),
            NOW,
            mark_empty_delivered=mark_empty_delivered,
        )


async def test_missing_adapter_reports_instead_of_generating():
    repo = _repo()
    outcome = await _call(_dispatcher(None), repo, _generated(), mark_empty_delivered=True)
    assert outcome.status == "no_adapter"
    repo.get.assert_not_called()


async def test_missing_config_reports_no_config():
    repo = _repo(config=None)
    outcome = await _call(_dispatcher(_adapter()), repo, _generated(), mark_empty_delivered=True)
    assert outcome.status == "no_config"


async def test_scheduled_run_consumes_the_slot_on_an_empty_window():
    """is_due would re-fire the same slot on every tick until the hour
    passes, so a scheduled empty run still records a delivery."""
    repo = _repo()
    outcome = await _call(_dispatcher(_adapter()), repo, None, mark_empty_delivered=True)
    assert outcome.status == "no_articles"
    repo.mark_delivered.assert_awaited_once_with(5, NOW)


async def test_manual_run_leaves_the_slot_alone_on_an_empty_window():
    repo = _repo()
    outcome = await _call(_dispatcher(_adapter()), repo, None, mark_empty_delivered=False)
    assert outcome.status == "no_articles"
    repo.mark_delivered.assert_not_awaited()


async def test_generation_failure_carries_the_error_back():
    repo = _repo()
    outcome = await _call(
        _dispatcher(_adapter()),
        repo,
        _generated(success=False, error="provider down"),
        mark_empty_delivered=True,
    )
    assert (outcome.status, outcome.error) == ("generation_failed", "provider down")
    repo.mark_delivered.assert_not_awaited()


async def test_chunk_budget_comes_from_the_adapter_not_the_call_site():
    dispatcher = _dispatcher(_adapter(chunk_size=3800))
    await _call(dispatcher, _repo(), _generated(), mark_empty_delivered=True)
    assert dispatcher.deliver_digest.call_args.kwargs["chunk_size"] == 3800
    # And the header shim ran, so a manual run pings the same way a scheduled
    # one does — the drift this orchestration was introduced to stop.
    assert dispatcher.deliver_digest.call_args.args[2] == "[discord] body"


async def test_failed_delivery_does_not_record_one():
    dispatcher = _dispatcher(_adapter())
    dispatcher.deliver_digest = AsyncMock(return_value=(0, None))
    repo = _repo()
    outcome = await _call(dispatcher, repo, _generated(), mark_empty_delivered=True)
    assert outcome.status == "delivery_failed"
    repo.mark_delivered.assert_not_awaited()


async def test_delivered_records_the_pin_and_reports_the_chunk_count():
    repo = _repo()
    outcome = await _call(_dispatcher(_adapter()), repo, _generated(), mark_empty_delivered=True)
    assert (outcome.status, outcome.chunks, outcome.mark_failed) == ("delivered", 2, False)
    repo.mark_delivered.assert_awaited_once_with(5, NOW, pinned_message_id="pin-new")


async def test_a_failed_delivery_record_is_reported_not_raised():
    """The digest is already on-platform; raising here would lose that fact."""
    repo = _repo()
    repo.mark_delivered = AsyncMock(side_effect=RuntimeError("database is locked"))
    outcome = await _call(_dispatcher(_adapter()), repo, _generated(), mark_empty_delivered=True)
    assert (outcome.status, outcome.mark_failed) == ("delivered", True)
