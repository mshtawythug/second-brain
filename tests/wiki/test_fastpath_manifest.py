"""Python-side unit tests for brain.wiki.fastpath_manifest.

Covers:
- read_manifest: valid JSON, version mismatch, malformed JSON, missing file.
- compute_fingerprint: idempotency, structural field changes, tag changes,
  wikilink/heading/block-ref/transclusion detection, error paths.
- ManifestError raised on unknown frontmatter fields and YAML parse errors.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from brain.wiki.fastpath_manifest import (
    FINGERPRINT_VERSION,
    Manifest,
    ManifestError,
    SlugEntry,
    _extract_block_refs,
    _extract_heading_anchors,
    _extract_inline_tags,
    _extract_transclusions,
    _extract_wikilinks,
    compute_fingerprint,
    read_manifest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(frontmatter: str, body: str = "") -> bytes:
    """Build a vault file source (frontmatter + body) as UTF-8 bytes."""
    text = f"---\n{frontmatter.strip()}\n---\n\n{body}" if frontmatter.strip() else body
    return text.encode("utf-8")


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# read_manifest
# ---------------------------------------------------------------------------


def test_read_manifest_valid(tmp_path: Path) -> None:
    """A well-formed manifest.json deserialises correctly."""
    data = {
        "version": FINGERPRINT_VERSION,
        "parent_build_id": "20260509-001122-abc123",
        "built_at_ms": 1_715_260_523_000,
        "slugs": {
            "my-note": {
                "fingerprint": "a" * 64,
                "output_path": "my-note.html",
                "source_path": "my-note.md",
            }
        },
    }
    _write_manifest(tmp_path / "manifest.json", data)
    m = read_manifest(tmp_path)
    assert isinstance(m, Manifest)
    assert m.version == FINGERPRINT_VERSION
    assert m.parent_build_id == "20260509-001122-abc123"
    assert m.built_at_ms == 1_715_260_523_000
    assert "my-note" in m.slugs
    entry = m.slugs["my-note"]
    assert isinstance(entry, SlugEntry)
    assert entry.fingerprint == "a" * 64
    assert entry.output_path == "my-note.html"
    assert entry.source_path == "my-note.md"


def test_read_manifest_version_mismatch(tmp_path: Path) -> None:
    """A manifest with a different version raises ManifestError."""
    data = {
        "version": FINGERPRINT_VERSION + 99,
        "parent_build_id": "build-001",
        "built_at_ms": 0,
        "slugs": {},
    }
    _write_manifest(tmp_path / "manifest.json", data)
    with pytest.raises(ManifestError, match="version"):
        read_manifest(tmp_path)


def test_read_manifest_malformed_json(tmp_path: Path) -> None:
    """A manifest with invalid JSON raises ManifestError."""
    (tmp_path / "manifest.json").write_text("not json {{{{", encoding="utf-8")
    with pytest.raises(ManifestError, match="malformed"):
        read_manifest(tmp_path)


def test_read_manifest_missing_file(tmp_path: Path) -> None:
    """A missing manifest.json raises ManifestError."""
    with pytest.raises(ManifestError, match="cannot read"):
        read_manifest(tmp_path)


def test_read_manifest_non_object_root(tmp_path: Path) -> None:
    """A manifest whose root is a JSON array raises ManifestError."""
    (tmp_path / "manifest.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ManifestError):
        read_manifest(tmp_path)


# ---------------------------------------------------------------------------
# compute_fingerprint — idempotency
# ---------------------------------------------------------------------------


def test_compute_fingerprint_idempotent() -> None:
    """Same source bytes + slug + paths always produce the same fingerprint."""
    src = _make_source("title: Hello\ntags:\n  - work\n", "Some body text.")
    fp1 = compute_fingerprint(
        source_bytes=src, slug="hello", source_path="hello.md", output_path="hello.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src, slug="hello", source_path="hello.md", output_path="hello.html"
    )
    assert fp1 == fp2
    assert len(fp1) == 64  # sha256 hex = 64 chars


def test_compute_fingerprint_body_change_does_not_affect_hash() -> None:
    """Prose-only body change (no structural elements) does NOT change fingerprint.

    The fingerprint covers structural elements (headings, wikilinks, etc.) not
    raw prose. This test confirms that adding/changing pure prose keeps the hash
    stable — which is the whole point of the fast path.
    """
    src1 = _make_source("title: Hello\n", "The quick brown fox.")
    src2 = _make_source("title: Hello\n", "The quick brown fox jumped over the lazy dog.")
    fp1 = compute_fingerprint(
        source_bytes=src1, slug="hello", source_path="hello.md", output_path="hello.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src2, slug="hello", source_path="hello.md", output_path="hello.html"
    )
    assert fp1 == fp2


# ---------------------------------------------------------------------------
# compute_fingerprint — structural field changes
# ---------------------------------------------------------------------------


def test_different_title_gives_different_hash() -> None:
    """Changing the ``title`` field changes the fingerprint."""
    src1 = _make_source("title: Alpha\n", "Body.")
    src2 = _make_source("title: Beta\n", "Body.")
    fp1 = compute_fingerprint(
        source_bytes=src1, slug="note", source_path="note.md", output_path="note.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src2, slug="note", source_path="note.md", output_path="note.html"
    )
    assert fp1 != fp2


def test_different_slug_gives_different_hash() -> None:
    """Different slug string produces a different fingerprint."""
    src = _make_source("title: Hello\n", "Body.")
    fp1 = compute_fingerprint(
        source_bytes=src, slug="hello", source_path="hello.md", output_path="hello.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src, slug="world", source_path="hello.md", output_path="world.html"
    )
    assert fp1 != fp2


def test_different_source_path_gives_different_hash() -> None:
    """Different source_path produces a different fingerprint."""
    src = _make_source("title: Hello\n", "Body.")
    fp1 = compute_fingerprint(
        source_bytes=src, slug="hello", source_path="a/hello.md", output_path="hello.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src, slug="hello", source_path="b/hello.md", output_path="hello.html"
    )
    assert fp1 != fp2


def test_ignored_field_does_not_change_hash() -> None:
    """Adding an ignored frontmatter field (Appendix A) does NOT change fingerprint."""
    src1 = _make_source("title: Hello\n", "Body.")
    src2 = _make_source("title: Hello\nowner: pat\nhits: 42\n", "Body.")
    fp1 = compute_fingerprint(
        source_bytes=src1, slug="hello", source_path="hello.md", output_path="hello.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src2, slug="hello", source_path="hello.md", output_path="hello.html"
    )
    assert fp1 == fp2


def test_draft_flag_changes_hash() -> None:
    """Toggling ``draft: true`` changes the fingerprint."""
    src1 = _make_source("title: Note\n", "Body.")
    src2 = _make_source("title: Note\ndraft: true\n", "Body.")
    fp1 = compute_fingerprint(
        source_bytes=src1, slug="note", source_path="note.md", output_path="note.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src2, slug="note", source_path="note.md", output_path="note.html"
    )
    assert fp1 != fp2


# ---------------------------------------------------------------------------
# compute_fingerprint — tags
# ---------------------------------------------------------------------------


def test_inline_tag_in_body_changes_hash() -> None:
    """Adding an inline ``#tag`` in body changes the fingerprint (SECTION_TAGS)."""
    src1 = _make_source("title: Note\n", "Plain body.")
    src2 = _make_source("title: Note\n", "Body with #work tag.")
    fp1 = compute_fingerprint(
        source_bytes=src1, slug="note", source_path="note.md", output_path="note.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src2, slug="note", source_path="note.md", output_path="note.html"
    )
    assert fp1 != fp2


