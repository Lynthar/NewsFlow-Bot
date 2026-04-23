"""Payload converters for common webhook receivers.

Each converter turns a platform-agnostic Message (or plain text notice) into a
WireRequest — the exact bytes + HTTP headers the receiver expects. Adding a
new receiver = adding one entry in each dispatch dict at the bottom; nothing
else in the codebase needs to know.

The `generic` format is the project's canonical JSON and the right default
for user-written endpoints (n8n, Zapier, custom scripts). The named formats
match the wire contracts of specific SaaS/self-hosted products so users can
point a Slack / ntfy / feishu webhook URL directly at NewsFlow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import Header

from newsflow.adapters.base import Message


@dataclass
class WireRequest:
    """A ready-to-send HTTP POST body + content-type headers."""

    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def build_payload(format_name: str, message: Message) -> WireRequest:
    """Convert a feed-entry Message into the given wire format."""
    converter = _ENTRY_CONVERTERS.get(format_name, _to_generic)
    return converter(message)


def build_notification_payload(format_name: str, text: str) -> WireRequest:
    """Convert a plain-text system notification (e.g. feed-auto-disabled)
    into the given wire format."""
    converter = _TEXT_CONVERTERS.get(format_name, _to_generic_text)
    return converter(text)


# ─── generic ─────────────────────────────────────────────────────────────────


def _to_generic(m: Message) -> WireRequest:
    payload = {
        "event": "feed.entry.new",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry": {
            "title": m.title,
            "title_translated": m.title_translated,
            "link": m.link,
            "summary": m.summary,
            "summary_translated": m.summary_translated,
            "source": m.source,
            "published_at": (
                m.published_at.isoformat() if m.published_at else None
            ),
            "image_url": m.image_url,
        },
    }
    return _json(payload)


def _to_generic_text(text: str) -> WireRequest:
    payload = {
        "event": "system.notification",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
    }
    return _json(payload)


# ─── slack ───────────────────────────────────────────────────────────────────
# incoming webhook → block kit payload.
# https://api.slack.com/messaging/webhooks


def _to_slack(m: Message) -> WireRequest:
    title = m.display_title
    summary = m.display_summary or "_No summary_"
    # Block kit section text limit is 3000; leave some headroom.
    if len(summary) > 2950:
        summary = summary[:2947] + "…"
    payload = {
        # `text` is the fallback shown in notifications / clients that don't
        # render blocks. Keep it compact.
        "text": f"{title} — {m.link}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title[:150]},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": summary},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Source: {m.source} · <{m.link}|Open>",
                    }
                ],
            },
        ],
    }
    return _json(payload)


def _to_slack_text(text: str) -> WireRequest:
    return _json({"text": text})


# ─── ntfy ────────────────────────────────────────────────────────────────────
# plain-text body + metadata headers.
# https://docs.ntfy.sh/publish/


def _to_ntfy(m: Message) -> WireRequest:
    body = (m.display_summary or m.display_title).encode("utf-8")
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        # ntfy decodes RFC-2047 for unicode titles.
        "Title": _rfc2047(m.display_title[:250]),
        "Click": m.link,
        "Tags": "newspaper,rss",
    }
    if m.image_url:
        headers["Attach"] = m.image_url
    return WireRequest(body=body, headers=headers)


def _to_ntfy_text(text: str) -> WireRequest:
    return WireRequest(
        body=text.encode("utf-8"),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Title": _rfc2047("NewsFlow"),
            "Tags": "warning,newsflow",
            "Priority": "high",
        },
    )


# ─── feishu / lark ───────────────────────────────────────────────────────────
# Group-bot webhook → post-card payload.
# https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot


def _to_lark(m: Message) -> WireRequest:
    title = m.display_title
    summary = m.display_summary or ""
    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": [
                        [{"tag": "text", "text": summary}],
                        [
                            {
                                "tag": "a",
                                "text": "Read more →",
                                "href": m.link,
                            },
                            {"tag": "text", "text": f"  ({m.source})"},
                        ],
                    ],
                }
            }
        },
    }
    return _json(payload)


def _to_lark_text(text: str) -> WireRequest:
    return _json({"msg_type": "text", "content": {"text": text}})


# ─── work-wechat (企业微信) ──────────────────────────────────────────────────
# Group-robot markdown message.
# https://developer.work.weixin.qq.com/document/path/91770


def _to_wecom(m: Message) -> WireRequest:
    title = m.display_title
    summary = m.display_summary or ""
    if len(summary) > 1500:
        summary = summary[:1497] + "…"
    md = (
        f"### {title}\n"
        f"> {summary}\n\n"
        f"[Read on {m.source}]({m.link})"
    )
    payload = {"msgtype": "markdown", "markdown": {"content": md}}
    return _json(payload)


def _to_wecom_text(text: str) -> WireRequest:
    return _json({"msgtype": "text", "text": {"content": text}})


# ─── shared helpers ──────────────────────────────────────────────────────────


def _json(payload: dict) -> WireRequest:
    return WireRequest(
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def _rfc2047(s: str) -> str:
    """Encode a header value for safe transport over HTTP. ASCII passes
    through unchanged; non-ASCII gets =?UTF-8?B?..?= which ntfy and any
    RFC-2047-aware receiver can decode back to the original string.

    Note: Header(s, 'utf-8').encode() is what actually emits the
    encoded-word form. str(Header(...)) just returns the raw unicode
    string, which aiohttp would then reject as invalid latin-1."""
    return Header(s, "utf-8").encode()


_ENTRY_CONVERTERS = {
    "generic": _to_generic,
    "slack": _to_slack,
    "ntfy": _to_ntfy,
    "lark": _to_lark,
    "wecom": _to_wecom,
}

_TEXT_CONVERTERS = {
    "generic": _to_generic_text,
    "slack": _to_slack_text,
    "ntfy": _to_ntfy_text,
    "lark": _to_lark_text,
    "wecom": _to_wecom_text,
}

SUPPORTED_FORMATS: frozenset[str] = frozenset(_ENTRY_CONVERTERS.keys())
