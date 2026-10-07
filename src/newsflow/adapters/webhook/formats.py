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
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import Header
from html import escape as html_escape
from typing import Any

from newsflow.adapters.base import Message, is_http_url


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


@dataclass(frozen=True)
class Refusal:
    """A receiver's verdict, carried in a 2xx response body, that it dropped the message."""

    error: str
    rate_limited: bool


def reads_verdict_from_body(format_name: str) -> bool:
    """Whether a 2xx from this format's receiver still needs its body checked."""
    return format_name in _BODY_VERDICTS


def body_refusal(format_name: str, body: bytes) -> Refusal | None:
    """The refusal a 2xx body reports, or None when the message was accepted.

    A body that is not the receiver's JSON envelope counts as accepted: nothing in
    it says otherwise, which is all a 2xx without this contract would tell us.
    """
    spec = _BODY_VERDICTS.get(format_name)
    if spec is None:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    code = next((data[k] for k in spec.code_keys if k in data), 0)
    if not code:
        return None
    reason = next((str(data[k]) for k in spec.message_keys if data.get(k)), "")
    return Refusal(
        error=f"{format_name} code {code}: {reason}"[:300],
        rate_limited=code in spec.rate_limit_codes,
    )


# ─── generic ─────────────────────────────────────────────────────────────────


def _to_generic(m: Message) -> WireRequest:
    payload = {
        "event": "feed.entry.new",
        "timestamp": datetime.now(UTC).isoformat(),
        "entry": {
            "title": m.title,
            "title_translated": m.title_translated,
            "link": m.link,
            "summary": m.summary,
            "summary_translated": m.summary_translated,
            "source": m.source,
            "published_at": (m.published_at.isoformat() if m.published_at else None),
            "image_url": m.image_url,
        },
    }
    return _json(payload)


def _to_generic_text(text: str) -> WireRequest:
    payload = {
        "event": "system.notification",
        "timestamp": datetime.now(UTC).isoformat(),
        "text": text,
    }
    return _json(payload)


# ─── slack ───────────────────────────────────────────────────────────────────
# incoming webhook → block kit payload.
# https://api.slack.com/messaging/webhooks


def _to_slack(m: Message) -> WireRequest:
    title = m.display_title
    # Block kit section text limit is 3000; leave some headroom.
    summary = _slack_mrkdwn(m.display_summary, 2950) if m.display_summary else "_No summary_"
    # A link that isn't http(s) is left out: `<!channel|Open>` would be a mention.
    url = _slack_url(m.link) if is_http_url(m.link) else None
    fallback = _slack_mrkdwn(title, 2950)
    context = f"Source: {_slack_mrkdwn(m.source, 200)}"
    payload = {
        # `text` is the fallback shown in notifications / clients that don't
        # render blocks. Keep it compact.
        "text": f"{fallback} — <{url}>" if url else fallback,
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
                        "text": f"{context} · <{url}|Open>" if url else context,
                    }
                ],
            },
        ],
    }
    return _json(payload)


def _to_slack_text(text: str) -> WireRequest:
    return _json({"text": _slack_mrkdwn(text, 3000)})


def _slack_mrkdwn(text: str, limit: int) -> str:
    """Feed text as inert mrkdwn: Slack reads ``<!channel>`` and ``<@U…>`` as mentions,
    and ``&``, ``<``, ``>`` written as entities display as themselves."""
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(out) > limit:
        # Never end on half an entity.
        out = re.sub(r"&[a-z]*$", "", out[: limit - 1]) + "…"
    return out


def _slack_url(url: str) -> str:
    """`url` for the ``<url|label>`` syntax, which ``<``, ``>`` and ``|`` would end early.
    None of the three is legal unencoded in a URL, so encoding them keeps its meaning."""
    return url.replace("<", "%3C").replace(">", "%3E").replace("|", "%7C")


# ─── ntfy ────────────────────────────────────────────────────────────────────
# plain-text body + metadata headers.
# https://docs.ntfy.sh/publish/