def test_yaml_tag_changes_frontmatter_section() -> None:
    """Adding a tag to YAML frontmatter ``tags:`` changes fingerprint."""
    src1 = _make_source("title: Note\ntags:\n  - work\n", "Body.")
    src2 = _make_source("title: Note\ntags:\n  - work\n  - personal\n", "Body.")
    fp1 = compute_fingerprint(
        source_bytes=src1, slug="note", source_path="note.md", output_path="note.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src2, slug="note", source_path="note.md", output_path="note.html"
    )
    assert fp1 != fp2


# ---------------------------------------------------------------------------
# compute_fingerprint — wikilinks, transclusions, block-refs, headings
# ---------------------------------------------------------------------------


def test_wikilink_rename_changes_hash() -> None:
    """Renaming a wikilink target changes the fingerprint."""
    src1 = _make_source("title: Note\n", "See [[old-target]] for details.")
    src2 = _make_source("title: Note\n", "See [[new-target]] for details.")
    fp1 = compute_fingerprint(
        source_bytes=src1, slug="note", source_path="note.md", output_path="note.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src2, slug="note", source_path="note.md", output_path="note.html"
    )
    assert fp1 != fp2


def test_heading_rename_changes_hash() -> None:
    """Renaming a heading changes the fingerprint (heading anchor changes)."""
    src1 = _make_source("title: Note\n", "## Old Heading\nSome text.")
    src2 = _make_source("title: Note\n", "## New Heading\nSome text.")
    fp1 = compute_fingerprint(
        source_bytes=src1, slug="note", source_path="note.md", output_path="note.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src2, slug="note", source_path="note.md", output_path="note.html"
    )
    assert fp1 != fp2


