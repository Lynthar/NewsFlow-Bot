"""Tests for the webhook payload converters.

These are pure data-transform tests — no I/O, no DB. They guard the wire
contract with Slack / ntfy / Feishu / WeCom so a refactor doesn't silently
ship malformed payloads to third parties.
"""

import json
from datetime import datetime, timezone

from newsflow.adapters.base import Message
from newsflow.adapters.webhook.formats import (
    SUPPORTED_FORMATS,
    build_notification_payload,
    build_payload,
)


def _make_message(**overrides) -> Message:
    defaults = dict(
        title="Hello World",
        summary="A concise summary of what happened.",
        link="https://example.com/article?ref=rss&id=42",
        source="Example News",
        published_at=datetime(2026, 4, 23, 6, 0, tzinfo=timezone.utc),
        image_url="https://example.com/cover.jpg",
        title_translated=None,
        summary_translated=None,
    )
    defaults.update(overrides)
    return Message(**defaults)


# ─── generic ─────────────────────────────────────────────────────────────────


def test_generic_entry_payload_has_required_shape():
    wire = build_payload("generic", _make_message())
    payload = json.loads(wire.body.decode("utf-8"))

    assert payload["event"] == "feed.entry.new"
    assert "timestamp" in payload
    entry = payload["entry"]
    assert entry["title"] == "Hello World"
    assert entry["link"] == "https://example.com/article?ref=rss&id=42"
    assert entry["source"] == "Example News"
    assert entry["published_at"] == "2026-04-23T06:00:00+00:00"
    assert entry["image_url"] == "https://example.com/cover.jpg"
    assert wire.headers["Content-Type"].startswith("application/json")


def test_generic_passes_translations_through():
    msg = _make_message(
        title_translated="你好世界", summary_translated="简短摘要"
    )
    wire = build_payload("generic", msg)
    entry = json.loads(wire.body)["entry"]
    assert entry["title_translated"] == "你好世界"
    assert entry["summary_translated"] == "简短摘要"


def test_generic_utf8_not_escaped():
    """ensure_ascii=False keeps CJK readable in the wire payload."""
    wire = build_payload("generic", _make_message(title="你好"))
    assert "你好".encode("utf-8") in wire.body


def test_unknown_format_falls_back_to_generic():
    wire = build_payload("unknown-format-xyz", _make_message())
    assert json.loads(wire.body)["event"] == "feed.entry.new"


def test_generic_notification_payload():
    wire = build_notification_payload("generic", "the feed was auto-disabled")
    payload = json.loads(wire.body)
    assert payload["event"] == "system.notification"
    assert payload["text"] == "the feed was auto-disabled"


# ─── slack ───────────────────────────────────────────────────────────────────


def test_slack_produces_block_kit():
    wire = build_payload("slack", _make_message())
    payload = json.loads(wire.body)
    assert "blocks" in payload
    assert payload["blocks"][0]["type"] == "header"
    assert payload["blocks"][0]["text"]["text"] == "Hello World"
    # fallback text for notification clients that don't render blocks
    assert "text" in payload


def test_slack_header_truncated_to_150_chars():
    long_title = "x" * 500
    wire = build_payload("slack", _make_message(title=long_title))
    payload = json.loads(wire.body)
    header_text = payload["blocks"][0]["text"]["text"]
    assert len(header_text) <= 150


def test_slack_empty_summary_has_placeholder():
    """Block-kit section text can't be empty; placeholder avoids API 400."""
    wire = build_payload(
        "slack", _make_message(summary="", summary_translated=None)
    )
    payload = json.loads(wire.body)
    section_text = payload["blocks"][1]["text"]["text"]
    assert section_text  # non-empty


# ─── ntfy ────────────────────────────────────────────────────────────────────


def test_ntfy_body_is_plaintext_summary():
    wire = build_payload("ntfy", _make_message())
    assert wire.body == b"A concise summary of what happened."
    assert wire.headers["Content-Type"].startswith("text/plain")


def test_ntfy_sets_click_and_tags_headers():
    wire = build_payload("ntfy", _make_message())
    assert wire.headers["Click"] == "https://example.com/article?ref=rss&id=42"
    assert "rss" in wire.headers["Tags"]


def test_ntfy_title_rfc2047_for_non_ascii():
    """ntfy.sh decodes =?UTF-8?B?…?= back to the original string."""
    wire = build_payload("ntfy", _make_message(title="你好世界"))
    # RFC-2047 encoded-word shape. Exact bytes are base64 but always have
    # the =?UTF-8?…?= envelope.
    assert wire.headers["Title"].startswith("=?utf-8?")


def test_ntfy_attach_set_when_image_present():
    wire = build_payload("ntfy", _make_message())
    assert wire.headers["Attach"] == "https://example.com/cover.jpg"


def test_ntfy_notification_sets_high_priority():
    wire = build_notification_payload("ntfy", "feed disabled")
    assert wire.headers.get("Priority") == "high"


# ─── lark / feishu ───────────────────────────────────────────────────────────


def test_lark_builds_post_card():
    wire = build_payload("lark", _make_message())
    payload = json.loads(wire.body)
    assert payload["msg_type"] == "post"
    post = payload["content"]["post"]["zh_cn"]
    assert post["title"] == "Hello World"
    # Second line contains the "Read more" link
    link_line = post["content"][1]
    hrefs = [el.get("href") for el in link_line if el.get("tag") == "a"]
    assert hrefs == ["https://example.com/article?ref=rss&id=42"]


def test_lark_text_notification_is_simple():
    wire = build_notification_payload("lark", "disabled")
    payload = json.loads(wire.body)
    assert payload == {"msg_type": "text", "content": {"text": "disabled"}}


# ─── wecom ───────────────────────────────────────────────────────────────────


def test_wecom_builds_markdown():
    wire = build_payload("wecom", _make_message())
    payload = json.loads(wire.body)
    assert payload["msgtype"] == "markdown"
    content = payload["markdown"]["content"]
    assert "Hello World" in content
    assert "Example News" in content
    assert "https://example.com/article?ref=rss&id=42" in content


def test_wecom_summary_truncated():
    long_summary = "x" * 5000
    wire = build_payload("wecom", _make_message(summary=long_summary))
    content = json.loads(wire.body)["markdown"]["content"]
    # 1500 chars cap + header + footer + some overhead
    assert len(content) < 2000


# ─── meta ────────────────────────────────────────────────────────────────────


def test_all_named_formats_produce_valid_bytes():
    """Smoke: every declared format returns non-empty body + content type."""
    msg = _make_message()
    for fmt in SUPPORTED_FORMATS:
        wire = build_payload(fmt, msg)
        assert wire.body, f"{fmt}: empty body"
        assert wire.headers, f"{fmt}: empty headers"
        text_wire = build_notification_payload(fmt, "note")
        assert text_wire.body, f"{fmt} text: empty body"
