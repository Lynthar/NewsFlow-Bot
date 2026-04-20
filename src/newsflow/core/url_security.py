"""URL safety checks for user-supplied feed URLs.

Rejects URLs that would allow an attacker to use the bot as a request
forwarder against the host's private network (SSRF). Specifically:

- only http/https schemes are allowed
- IP-literal hosts are rejected if they're private, loopback, link-local,
  or reserved (catches 127.0.0.1, 10/8, 192.168/16, 169.254.169.254, ::1, …)
- URL length is capped

What this does NOT protect against: a hostname that resolves at fetch time
to a private IP. DNS-rebinding-style SSRF needs a custom aiohttp connector
that pins the resolved IP before connect. For now we lean on the container
egress policy / VPS network boundary for that defense.
"""

import ipaddress
from urllib.parse import urlparse

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_FEED_URL_LENGTH = 2048


class InvalidFeedURLError(ValueError):
    """Raised when a feed URL is rejected by validate_feed_url."""


def validate_feed_url(url: str) -> None:
    """Raise InvalidFeedURLError if `url` is malformed or unsafe to fetch."""
    if not url or not url.strip():
        raise InvalidFeedURLError("URL is empty")

    if len(url) > MAX_FEED_URL_LENGTH:
        raise InvalidFeedURLError(
            f"URL exceeds max length of {MAX_FEED_URL_LENGTH} characters"
        )

    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise InvalidFeedURLError(f"Malformed URL: {e}") from e

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise InvalidFeedURLError(
            f"Scheme {parsed.scheme!r} not allowed (must be http or https)"
        )

    host = parsed.hostname
    if not host:
        raise InvalidFeedURLError("URL has no host")

    # If host is an IP literal, reject unsafe ranges. Hostname-based URLs
    # pass this check — see module docstring for the DNS caveat.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # Hostname, not an IP literal — OK at this layer.

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise InvalidFeedURLError(
            f"Host {host} resolves to a private/loopback/link-local address"
        )
    if ip.is_multicast or ip.is_unspecified:
        raise InvalidFeedURLError(
            f"Host {host} is a multicast/unspecified address"
        )
