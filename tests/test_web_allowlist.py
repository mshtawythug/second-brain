"""The `brain ingest-web` allowlist (RM-41): ``$BRAIN_HOME/web-allowlist.txt``.

Default-deny: no file loads as ``None`` and a file with no entries loads empty;
the command refuses both. A malformed line fails the whole load, naming the
line number and never the line (it is the owner's own browsing scope).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.config import ConfigError
from brain.settings.web_allowlist import (
    CONFIG_NAME,
    MAX_FILE_BYTES,
    WebAllowlist,
    allowlist_path,
    load_web_allowlist,
)


def _write(home: Path, text: str) -> Path:
    path = home / CONFIG_NAME
    path.write_text(text, encoding="utf-8")
    return path


def test_the_file_lives_in_brain_home(tmp_path: Path) -> None:
    assert allowlist_path(tmp_path) == tmp_path / "web-allowlist.txt"


def test_a_missing_file_loads_as_none(tmp_path: Path) -> None:
    assert load_web_allowlist(tmp_path) is None


def test_comments_and_blank_lines_only_load_as_empty(tmp_path: Path) -> None:
    _write(tmp_path, "# nothing yet\n\n   \n")

    allowlist = load_web_allowlist(tmp_path)

    assert allowlist is not None
    assert allowlist.is_empty
    assert not allowlist.allows("example.com")


def test_exact_and_wildcard_entries_load_normalized(tmp_path: Path) -> None:
    _write(tmp_path, "# docs\nExample.COM\n*.Example.org.\n  docs.example.net  \n")

    allowlist = load_web_allowlist(tmp_path)

    assert allowlist == WebAllowlist(
        exact=frozenset({"example.com", "docs.example.net"}),
        suffixes=frozenset({"example.org"}),
    )
    assert not allowlist.is_empty


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("example.com", True),
        ("www.example.com", False),  # exact entries do not cover subdomains
        ("docs.example.org", True),
        ("a.b.example.org", True),
        ("example.org", False),  # a wildcard never covers the apex (D1)
        ("badexample.org", False),  # label boundary
        ("example.org.evil.example.net", False),
        ("docs.example.net", True),
        ("", False),
    ],
)
def test_allows(host: str, allowed: bool) -> None:
    allowlist = WebAllowlist(
        exact=frozenset({"example.com", "docs.example.net"}),
        suffixes=frozenset({"example.org"}),
    )

    assert allowlist.allows(host) is allowed


def test_a_punycode_entry_matches_its_host(tmp_path: Path) -> None:
    _write(tmp_path, "xn--bcher-kva.example.com\n")

    allowlist = load_web_allowlist(tmp_path)

    assert allowlist is not None
    assert allowlist.allows("xn--bcher-kva.example.com")


@pytest.mark.parametrize("entry", ["bücher.example.com", "faß.example.de", "*.ｅxample.org"])
def test_a_non_ascii_entry_is_refused_with_the_punycode_hint(
    tmp_path: Path, entry: str
) -> None:
    """sec-114 F8: IDNA 2003 and UTS #46 disagree (faß), so entries must be punycode."""
    _write(tmp_path, f"example.com\n{entry}\n")

    with pytest.raises(ConfigError) as caught:
        load_web_allowlist(tmp_path)

    assert f"{CONFIG_NAME}: line 2 is not ASCII" in str(caught.value)
    assert "punycode (xn--)" in str(caught.value)
    assert entry not in str(caught.value)


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.1", "2130706433", "0x7f.1", "8.8.1.2", "2001:db8::1"]
)
def test_allows_never_matches_an_address_host(host: str) -> None:
    """sec-114 F8: even an allowlist built by hand cannot let an address through."""
    allowlist = WebAllowlist(exact=frozenset({host}), suffixes=frozenset({"1.2", "8.1.2"}))

    assert allowlist.allows(host) is False


@pytest.mark.parametrize(
    "entry",
    [
        "https://example.com",
        "example.com/path",
        "example.com:443",
        "user@example.com",
        "exa mple.com",
        "example.com # inline comment",
        "*",
        "*.",
        "*.com",
        "**.example.com",
        "docs.*.example.com",
        "example.*",
        "*example.com",
        "127.0.0.1",
        "10.0.0.8",
        "2001:db8::1",
        "[::1]",
        "*.10.0.0.8",
        "example..com",
        "-bad.example.com",
        # sec-114 F8: an address in any form, localhost, single labels.
        "127.1",
        "2130706433",
        "0x7f.1",
        "*.1.2",
        "*.0x7f.1",
        "localhost",
        "LOCALHOST.",
        "docs.localhost",
        "*.localhost",
        "intranet",
    ],
)
def test_a_malformed_line_fails_with_its_number_and_never_its_text(
    tmp_path: Path, entry: str
) -> None:
    _write(tmp_path, f"example.com\n# fine\n{entry}\n")

    with pytest.raises(ConfigError) as caught:
        load_web_allowlist(tmp_path)

    message = str(caught.value)
    assert f"{CONFIG_NAME}: line 3" in message
    assert entry.strip() not in message


def test_an_unreadable_file_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / CONFIG_NAME).mkdir()  # a directory where the file should be

    with pytest.raises(ConfigError, match="could not be read"):
        load_web_allowlist(tmp_path)


def test_a_file_that_is_not_utf8_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / CONFIG_NAME).write_bytes(b"example.com\n\xff\xfe\n")

    with pytest.raises(ConfigError, match="could not be read"):
        load_web_allowlist(tmp_path)


def test_an_oversized_file_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / CONFIG_NAME).write_bytes(b"# " + b"x" * MAX_FILE_BYTES + b"\n")

    with pytest.raises(ConfigError, match="is larger than"):
        load_web_allowlist(tmp_path)
