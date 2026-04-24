"""Tests for Dispatcher.deliver_digest — the pin-aware digest delivery
helper.

Covered scenarios (matrix of chunk count × pin outcome × prior pin):
- single chunk, pin ok, no prior pin       → (1, new_id), no unpin
- single chunk, pin ok, prior pin exists   → (1, new_id), unpin called with old
- single chunk, pin fails (True, None)     → (1, None),   NO unpin (prior preserved)
- single chunk, send fails (False, None)   → (0, None),   no unpin
- multi-chunk, pin first only              → (N, new_id), only first chunk pinned
- empty text                               → (0, None)
- adapter lacks pin support (default impl) → (N, None)    via base fallback
"""

from unittest.mock import AsyncMock, MagicMock, patch

from newsflow.adapters.base import BaseAdapter, Message
from newsflow.services.dispatcher import Dispatcher


def _make_dispatcher() -> Dispatcher:
    fake = MagicMock()
    fake.discord_enabled = False
    fake.telegram_enabled = False
    fake.webhooks_enabled = False
    fake.fetch_interval_minutes = 60
    with patch("newsflow.services.dispatcher.get_settings", return_value=fake):
        return Dispatcher()


def _mock_adapter(
    *,
    pin_result: tuple[bool, str | None] = (True, "msg-new"),
    send_text_result: bool = True,
    unpin_result: bool = True,
):
    """Build a MagicMock adapter satisfying the MessageSender protocol."""
    adapter = MagicMock()
    adapter.send_text = AsyncMock(return_value=send_text_result)
    adapter.send_text_pinned = AsyncMock(return_value=pin_result)
    adapter.unpin_message = AsyncMock(return_value=unpin_result)
    adapter.is_connected = MagicMock(return_value=True)
    return adapter


# ===== single chunk paths =====


async def test_single_chunk_pin_succeeds_no_prior():
    d = _make_dispatcher()
    adapter = _mock_adapter(pin_result=(True, "msg-1"))

    sent, pin_id = await d.deliver_digest(
        adapter, "chan", "short body", chunk_size=1900, prior_pin_id=None
    )

    assert sent == 1
    assert pin_id == "msg-1"
    adapter.send_text_pinned.assert_awaited_once_with("chan", "short body")
    adapter.send_text.assert_not_awaited()
    adapter.unpin_message.assert_not_awaited()


async def test_single_chunk_pin_succeeds_with_prior_triggers_unpin():
    d = _make_dispatcher()
    adapter = _mock_adapter(pin_result=(True, "msg-new"))

    sent, pin_id = await d.deliver_digest(
        adapter, "chan", "body", chunk_size=1900, prior_pin_id="msg-old"
    )

    assert sent == 1
    assert pin_id == "msg-new"
    adapter.unpin_message.assert_awaited_once_with("chan", "msg-old")


async def test_pin_fails_prior_is_preserved():
    """Send succeeded but pin failed (common: missing Manage Messages
    perm). We must NOT unpin the prior — it stays as the channel's
    last known digest pin until the next successful pin replaces it."""
    d = _make_dispatcher()
    adapter = _mock_adapter(pin_result=(True, None))

    sent, pin_id = await d.deliver_digest(
        adapter, "chan", "body", chunk_size=1900, prior_pin_id="msg-old"
    )

    assert sent == 1
    assert pin_id is None
    adapter.unpin_message.assert_not_awaited()


async def test_send_fails_returns_zero():
    d = _make_dispatcher()
    adapter = _mock_adapter(pin_result=(False, None))

    sent, pin_id = await d.deliver_digest(
        adapter, "chan", "body", chunk_size=1900, prior_pin_id="msg-old"
    )

    assert sent == 0
    assert pin_id is None
    adapter.send_text.assert_not_awaited()
    adapter.unpin_message.assert_not_awaited()


# ===== multi-chunk =====


async def test_multi_chunk_only_first_pinned():
    d = _make_dispatcher()
    adapter = _mock_adapter(pin_result=(True, "msg-first"))

    # Force chunking: text much longer than chunk_size, with clear
    # paragraph boundaries so the splitter produces multiple chunks.
    paragraphs = ["P" + str(i) * 40 for i in range(6)]
    text = "\n\n".join(paragraphs)

    sent, pin_id = await d.deliver_digest(
        adapter, "chan", text, chunk_size=80, prior_pin_id=None
    )

    assert sent >= 2, "multi-chunk test requires at least 2 chunks"
    assert pin_id == "msg-first"
    # First chunk pinned; remainder sent as plain text.
    adapter.send_text_pinned.assert_awaited_once()
    assert adapter.send_text.await_count == sent - 1