def test_heading_reorder_changes_hash() -> None:
    """Reordering headings changes the fingerprint (anchors in document order)."""
    src1 = _make_source("title: Note\n", "## Alpha\n## Beta\n")
    src2 = _make_source("title: Note\n", "## Beta\n## Alpha\n")
    fp1 = compute_fingerprint(
        source_bytes=src1, slug="note", source_path="note.md", output_path="note.html"
    )
    fp2 = compute_fingerprint(
        source_bytes=src2, slug="note", source_path="note.md", output_path="note.html"
    )
    assert fp1 != fp2


# ---------------------------------------------------------------------------
# compute_fingerprint — error paths
# ---------------------------------------------------------------------------


def test_unknown_frontmatter_field_raises() -> None:
    """A frontmatter field not in structural or ignored lists raises ManifestError."""
    src = _make_source("title: Note\nmy_super_new_field: value\n", "Body.")
    with pytest.raises(ManifestError, match="unknown frontmatter field"):
        compute_fingerprint(
            source_bytes=src, slug="note", source_path="note.md", output_path="note.html"
        )


def test_yaml_parse_error_raises() -> None:
    """Malformed YAML frontmatter raises ManifestError."""
    # Tabs in YAML indentation are not allowed
    bad_yaml = "title: Hello\ntags:\n\t- work\n"
    src = f"---\n{bad_yaml}---\n\nBody.".encode()
    with pytest.raises(ManifestError):
        compute_fingerprint(
            source_bytes=src, slug="note", source_path="note.md", output_path="note.html"
        )


def test_non_utf8_bytes_raises() -> None:
    """Non-UTF-8 source bytes raise ManifestError."""
    with pytest.raises(ManifestError, match="not valid UTF-8"):
        compute_fingerprint(
            source_bytes=b"\xff\xfe invalid",
            slug="note", source_path="note.md", output_path="note.html",
        )


# ---------------------------------------------------------------------------
# Body extraction helpers — standalone unit tests
# ---------------------------------------------------------------------------


def test_extract_wikilinks_basic() -> None:
    body = "See [[my-doc]] and [[folder/other|Other]] for details."
    links = _extract_wikilinks(body)
    assert "my-doc" in links
    assert "folder/other" in links


def test_extract_wikilinks_excludes_transclusions() -> None:
    body = "![[transcluded]] is not a wikilink."
    links = _extract_wikilinks(body)
    assert "transcluded" not in links


def test_extract_wikilinks_preserves_anchor() -> None:
    body = "See [[target#some-heading]] for details."
    links = _extract_wikilinks(body)
    assert "target#some-heading" in links


def test_extract_wikilinks_drops_alias() -> None:
    body = "See [[target|Pretty Name]] for details."
    links = _extract_wikilinks(body)
    assert "target" in links
    assert "Pretty Name" not in links


def test_extract_transclusions() -> None:
    body = "Embedded: ![[other-doc#^block123]] and ![[full-page]]."
    targets = _extract_transclusions(body)
    assert "other-doc#^block123" in targets
    assert "full-page" in targets


def test_extract_block_refs() -> None:
    body = "Some paragraph text. ^myblock\n\nAnother paragraph. ^second-ref"
    refs = _extract_block_refs(body)
    assert "myblock" in refs
    assert "second-ref" in refs


def test_extract_block_refs_ignores_wikilink_targets() -> None:
    """``[[target#^block]]`` should NOT produce a block-ref definition."""
    body = "See [[target#^block]] for context."
    refs = _extract_block_refs(body)
    assert "block" not in refs


def test_extract_heading_anchors_order() -> None:
    body = "## First\n## Second\n# Top\n"
    anchors = _extract_heading_anchors(body)
    assert anchors == ["first", "second", "top"]


def test_extract_heading_anchors_deduplication() -> None:
    """Duplicate headings get github-slugger ``-1``, ``-2`` suffixes."""
    body = "## Foo\n## Foo\n## Foo\n"
    anchors = _extract_heading_anchors(body)
    assert anchors == ["foo", "foo-1", "foo-2"]


def test_extract_inline_tags() -> None:
    body = "Meeting notes #work #project-alpha for Q3."
    tags = _extract_inline_tags(body)
    assert "work" in tags
    assert "project-alpha" in tags


def test_extract_inline_tags_excludes_headings() -> None:
    """ATX headings (``# Heading``) are NOT inline tags."""
    body = "# My Heading\nSome content."
    tags = _extract_inline_tags(body)
    # "My" starts after "# " space; heading lines should not produce tags.
    assert "My" not in tags
    assert "my" not in tags


# ---------------------------------------------------------------------------
# FINGERPRINT_VERSION constant
# ---------------------------------------------------------------------------


def test_fingerprint_version_is_two() -> None:
    """FINGERPRINT_VERSION is 2."""
    assert FINGERPRINT_VERSION == 2