def _to_ntfy(m: Message) -> WireRequest:
    body = (m.display_summary or m.display_title).encode("utf-8")
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        # ntfy decodes RFC-2047 for unicode titles.
        "Title": _rfc2047(m.display_title[:250]),
        "Tags": "newspaper,rss",
    }
    # Click/Attach become HTTP header values built from untrusted feed data. A CR/LF
    # or non-latin-1 byte makes aiohttp raise ValueError, which the ClientError
    # handler misses — set them only for clean http(s) URLs.
    click = _safe_header_url(m.link)
    if click:
        headers["Click"] = click
    attach = _safe_header_url(m.image_url)
    if attach:
        headers["Attach"] = attach
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
    rows: list[list[dict[str, str]]] = []
    if m.display_summary:
        rows.append([{"tag": "text", "text": m.display_summary}])
    rows.append(
        [
            {"tag": "a", "text": "Read more →", "href": m.link},
            {"tag": "text", "text": f"  ({m.source})"},
        ]
    )
    payload = {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {"title": m.display_title, "content": rows}}},
    }
    return _json(payload)


def _to_lark_text(text: str) -> WireRequest:
    # A post, like entries: `msg_type: text` reads <at user_id="all"> in the text itself,
    # while a post only mentions through an explicit `at` element.
    post = {"title": "NewsFlow", "content": [[{"tag": "text", "text": text}]]}
    return _json({"msg_type": "post", "content": {"post": {"zh_cn": post}}})


# ─── work-wechat (企业微信) ──────────────────────────────────────────────────
# Group-robot markdown message.
# https://developer.work.weixin.qq.com/document/path/91770


# Byte caps on `content`; over them the robot answers errcode 40058 and drops the message.
_WECOM_MARKDOWN_MAX_BYTES = 4096
_WECOM_TEXT_MAX_BYTES = 2048


def _to_wecom(m: Message) -> WireRequest:
    title = _clip_utf8(_wecom_inert(m.display_title), 1024)
    tail = f"\n\n[Read on {_wecom_inert(m.source)}]({_wecom_inert(m.link)})"
    head = f"### {title}\n> "
    room = _WECOM_MARKDOWN_MAX_BYTES - len(head.encode()) - len(tail.encode())
    summary = _clip_utf8(_wecom_inert(m.display_summary or ""), room)
    md = _clip_utf8(head + summary + tail, _WECOM_MARKDOWN_MAX_BYTES)
    payload = {"msgtype": "markdown", "markdown": {"content": md}}
    return _json(payload)


def _to_wecom_text(text: str) -> WireRequest:
    content = _clip_utf8(_wecom_inert(text), _WECOM_TEXT_MAX_BYTES)
    return _json({"msgtype": "text", "text": {"content": content}})


def _wecom_inert(text: str) -> str:
    """Both text and markdown content read ``<@userid>`` as a mention; a zero-width
    space after the ``<`` leaves it as visible text."""
    return text.replace("<@", "<\u200b@")


# ─── discord ─────────────────────────────────────────────────────────────────
# Channel webhook → embed payload. No bot token, no gateway connection.
# https://discord.com/developers/docs/resources/webhook#execute-webhook

# discord.Color.blue(), so a webhook destination looks like the bot adapter's own posts.
_DISCORD_BLUE = 0x3498DB


def _to_discord(m: Message) -> WireRequest:
    ts = m.published_at or datetime.now(UTC)
    if ts.tzinfo is None:
        # SQLite returns naive datetimes even for tz-aware columns, and Discord
        # rejects a timestamp carrying no offset.
        ts = ts.replace(tzinfo=UTC)
    embed: dict[str, Any] = {
        # The title field is not markdown-parsed, so a hostile "](" in feed text
        # cannot forge a link. Never move the title into `description`.
        "title": m.display_title[:256] or "(untitled)",
        "color": _DISCORD_BLUE,
        "timestamp": ts.isoformat(),
        "footer": {"text": f"Source: {m.source}"[:2048]},
    }
    if is_http_url(m.link):
        embed["url"] = m.link
    summary = m.display_summary
    if summary:
        embed["fields"] = [{"name": "Summary", "value": summary[:1024], "inline": False}]
    if is_http_url(m.image_url):
        embed["image"] = {"url": m.image_url}
    # Feed text rides entirely inside the embed, where mentions never notify. The
    # explicit empty parse list holds that line if `content` ever carries text.
    return _json({"embeds": [embed], "allowed_mentions": {"parse": []}})