async def test_multi_chunk_partial_tail_failure_still_counts_pin():
    """A tail-chunk send failure doesn't invalidate the successful pin
    on the first chunk. chunks_sent reflects the true count."""
    d = _make_dispatcher()
    adapter = _mock_adapter(pin_result=(True, "msg-first"))
    # One tail chunk fails, the rest succeed. Use a function-style
    # side_effect to avoid StopIteration when chunk count grows.
    call_num = {"n": 0}

    async def tail_send(_channel_id, _text):
        call_num["n"] += 1
        # Fail the 2nd tail send; succeed otherwise.
        return call_num["n"] != 2

    adapter.send_text = AsyncMock(side_effect=tail_send)

    paragraphs = ["P" + str(i) * 40 for i in range(6)]
    text = "\n\n".join(paragraphs)

    sent, pin_id = await d.deliver_digest(
        adapter, "chan", text, chunk_size=80, prior_pin_id=None
    )

    # 1 pinned (first chunk) + (tail_total - 1) successful tail sends.
    assert sent >= 2
    assert sent == 1 + adapter.send_text.await_count - 1  # pinned + successes
    assert pin_id == "msg-first"


# ===== empty input =====


async def test_empty_text_is_noop():
    d = _make_dispatcher()
    adapter = _mock_adapter()

    sent, pin_id = await d.deliver_digest(
        adapter, "chan", "", chunk_size=1900, prior_pin_id=None
    )

    assert sent == 0
    assert pin_id is None
    adapter.send_text_pinned.assert_not_awaited()
    adapter.send_text.assert_not_awaited()


# ===== adapter default (no pin support) falls back gracefully =====


class _PinlessAdapter(BaseAdapter):
    """Only implements send_text — pin methods inherit BaseAdapter
    defaults so we verify the fallback path keeps digests working on
    any adapter that hasn't opted into pinning (e.g. webhook)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    @property
    def platform_name(self) -> str:  # pragma: no cover
        return "test"

    async def start(self) -> None:  # pragma: no cover
        pass

    async def stop(self) -> None:  # pragma: no cover
        pass

    async def send_message(
        self, channel_id: str, message: Message
    ) -> bool:  # pragma: no cover
        return True

    async def send_text(self, channel_id: str, text: str) -> bool:
        self.sent.append((channel_id, text))
        return True


async def test_default_pin_fallback_delivers_without_pin():
    d = _make_dispatcher()
    adapter = _PinlessAdapter()

    sent, pin_id = await d.deliver_digest(
        adapter, "chan", "body", chunk_size=1900, prior_pin_id="msg-old"
    )

    assert sent == 1
    # Fallback returns (sent, None); caller preserves prior pin.
    assert pin_id is None
    assert adapter.sent == [("chan", "body")]


async def test_unpin_noops_on_same_id():
    """Belt-and-suspenders: if new pin id somehow equals prior pin id
    (shouldn't happen on Discord/Telegram, but defensive), we skip the
    unpin to avoid unpinning the just-pinned digest."""
    d = _make_dispatcher()
    adapter = _mock_adapter(pin_result=(True, "same-id"))

    sent, pin_id = await d.deliver_digest(
        adapter, "chan", "body", chunk_size=1900, prior_pin_id="same-id"
    )

    assert sent == 1
    assert pin_id == "same-id"
    adapter.unpin_message.assert_not_awaited()


async def test_unpin_exception_is_swallowed():
    """Prior-unpin is pure best-effort; an exception must not break
    the caller's commit/delivery path."""
    d = _make_dispatcher()
    adapter = _mock_adapter(pin_result=(True, "msg-new"))
    adapter.unpin_message = AsyncMock(side_effect=RuntimeError("network"))

    sent, pin_id = await d.deliver_digest(
        adapter, "chan", "body", chunk_size=1900, prior_pin_id="msg-old"
    )

    assert sent == 1
    assert pin_id == "msg-new"


# ===== _chunk_text oversize-paragraph fallback =====


def test_chunk_text_splits_oversized_paragraph_on_newlines():
    """A single paragraph (no \\n\\n) longer than chunk_size must be
    broken further on \\n boundaries — previously it was emitted whole
    and could trip Discord's 4000-char content limit."""
    lines = ["line " + str(i) + " " + "x" * 40 for i in range(20)]
    text = "\n".join(lines)  # no \n\n — one paragraph
    assert len(text) > 200

    chunks = Dispatcher._chunk_text(text, 200)

    assert len(chunks) >= 2
    assert all(len(c) <= 200 for c in chunks), (
        f"oversize chunk slipped through: {[len(c) for c in chunks]}"
    )


def test_chunk_text_hard_slices_single_long_line():
    """One line with no \\n and no \\n\\n that exceeds chunk_size must
    still be capped by a hard character-count slice — the absolute
    last-resort path."""
    text = "A" * 5000  # no newlines at all

    chunks = Dispatcher._chunk_text(text, 1900)

    assert all(len(c) <= 1900 for c in chunks)
    assert "".join(chunks) == text  # no data loss


def test_chunk_text_preserves_paragraph_splitting_when_fits():
    """Regression guard: normal paragraph-boundary splitting must
    still work (no regression from the hard-split fallback)."""
    paragraphs = ["P" + str(i) * 40 for i in range(6)]
    text = "\n\n".join(paragraphs)

    chunks = Dispatcher._chunk_text(text, 80)

    assert len(chunks) >= 2
    assert all(len(c) <= 80 for c in chunks)
