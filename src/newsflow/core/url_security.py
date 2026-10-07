"""URL safety checks for user-supplied feed URLs.

Allows only http/https, rejects IP-literal hosts that are private, loopback, link-local
or reserved, and caps URL length.

Does NOT protect against a hostname that resolves to a private IP at fetch time; that
needs a connector pinning the resolved IP before connect.
"""

import ipaddress
import socket
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
        raise InvalidFeedURLError(f"URL exceeds max length of {MAX_FEED_URL_LENGTH} characters")

    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise InvalidFeedURLError(f"Malformed URL: {e}") from e

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise InvalidFeedURLError(f"Scheme {parsed.scheme!r} not allowed (must be http or https)")

    host = parsed.hostname
    if not host:
        raise InvalidFeedURLError("URL has no host")

    ip = _ip_literal(host)
    if ip is None:
        return  # Hostname, not an IP literal — OK at this layer (see the DNS caveat above).

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise InvalidFeedURLError(f"Host {host} resolves to a private/loopback/link-local address")
    if ip.is_multicast or ip.is_unspecified:
        raise InvalidFeedURLError(f"Host {host} is a multicast/unspecified address")


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address `host` denotes without a DNS lookup, or None for a hostname.

    Asks the OS resolver, which is what the connection will use: it accepts shorthand
    such as ``127.1`` or ``0x7f000001`` that ``ipaddress`` rejects as not an address.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, flags=socket.AI_NUMERICHOST)
    except (OSError, UnicodeError, ValueError):
        return None
    return ipaddress.ip_address(infos[0][4][0])
