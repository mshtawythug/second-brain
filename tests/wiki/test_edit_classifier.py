"""Tests for brain.wiki.edit_classifier — 16-scenario corpus + error paths.

Organisation:
  TRIVIAL      — 8 scenarios where fingerprint is unchanged after modification
  NON-TRIVIAL  — 11 scenarios where fingerprint changes (includes description
                 + rename-guard slug-collision)
  ERRORS       — manifest missing / corrupt / wrong version / deleted file /
                 outside vault / unknown frontmatter field
  RESULT-FIELDS — completeness checks on ClassificationResult

Slugify parity tests live in tests/wiki/test_slug.py (one test per module).

Zero-false-trivials policy: every TRIVIAL scenario uses compute_fingerprint to
assert that original_bytes and modified_bytes produce the SAME fingerprint
BEFORE calling classify_edit. If the assertion fires, the test is mis-authored,
not a classifier bug.

description-field verdict: `description` IS in _STRUCTURAL_FIELD_ORDER (it
is a structural field that affects SEO meta rendering). Therefore changing
`description` is NON-TRIVIAL. See test_nontrivial_09_description_changed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from brain.wiki.edit_classifier import (
    ClassificationResult,
    EditClassification,
    classify_edit,
)
from brain.wiki.fastpath_manifest import (
    FINGERPRINT_VERSION,
    compute_fingerprint,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_source(frontmatter: str = "", body: str = "") -> bytes:
    """Build vault file bytes from optional frontmatter and body."""
    text = f"---\n{frontmatter.strip()}\n---\n\n{body}" if frontmatter.strip() else body
    return text.encode("utf-8")


def _fingerprint(source_bytes: bytes, slug: str) -> str:
    """Compute fingerprint with canonical source_path/output_path for slug."""
    return compute_fingerprint(
        source_bytes=source_bytes,
        slug=slug,
        source_path=f"{slug}.md",
        output_path=f"{slug}.html",
    )


def _write_manifest(
    fastpath_dir: Path,
    slug: str,
    fingerprint: str,
    *,
    source_path: str | None = None,
    output_path: str | None = None,
    version: int = FINGERPRINT_VERSION,
    parent_build_id: str = "20260509-001122-abc123",
) -> None:
    """Write manifest.json with the given fingerprint for slug."""
    sp = source_path or f"{slug}.md"
    op = output_path or f"{slug}.html"
    data: dict[str, Any] = {
        "version": version,
        "parent_build_id": parent_build_id,
        "built_at_ms": 1_715_260_523_000,
        "slugs": {
            slug: {
                "fingerprint": fingerprint,
                "output_path": op,
                "source_path": sp,
            }
        },
    }
    fastpath_dir.mkdir(parents=True, exist_ok=True)
    (fastpath_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")


def _setup_file(
    tmp_path: Path,
    slug: str,
    original_bytes: bytes,
    modified_bytes: bytes,
    *,
    expect_trivial: bool,
) -> tuple[Path, Path, Path]:
    """Create vault + fastpath dirs for a scenario.

    Writes manifest with fingerprint of *original_bytes* and writes
    *modified_bytes* to the source file (simulating a post-edit state).

    Also asserts that original and modified have the same fingerprint
    (expect_trivial=True) or different fingerprints (expect_trivial=False).
    This catches test-authoring bugs before the classifier runs.

    Returns (vault_root, fastpath_dir, source_path).
    """
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    fastpath_dir = tmp_path / "fastpath"
    fastpath_dir.mkdir()

    fp_orig = _fingerprint(original_bytes, slug)
    fp_mod = _fingerprint(modified_bytes, slug)

    if expect_trivial:
        assert fp_orig == fp_mod, (
            f"Test authoring bug: fingerprints differ for slug {slug!r} "
            f"but scenario is marked trivial"
        )
    else:
        assert fp_orig != fp_mod, (
            f"Test authoring bug: fingerprints are equal for slug {slug!r} "
            f"but scenario is marked non-trivial"
        )

    # Manifest stores fingerprint of ORIGINAL (pre-edit) file.
    _write_manifest(fastpath_dir, slug, fp_orig)

    # Source file on disk = MODIFIED (post-edit) bytes.
    source_path = vault_root / f"{slug}.md"
    source_path.write_bytes(modified_bytes)

    return vault_root, fastpath_dir, source_path


def _trivial_setup(
    tmp_path: Path, slug: str, original_bytes: bytes, modified_bytes: bytes
) -> tuple[Path, Path, Path]:
    return _setup_file(tmp_path, slug, original_bytes, modified_bytes, expect_trivial=True)


def _nontrivial_setup(
    tmp_path: Path, slug: str, original_bytes: bytes, modified_bytes: bytes
) -> tuple[Path, Path, Path]:
    return _setup_file(tmp_path, slug, original_bytes, modified_bytes, expect_trivial=False)


def _assert_trivial(result: ClassificationResult) -> None:
    assert result.classification == EditClassification.TRIVIAL, (
        f"Expected TRIVIAL, got NON_TRIVIAL: {result.reason}"
    )
    assert result.old_fingerprint is not None
    assert result.new_fingerprint is not None
    assert result.old_fingerprint == result.new_fingerprint


def _assert_nontrivial(result: ClassificationResult, *, reason_contains: str = "") -> None:
    assert result.classification == EditClassification.NON_TRIVIAL, (
        f"Expected NON_TRIVIAL, got TRIVIAL: {result.reason}"
    )
    if reason_contains:
        assert reason_contains in result.reason, (
            f"Expected reason to contain {reason_contains!r}, got: {result.reason!r}"
        )


# ---------------------------------------------------------------------------
# TRIVIAL scenarios (8) — fingerprint unchanged
# ---------------------------------------------------------------------------


class TestTrivialScenarios:
    """Eight scenarios where the edit does NOT change the structural fingerprint."""

    def test_trivial_01_whitespace_only_edit(self, tmp_path: Path) -> None:
        """Extra blank line mid-paragraph is prose-only — fingerprint unchanged."""
        slug = "trivial-01"
        orig = _make_source(body="First paragraph.\nSecond paragraph.")
        mod = _make_source(body="First paragraph.\n\nSecond paragraph.")
        vault, fdir, src = _trivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_trivial(result)
        assert result.slug == slug

    def test_trivial_02_trailing_newline_added(self, tmp_path: Path) -> None:
        """Trailing newline added — fingerprint unchanged."""
        slug = "trivial-02"
        orig = _make_source(body="Hello world.")
        mod = _make_source(body="Hello world.\n")
        vault, fdir, src = _trivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_trivial(result)

    def test_trivial_03_html_comment_added(self, tmp_path: Path) -> None:
        """HTML comment in body (no # char) — fingerprint unchanged."""
        slug = "trivial-03"
        orig = _make_source(body="Some text here.")
        mod = _make_source(body="Some text here.\n<!-- a reminder note -->")
        vault, fdir, src = _trivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_trivial(result)

    def test_trivial_04_plain_prose_change(self, tmp_path: Path) -> None:
        """Plain prose edited — no wikilinks, headings, or tags — fingerprint unchanged."""
        slug = "trivial-04"
        orig = _make_source(body="The quick brown fox.")
        mod = _make_source(body="The quick brown fox jumps over the lazy dog.")
        vault, fdir, src = _trivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_trivial(result)

    def test_trivial_05_ignored_frontmatter_external_id(self, tmp_path: Path) -> None:
        """Changing `external_id` (Appendix A ignored field) — fingerprint unchanged."""
        slug = "trivial-05"
        orig = _make_source(
            frontmatter="title: My Note\nexternal_id: old-abc123",
            body="Body text.",
        )
        mod = _make_source(
            frontmatter="title: My Note\nexternal_id: new-xyz789",
            body="Body text.",
        )
        vault, fdir, src = _trivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_trivial(result)

    def test_trivial_06_ignored_frontmatter_autopilot_field(self, tmp_path: Path) -> None:
        """Changing `autopilot_sweep_workload` (ignored) — fingerprint unchanged."""
        slug = "trivial-06"
        orig = _make_source(
            frontmatter="title: Note\nautopilot_sweep_workload: false",
            body="Content here.",
        )
        mod = _make_source(
            frontmatter="title: Note\nautopilot_sweep_workload: true",
            body="Content here.",
        )
        vault, fdir, src = _trivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_trivial(result)

    def test_trivial_07_trailing_whitespace_stripped(self, tmp_path: Path) -> None:
        """Trailing whitespace removed from prose lines — fingerprint unchanged."""
        slug = "trivial-07"
        orig = _make_source(body="Line one.   \nLine two.   ")
        mod = _make_source(body="Line one.\nLine two.")
        vault, fdir, src = _trivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_trivial(result)

    def test_trivial_08_mid_paragraph_word_change(self, tmp_path: Path) -> None:
        """Prose word change, no structural elements — fingerprint unchanged."""
        slug = "trivial-08"
        orig = _make_source(body="This is a paragraph about cats.")
        mod = _make_source(body="This is a paragraph about dogs.")
        vault, fdir, src = _trivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_trivial(result)


# ---------------------------------------------------------------------------
# NON-TRIVIAL scenarios (10) — fingerprint changed
# ---------------------------------------------------------------------------


class TestNonTrivialScenarios:
    """Ten scenarios where the edit DOES change the structural fingerprint."""

    def test_nontrivial_01_new_wikilink_added(self, tmp_path: Path) -> None:
        """New [[wikilink]] added to body — wikilinks section changes."""
        slug = "nontrivial-01"
        orig = _make_source(body="No links here.")
        mod = _make_source(body="No links here. Check [[other-doc]].")
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result, reason_contains="fingerprint changed")

    def test_nontrivial_02_wikilink_removed(self, tmp_path: Path) -> None:
        """Existing [[wikilink]] removed from body — wikilinks section changes."""
        slug = "nontrivial-02"
        orig = _make_source(body="See [[old-link]] for more info.")
        mod = _make_source(body="No more links here.")
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result, reason_contains="fingerprint changed")

    def test_nontrivial_03_wikilink_target_changed(self, tmp_path: Path) -> None:
        """Wikilink target changed from [[original]] to [[renamed]] — wikilinks differ."""
        slug = "nontrivial-03"
        orig = _make_source(body="See [[original-target]] for more.")
        mod = _make_source(body="See [[renamed-target]] for more.")
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result, reason_contains="fingerprint changed")

    def test_nontrivial_04_transclusion_added(self, tmp_path: Path) -> None:
        """Block transclusion ![[target#^block]] added — transclusions section changes."""
        slug = "nontrivial-04"
        orig = _make_source(body="No transclusions.")
        mod = _make_source(body="No transclusions.\n\n![[target-doc#^block1]]")
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result, reason_contains="fingerprint changed")

    def test_nontrivial_05_new_heading_added(self, tmp_path: Path) -> None:
        """New ATX heading added — heading anchors section changes."""
        slug = "nontrivial-05"
        orig = _make_source(body="Some prose without headings.")
        mod = _make_source(body="## New Section\n\nSome prose.")
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result, reason_contains="fingerprint changed")

    def test_nontrivial_06_heading_text_changed(self, tmp_path: Path) -> None:
        """Heading text changed — anchor slug changes — heading anchors differ."""
        slug = "nontrivial-06"
        orig = _make_source(body="## Original Heading\n\nBody text.")
        mod = _make_source(body="## Changed Heading\n\nBody text.")
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result, reason_contains="fingerprint changed")

    def test_nontrivial_07_frontmatter_title_changed(self, tmp_path: Path) -> None:
        """Frontmatter `title` is structural — change forces non-trivial."""
        slug = "nontrivial-07"
        orig = _make_source(frontmatter="title: Original Title", body="Body text.")
        mod = _make_source(frontmatter="title: Changed Title", body="Body text.")
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result, reason_contains="fingerprint changed")

    def test_nontrivial_08_frontmatter_tags_changed(self, tmp_path: Path) -> None:
        """Frontmatter `tags` is structural — adding a tag forces non-trivial."""
        slug = "nontrivial-08"
        orig = _make_source(
            frontmatter="title: Note\ntags: [tag1, tag2]", body="Body text."
        )
        mod = _make_source(
            frontmatter="title: Note\ntags: [tag1, tag2, tag3]", body="Body text."
        )
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result, reason_contains="fingerprint changed")

    def test_nontrivial_09_description_changed(self, tmp_path: Path) -> None:
        """Frontmatter `description` IS structural (in _STRUCTURAL_FIELD_ORDER).

        Per spec, description is an SEO meta field that Quartz renders. Changing
        it changes the canonical blob → NON_TRIVIAL. Scenario #5 in the task
        description is therefore NON-TRIVIAL, not trivial.
        """
        slug = "nontrivial-09"
        orig = _make_source(
            frontmatter="title: Note\ndescription: Original SEO description",
            body="Body text.",
        )
        mod = _make_source(
            frontmatter="title: Note\ndescription: Updated SEO description",
            body="Body text.",
        )
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result, reason_contains="fingerprint changed")

    def test_nontrivial_10_block_ref_added(self, tmp_path: Path) -> None:
        """New block-ref id `^blockid` defined in body — block_refs section changes."""
        slug = "nontrivial-10"
        orig = _make_source(body="Some content without a block ref.")
        mod = _make_source(body="Some content without a block ref. ^myblock")
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result, reason_contains="fingerprint changed")

    def test_nontrivial_11_slug_not_in_manifest(self, tmp_path: Path) -> None:
        """Slug computed from source_path is not in manifest — non-trivial."""
        slug_in_manifest = "some-other-note"
        unknown_slug = "unknown-note"

        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        fastpath_dir = tmp_path / "fastpath"
        fastpath_dir.mkdir()

        # Write manifest for a *different* slug.
        other_bytes = _make_source(body="Other note content.")
        fp = _fingerprint(other_bytes, slug_in_manifest)
        _write_manifest(fastpath_dir, slug_in_manifest, fp)

        # Source file for the slug that's NOT in the manifest.
        source_path = vault_root / f"{unknown_slug}.md"
        source_path.write_bytes(_make_source(body="Unknown note content."))

        result = classify_edit(
            fastpath_dir=fastpath_dir, source_path=source_path, vault_root=vault_root
        )
        _assert_nontrivial(result, reason_contains="slug not in manifest")
        assert result.slug == unknown_slug

    def test_nontrivial_12_source_path_changed_same_slug(self, tmp_path: Path) -> None:
        """Rename guard: 'a b.md' and 'a-b.md' collide on slug 'a-b'.

        The manifest was built when the file was named 'a b.md' (source_path
        recorded as 'a b.md').  The watcher now fires on 'a-b.md' — same slug
        but a DIFFERENT filename.  Without the rename guard this would be a
        false-trivial if the body is unchanged.  The guard must detect the
        path divergence and force NON_TRIVIAL.
        """
        # Slug produced by both filenames
        shared_slug = "a-b"

        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        fastpath_dir = tmp_path / "fastpath"
        fastpath_dir.mkdir()

        body = "Same body content — no structural changes."
        content = _make_source(body=body)

        # Manifest was written for the SPACED filename ("a b.md").
        spaced_source_path = "a b.md"
        fp = compute_fingerprint(
            source_bytes=content,
            slug=shared_slug,
            source_path=spaced_source_path,
            output_path=f"{shared_slug}.html",
        )
        _write_manifest(
            fastpath_dir,
            shared_slug,
            fp,
            source_path=spaced_source_path,
            output_path=f"{shared_slug}.html",
        )

        # Event fires on the HYPHENATED filename ("a-b.md").
        hyphenated_path = vault_root / "a-b.md"
        hyphenated_path.write_bytes(content)

        result = classify_edit(
            fastpath_dir=fastpath_dir,
            source_path=hyphenated_path,
            vault_root=vault_root,
        )

        # Must be NON_TRIVIAL — the path divergence is detected
        _assert_nontrivial(result, reason_contains="source path changed")
        assert "a b.md" in result.reason  # manifest path visible in reason
        assert "a-b.md" in result.reason  # current path visible in reason
        assert result.slug == shared_slug
        # old_fingerprint is available (entry was found before guard tripped)
        assert result.old_fingerprint is not None
        assert result.new_fingerprint is None


