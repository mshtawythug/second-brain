"""Unit tests for brain.vault.frontmatter.body_hash.

The hash is the canary the sync engine reads to decide whether a re-embed
is necessary. Keep it stable under: frontmatter-only edits, CRLF/LF
differences, and trailing whitespace tweaks.
"""
import hashlib

from brain.vault.frontmatter import body_hash, dump_frontmatter


def test_same_body_different_frontmatter_same_hash() -> None:
    """The hash ignores frontmatter — only the body matters."""
    a = dump_frontmatter({"id": "1", "title": "A"}, "the body\n")
    b = dump_frontmatter({"id": "1", "title": "A", "tags": ["x"]}, "the body\n")
    assert body_hash(a) == body_hash(b)


def test_identical_bodies_with_no_frontmatter_same_hash() -> None:
    a = "Just a body line.\n"
    b = "Just a body line.\n"
    assert body_hash(a) == body_hash(b)


def test_different_bodies_yield_different_hashes() -> None:
    a = dump_frontmatter({"id": "1"}, "body one")
    b = dump_frontmatter({"id": "1"}, "body two")
    assert body_hash(a) != body_hash(b)


def test_crlf_normalized_to_lf() -> None:
    """Windows-saved file vs macOS-saved file → same hash."""
    crlf = "---\r\nid: x\r\n---\r\n\r\nline one\r\nline two\r\n"
    lf = "---\nid: x\n---\n\nline one\nline two\n"
    assert body_hash(crlf) == body_hash(lf)


def test_leading_and_trailing_whitespace_tolerated() -> None:
    """Bodies that differ only by leading/trailing whitespace hash the same.

    Editors variously add/remove trailing newlines, indent the first line, etc.;
    the spec calls for ``.strip()`` before hashing so those cosmetic shifts
    don't trigger a re-embed.
    """
    a = dump_frontmatter({"id": "x"}, "the body")
    b = dump_frontmatter({"id": "x"}, "the body\n")
    c = dump_frontmatter({"id": "x"}, "the body\n\n\n")
    d = dump_frontmatter({"id": "x"}, "  \nthe body\n  ")
    assert body_hash(a) == body_hash(b) == body_hash(c) == body_hash(d)


def test_hash_is_deterministic() -> None:
    """Multiple calls with the same input return the same digest."""
    text = dump_frontmatter({"id": "x"}, "deterministic\n")
    h1 = body_hash(text)
    h2 = body_hash(text)
    assert h1 == h2


def test_hash_returns_64_hex_chars() -> None:
    """SHA-256 in hex is 64 chars."""
    h = body_hash("hello")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_value_for_known_body() -> None:
    """Pin the digest of a known input so a future change is caught."""
    text = dump_frontmatter({"id": "1"}, "stable body\n")
    expected = hashlib.sha256(b"stable body").hexdigest()
    assert body_hash(text) == expected


def test_empty_body_after_frontmatter() -> None:
    text = dump_frontmatter({"id": "x"}, "")
    # Empty body normalizes to empty string; hash is sha256("").
    assert body_hash(text) == hashlib.sha256(b"").hexdigest()


def test_body_with_only_unicode() -> None:
    text = dump_frontmatter({"id": "x"}, "中文 body\n")
    assert body_hash(text) == hashlib.sha256("中文 body".encode()).hexdigest()


def test_carriage_return_only_also_normalized() -> None:
    """Old-Mac line endings (``\\r``) collapse to LF too."""
    cr = "---\nid: x\n---\n\nbody\rline two\r"
    lf = "---\nid: x\n---\n\nbody\nline two\n"
    assert body_hash(cr) == body_hash(lf)


def test_file_without_frontmatter_hashes_full_text() -> None:
    """A file that opens without ``---`` is treated as pure body."""
    text = "no frontmatter at all\n"
    assert body_hash(text) == hashlib.sha256(b"no frontmatter at all").hexdigest()
