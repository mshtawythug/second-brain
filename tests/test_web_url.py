"""URL normalization for `brain ingest-web` (RM-41): what a stored page URL keeps and drops.

Synthetic URLs only (``example.com`` / ``example.org``). The query string, the
fragment and the userinfo carry session tokens, search terms and credentials, so
they never survive normalization (ticket protocol point 6).
"""
from __future__ import annotations

import pytest

from brain.ingest.web_url import (
    MAX_URL_CHARS,
    host_of,
    is_numeric_host,
    normalize_host,
    normalize_url,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/a/b", "https://example.com/a/b"),
        ("HTTPS://Docs.Example.COM/Path", "https://docs.example.com/Path"),
        ("https://example.com", "https://example.com/"),
        ("http://example.org/x", "http://example.org/x"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com./a", "https://example.com/a"),
        ("https://bücher.example.com/", "https://xn--bcher-kva.example.com/"),
        ("https://[2001:db8::1]:8443/a", "https://[2001:db8::1]:8443/a"),
    ],
)
def test_normalize_url_canonicalizes_scheme_host_and_port(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/a?session=synthetic-token-123",
        "https://example.com/a#section-2",
        "https://example.com/a?q=search+terms#frag",
        "https://user:synthetic-pass@example.com/a",
        "https://user@example.com/a",
    ],
)
def test_query_fragment_and_userinfo_are_dropped(raw: str) -> None:
    assert normalize_url(raw) == "https://example.com/a"


@pytest.mark.parametrize(
    "raw",
    [
        "file:///etc/hosts",
        "chrome://settings/",
        "about:blank",
        "data:text/html,hi",
        "javascript:alert(1)",
        "ftp://example.com/a",
        "",
        "not a url",
        "https://",
        "https:///path-only",
        "https://exa mple.com/",
        "https://example.com:notaport/",
        "https://example.com:99999/",
        "https://example..com/",
        "https://-bad-.example.com/",
        "https://example.com/a b",
        "https://example.com/a\x1bb",
        "https://example_com/",
    ],
)
def test_unusable_urls_normalize_to_none(raw: str) -> None:
    assert normalize_url(raw) is None


def test_a_url_over_the_length_cap_is_refused() -> None:
    path = "a" * MAX_URL_CHARS

    assert normalize_url(f"https://example.com/{path}") is None


def test_a_url_at_the_cap_is_kept() -> None:
    prefix = "https://example.com/"
    url = prefix + "a" * (MAX_URL_CHARS - len(prefix))

    assert normalize_url(url) == url


def test_host_of_returns_the_normalized_host() -> None:
    assert host_of("https://docs.example.com:8443/a") == "docs.example.com"
    assert host_of("https://[2001:db8::1]/a") == "2001:db8::1"


def test_host_of_a_url_without_a_host_is_empty() -> None:
    assert host_of("about:blank") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.COM", "example.com"),
        ("example.com.", "example.com"),
        ("bücher.example.com", "xn--bcher-kva.example.com"),
        ("127.0.0.1", "127.0.0.1"),
        ("2001:DB8::1", "2001:db8::1"),
        ("", None),
        (".", None),
        ("a..b", None),
        ("-a.example.com", None),
        ("a-.example.com", None),
        ("exa mple.com", None),
        ("example_com", None),
        ("a" * 64 + ".example.com", None),
        ("x" * 254, None),
        (".".join(["a" * 63] * 4), None),  # every label legal, the whole name too long
    ],
)
def test_normalize_host(raw: str, expected: str | None) -> None:
    assert normalize_host(raw) == expected


# --- sec-114 F3: path parameters carry session ids -------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/a;jsessionid=ABCDEF0123456789?x=1#f", "https://example.com/a"),
        ("https://example.com/app;JSESSIONID=synthetic/page;sid=1", "https://example.com/app/page"),
        ("https://example.com/;phpsessid=synthetic", "https://example.com/"),
        ("https://example.com/a;b;c/d", "https://example.com/a/d"),
        ("https://example.com/keep%3Bencoded", "https://example.com/keep%3Bencoded"),
    ],
)
def test_path_parameters_are_dropped_from_every_segment(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


# --- sec-114 F8: an address in any form is not a name ---------------------------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "10.0.0.8", "2001:db8::1", "127.1", "2130706433", "0x7f.1", "0X7F.1",
     "1.2", "a.1", "example.0x", "8.8.1.2."],
)
def test_numeric_hosts(host: str) -> None:
    assert is_numeric_host(host) is True


@pytest.mark.parametrize(
    "host", ["example.com", "1a.example.com", "0xexample.com", "example.0xg", "x-1.example.org"]
)
def test_named_hosts(host: str) -> None:
    assert is_numeric_host(host) is False