# ---------------------------------------------------------------------------
# ERROR PATH scenarios
# ---------------------------------------------------------------------------


class TestErrorPaths:
    """Error conditions that all force NON_TRIVIAL."""

    def test_error_manifest_missing(self, tmp_path: Path) -> None:
        """manifest.json does not exist — non-trivial with clear reason."""
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        fastpath_dir = tmp_path / "fastpath"
        fastpath_dir.mkdir()  # dir exists but no manifest.json inside

        source_path = vault_root / "my-note.md"
        source_path.write_bytes(_make_source(body="Some content."))

        result = classify_edit(
            fastpath_dir=fastpath_dir, source_path=source_path, vault_root=vault_root
        )
        _assert_nontrivial(result, reason_contains="manifest missing")
        assert result.slug == "my-note"
        assert result.old_fingerprint is None

    def test_error_manifest_json_corrupt(self, tmp_path: Path) -> None:
        """manifest.json contains invalid JSON — non-trivial."""
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        fastpath_dir = tmp_path / "fastpath"
        fastpath_dir.mkdir()
        (fastpath_dir / "manifest.json").write_text("{not valid json!!!", encoding="utf-8")

        source_path = vault_root / "my-note.md"
        source_path.write_bytes(_make_source(body="Content."))

        result = classify_edit(
            fastpath_dir=fastpath_dir, source_path=source_path, vault_root=vault_root
        )
        _assert_nontrivial(result, reason_contains="manifest unreadable")

    def test_error_manifest_version_mismatch(self, tmp_path: Path) -> None:
        """manifest.json has unsupported version (999) — non-trivial."""
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        fastpath_dir = tmp_path / "fastpath"
        fastpath_dir.mkdir()

        data: dict[str, Any] = {
            "version": 999,
            "parent_build_id": "20260509-001122-abc123",
            "built_at_ms": 1_715_260_523_000,
            "slugs": {},
        }
        (fastpath_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

        source_path = vault_root / "my-note.md"
        source_path.write_bytes(_make_source(body="Content."))

        result = classify_edit(
            fastpath_dir=fastpath_dir, source_path=source_path, vault_root=vault_root
        )
        _assert_nontrivial(result, reason_contains="version")

    def test_error_source_file_deleted(self, tmp_path: Path) -> None:
        """Source file does not exist (deleted/moved) — non-trivial."""
        slug = "deleted-note"
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        fastpath_dir = tmp_path / "fastpath"
        fastpath_dir.mkdir()

        original_bytes = _make_source(body="Original content.")
        fp = _fingerprint(original_bytes, slug)
        _write_manifest(fastpath_dir, slug, fp)

        # Do NOT create the source file — simulates deletion.
        source_path = vault_root / f"{slug}.md"

        result = classify_edit(
            fastpath_dir=fastpath_dir, source_path=source_path, vault_root=vault_root
        )
        _assert_nontrivial(result, reason_contains="source file missing")
        assert result.slug == slug

    def test_error_source_path_outside_vault(self, tmp_path: Path) -> None:
        """source_path outside vault_root — raises ValueError (programmer error)."""
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        fastpath_dir = tmp_path / "fastpath"
        fastpath_dir.mkdir()

        outside = tmp_path / "outside-vault.md"
        outside.write_bytes(b"content")

        with pytest.raises(ValueError, match="not inside vault_root"):
            classify_edit(
                fastpath_dir=fastpath_dir,
                source_path=outside,
                vault_root=vault_root,
            )

    def test_error_unknown_frontmatter_field(self, tmp_path: Path) -> None:
        """Unknown frontmatter field (not in structural or Appendix A lists) forces non-trivial.

        compute_fingerprint raises ManifestError for unknown fields, which
        classify_edit catches and returns as non-trivial.
        """
        slug = "unknown-field-note"
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        fastpath_dir = tmp_path / "fastpath"
        fastpath_dir.mkdir()

        # Build original content WITHOUT the unknown field to establish manifest fp.
        original_bytes = _make_source(
            frontmatter="title: My Note", body="Some content."
        )
        fp = _fingerprint(original_bytes, slug)
        _write_manifest(fastpath_dir, slug, fp)

        # Modified file INTRODUCES an unknown frontmatter field.
        modified_bytes = _make_source(
            frontmatter="title: My Note\nmy_completely_unknown_field: value",
            body="Some content.",
        )
        source_path = vault_root / f"{slug}.md"
        source_path.write_bytes(modified_bytes)

        result = classify_edit(
            fastpath_dir=fastpath_dir, source_path=source_path, vault_root=vault_root
        )
        _assert_nontrivial(result, reason_contains="fingerprint computation failed")


# ---------------------------------------------------------------------------
# Additional structural-field coverage
# ---------------------------------------------------------------------------


class TestStructuralFrontmatterFields:
    """Spot-check several more structural fields to confirm they force non-trivial."""

    def test_draft_changed_is_nontrivial(self, tmp_path: Path) -> None:
        slug = "draft-note"
        orig = _make_source(frontmatter="title: Note\ndraft: false", body="Content.")
        mod = _make_source(frontmatter="title: Note\ndraft: true", body="Content.")
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result)

    def test_aliases_changed_is_nontrivial(self, tmp_path: Path) -> None:
        slug = "alias-note"
        orig = _make_source(
            frontmatter="title: Note\naliases: [old-alias]", body="Content."
        )
        mod = _make_source(
            frontmatter="title: Note\naliases: [new-alias]", body="Content."
        )
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_nontrivial(result)

    def test_multiple_ignored_fields_still_trivial(self, tmp_path: Path) -> None:
        """Changing several ignored fields at once is still trivial."""
        slug = "multi-ignored"
        orig = _make_source(
            frontmatter=(
                "title: Note\n"
                "external_id: old\n"
                "id: uuid-1\n"
                "source: manual\n"
                "notes: old notes"
            ),
            body="Body.",
        )
        mod = _make_source(
            frontmatter=(
                "title: Note\n"
                "external_id: new\n"
                "id: uuid-2\n"
                "source: manual\n"
                "notes: new notes"
            ),
            body="Body.",
        )
        vault, fdir, src = _trivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        _assert_trivial(result)