def _to_discord_text(text: str) -> WireRequest:
    return _json({"content": text[:2000], "allowed_mentions": {"parse": []}})


# ─── matrix ──────────────────────────────────────────────────────────────────
# matrix-hookshot generic webhook: `text`, plus `html` for the formatted body.
# https://matrix-org.github.io/matrix-hookshot/latest/setup/webhooks.html


def _to_matrix(m: Message) -> WireRequest:
    title = m.display_title or "(untitled)"
    summary = m.display_summary

    lines = [title]
    if summary:
        lines.append(summary)
    if m.link:
        lines.append(m.link)
    lines.append(f"— {m.source}")

    # Feed text reaches Matrix clients as HTML, so every interpolated value is
    # escaped here instead of trusting the client's tag whitelist.
    head = html_escape(title)
    if is_http_url(m.link):
        head = f'<a href="{html_escape(m.link)}">{head}</a>'
    parts = [f"<b>{head}</b>"]
    if summary:
        parts.append(f"<br/>{html_escape(summary)}")
    parts.append(f"<br/><i>{html_escape(m.source)}</i>")

    # `html` alone is ignored — hookshot needs `text` as the fallback body.
    return _json({"text": "\n".join(lines), "html": "".join(parts)})


def _to_matrix_text(text: str) -> WireRequest:
    return _json({"text": text})


# ─── shared helpers ──────────────────────────────────────────────────────────


def _clip_utf8(text: str, max_bytes: int) -> str:
    """`text` cut to at most `max_bytes` of UTF-8, with "…" marking a cut."""
    raw = text.encode()
    if len(raw) <= max_bytes:
        return text
    if max_bytes < 3:
        return ""
    return raw[: max_bytes - 3].decode(errors="ignore") + "…"


def _json(payload: dict[str, Any]) -> WireRequest:
    return WireRequest(
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def _rfc2047(s: str) -> str:
    """Encode a header value as a single safe line for HTTP transport.

    Collapse every whitespace run (incl. CR/LF/tab) to a single space, then
    RFC-2047 encode with folding suppressed (high ``maxlinelen``). Two reasons:

    - ``Header(s).encode()`` defaults to ``maxlinelen=76`` and FOLDS long or
      non-ASCII values by inserting a newline. aiohttp rejects any header value
      containing a control char, so a folded ``Title`` raised ValueError and the
      ntfy send failed permanently — hitting every CJK title (base64 grows past
      76) and any title over ~40 chars.
    - A raw newline in the source title would otherwise inject a header line;
      collapsing whitespace closes that too.

    ntfy decodes the resulting ``=?UTF-8?B?..?=`` / ``=?UTF-8?Q?..?=`` word back
    to the original string."""
    collapsed = " ".join(s.split())
    return Header(collapsed, "utf-8").encode(maxlinelen=998)


def _safe_header_url(value: str | None) -> str | None:
    """`value` if it may also ride in an HTTP header, else None: aiohttp refuses a
    non-latin-1 header value with ValueError, failing the send."""
    return value if is_http_url(value) and value.isascii() else None


_ENTRY_CONVERTERS = {
    "generic": _to_generic,
    "slack": _to_slack,
    "ntfy": _to_ntfy,
    "lark": _to_lark,
    "wecom": _to_wecom,
    "discord": _to_discord,
    "matrix": _to_matrix,
}

_TEXT_CONVERTERS = {
    "generic": _to_generic_text,
    "slack": _to_slack_text,
    "ntfy": _to_ntfy_text,
    "lark": _to_lark_text,
    "wecom": _to_wecom_text,
    "discord": _to_discord_text,
    "matrix": _to_matrix_text,
}

SUPPORTED_FORMATS: frozenset[str] = frozenset(_ENTRY_CONVERTERS.keys())


@dataclass(frozen=True)
class _BodyVerdict:
    code_keys: tuple[str, ...]
    message_keys: tuple[str, ...]
    rate_limit_codes: frozenset[int]


# Receivers that answer HTTP 200 to a refused message, with a non-zero code in the body.
_BODY_VERDICTS = {
    "wecom": _BodyVerdict(("errcode",), ("errmsg",), frozenset({45009})),
    "lark": _BodyVerdict(("code", "StatusCode"), ("msg", "StatusMessage"), frozenset({11232})),
}
