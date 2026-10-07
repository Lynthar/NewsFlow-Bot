"""Tests for validate_feed_url — SSRF guard for user-supplied feed URLs."""

import pytest

from newsflow.core.url_security import (
    MAX_FEED_URL_LENGTH,
    InvalidFeedURLError,
    validate_feed_url,
)


def test_accepts_plain_http_and_https():
    validate_feed_url("http://example.com/feed.xml")
    validate_feed_url("https://example.com/feed.xml")
    validate_feed_url("https://sub.example.com:8443/path?query=1")


def test_accepts_public_ip_literal():
    validate_feed_url("http://8.8.8.8/feed")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/feed",
        "gopher://example.com/feed",
        "javascript:alert(1)",
        "data:text/xml,<rss/>",
    ],
)
def test_rejects_non_http_schemes(url):
    with pytest.raises(InvalidFeedURLError):
        validate_feed_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/feed",  # loopback
        "http://localhost/feed",  # hostname — note: we only catch IP literals here
        "http://[::1]/feed",  # IPv6 loopback
        "http://10.0.0.1/feed",  # private
        "http://192.168.1.1/feed",  # private
        "http://172.16.0.1/feed",  # private
        "http://169.254.169.254/latest/meta-data/",  # AWS/GCP metadata
        "http://[fe80::1]/feed",  # IPv6 link-local
        "http://0.0.0.0/feed",  # unspecified
    ],
)
def test_rejects_private_ip_literals(url):
    if "localhost" in url:
        # localhost is a hostname, not an IP — this layer doesn't catch
        # hostnames. Make that explicit in the test.
        validate_feed_url(url)
        return
    with pytest.raises(InvalidFeedURLError):
        validate_feed_url(url)


@pytest.mark.parametrize(
    "host",
    ["127.1", "0x7f.0.0.1", "2130706433", "0x7f000001", "10.1", "0xa9fea9fe", "0"],
)
def test_rejects_shorthand_ip_literals(host):
    # ipaddress rejects these spellings, but the resolver the fetch goes through
    # reads each one as a loopback, private, link-local or unspecified address.
    with pytest.raises(InvalidFeedURLError):
        validate_feed_url(f"http://{host}/feed")


def test_accepts_shorthand_for_a_public_address():
    validate_feed_url("http://1.1/feed")


def test_rejects_empty_and_whitespace():
    with pytest.raises(InvalidFeedURLError):
        validate_feed_url("")
    with pytest.raises(InvalidFeedURLError):
        validate_feed_url("   ")


def test_rejects_oversize_url():
    huge = "https://example.com/" + "a" * MAX_FEED_URL_LENGTH
    with pytest.raises(InvalidFeedURLError):
        validate_feed_url(huge)


def test_accepts_url_exactly_at_the_length_cap():
    """The cap mirrors Feed.url's String(2048): a URL that fits the column is legal."""
    prefix = "https://example.com/"
    url = prefix + "a" * (MAX_FEED_URL_LENGTH - len(prefix))
    assert len(url) == MAX_FEED_URL_LENGTH
    validate_feed_url(url)


@pytest.mark.parametrize("url", ["http://224.0.0.1/feed", "http://[ff02::1]/feed"])
def test_rejects_multicast_literals(url):
    # A multicast group is never a unicast HTTP endpoint (RFC 1112, RFC 4291 §2.7).
    with pytest.raises(InvalidFeedURLError):
        validate_feed_url(url)


def test_rejects_url_with_no_host():
    with pytest.raises(InvalidFeedURLError):
        validate_feed_url("http:///feed")