# ---------------------------------------------------------------------------
# ClassificationResult field completeness
# ---------------------------------------------------------------------------


class TestResultFields:
    """Verify ClassificationResult is fully populated in each path."""

    def test_trivial_result_has_both_fingerprints(self, tmp_path: Path) -> None:
        slug = "result-fields"
        orig = _make_source(body="Original.")
        mod = _make_source(body="Modified.")  # prose-only change
        vault, fdir, src = _trivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        assert result.classification == EditClassification.TRIVIAL
        assert result.slug == slug
        assert result.old_fingerprint is not None
        assert result.new_fingerprint is not None
        assert len(result.old_fingerprint) == 64  # sha256 hex
        assert result.reason == "fingerprint unchanged"

    def test_nontrivial_result_has_both_fingerprints_when_computable(
        self, tmp_path: Path
    ) -> None:
        slug = "result-nontrivial"
        orig = _make_source(body="No links.")
        mod = _make_source(body="Has [[a-link]] now.")
        vault, fdir, src = _nontrivial_setup(tmp_path, slug, orig, mod)
        result = classify_edit(fastpath_dir=fdir, source_path=src, vault_root=vault)
        assert result.classification == EditClassification.NON_TRIVIAL
        assert result.slug == slug
        assert result.old_fingerprint is not None
        assert result.new_fingerprint is not None
        assert result.old_fingerprint != result.new_fingerprint

    def test_missing_manifest_result_has_no_fingerprints(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        fastpath_dir = tmp_path / "fastpath"
        fastpath_dir.mkdir()
        source_path = vault_root / "my-note.md"
        source_path.write_bytes(b"content")
        result = classify_edit(
            fastpath_dir=fastpath_dir, source_path=source_path, vault_root=vault_root
        )
        assert result.classification == EditClassification.NON_TRIVIAL
        assert result.old_fingerprint is None
        assert result.new_fingerprint is None
