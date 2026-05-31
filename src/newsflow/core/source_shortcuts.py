"""Source URL shortcuts: expand friendly forms like ``gh:owner/repo`` into the
provider's real RSS/Atom URL, so users needn't know each site's feed path.

This is pure UX sugar over the existing RSS path — every expanded value is a
normal feed that flows through the unchanged ``FeedFetcher``. Two guarantees
keep it from ever altering a real feed URL:

- anything containing ``"://"`` (i.e. already a URL) is returned unchanged;
- an unknown prefix, or a known prefix with a malformed body, is returned
  unchanged (then rejected downstream by ``validate_feed_url`` as usual).

So the worst case for a bad shortcut is the same "rejected URL" error the user
would get today — never a wrong feed.
"""

from urllib.parse import quote


def _gnews(query: str) -> str | None:
    """``gnews:<keywords>`` → Google News keyword-search RSS. hl/gl/ceid pick
    the edition; these defaults give the English/US edition (format verified
    2026-05). Prefix is ``gnews`` not ``news`` on purpose — ``news:`` is a real
    URI scheme and we never want to shadow one."""
    q = query.strip()
    if not q:
        return None
    return (
        f"https://news.google.com/rss/search?q={quote(q)}"
        "&hl=en-US&gl=US&ceid=US:en"
    )


def _github(spec: str) -> str | None:
    """``gh:owner/repo`` → that repo's releases Atom feed."""
    parts = spec.strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        return None
    owner, repo = parts
    return f"https://github.com/{owner}/{repo}/releases.atom"


def _youtube(channel_id: str) -> str | None:
    """``yt:<channel_id>`` → that channel's uploads feed. Expects a raw channel
    id (the ``UC…`` form), not a handle — handle resolution needs an API call."""
    cid = channel_id.strip()
    if not cid:
        return None
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"


def _pypi(pkg: str) -> str | None:
    """``pypi:<package>`` → that package's release RSS."""
    p = pkg.strip().strip("/")
    if not p:
        return None
    return f"https://pypi.org/rss/project/{p}/releases.xml"


def _reddit(sub: str) -> str | None:
    """``reddit:<subreddit>`` → that subreddit's RSS. Tolerates a leading
    ``r/``."""
    s = sub.strip().strip("/")
    if s.startswith("r/"):
        s = s[2:]
    if not s:
        return None
    return f"https://www.reddit.com/r/{s}.rss"


def _mastodon(spec: str) -> str | None:
    """``masto:user@instance`` → that account's RSS (``https://instance/@user.rss``)."""
    spec = spec.strip().lstrip("@")
    user, sep, instance = spec.partition("@")
    if not sep or not user or not instance:
        return None
    return f"https://{instance}/@{user}.rss"


# prefix (lowercased) → expander. Each returns a URL, or None when the body is
# malformed (caller then falls back to the original string).
_EXPANDERS = {
    "gnews": _gnews,
    "gh": _github,
    "yt": _youtube,
    "pypi": _pypi,
    "reddit": _reddit,
    "masto": _mastodon,
}


def expand_source_shortcut(raw: str) -> str:
    """Expand a ``prefix:body`` shortcut into a real feed URL.

    Returns ``raw`` unchanged when it already looks like a URL (contains
    ``"://"``), when the prefix is unknown, or when the body is malformed — so
    a real feed URL is never touched.
    """
    if not raw:
        return raw
    s = raw.strip()
    if "://" in s:
        return s
    prefix, sep, body = s.partition(":")
    if not sep:
        return s
    expander = _EXPANDERS.get(prefix.lower())
    if expander is None:
        return s
    return expander(body) or s
