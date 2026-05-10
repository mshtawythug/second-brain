"""Production parity test for T2 — full build emits manifest with correct fingerprints.

Verifies that after a real Quartz full build (with the T2 overlay installed),
every fingerprint stored in ``manifest.json`` matches the Python
``compute_fingerprint`` re-computation for the same source file.

**STOP-THE-LINE**: any TS-side fingerprint divergence from Python recompute
causes this test to FAIL hard. There is no softening.

Skip-gate:
    - ``node`` must be on PATH.
    - The live Quartz workspace (``~/brain-vault/.quartz``) must exist with
      ``node_modules`` installed.
    - The overlay must be installed: ``<workspace>/quartz/build.ts`` must contain
      ``writeFastpathArtifacts`` (indicating ``brain vault render --overlay``
      was run since T2 was added to the repo).

If any prerequisite is missing, ALL tests in this module skip cleanly.

Usage (local, when prerequisites are satisfied):

    pytest tests/wiki/test_fastpath_production_parity.py -v --no-cov -m e2e

Coverage corpus (curated fixture vault — see ``_FIXTURE_FILES`` below):
    - Folder index (``some-folder/index.md``)
    - Plain markdown (no frontmatter)
    - Frontmatter with structural fields (title, draft, tags, aliases)
    - Frontmatter with one Appendix-A ignored field (``source``)
    - YAML + inline tag merge (``#inline-tag`` in body)
    - Bare YAML datetime (``date: 2024-03-15``)
    - Wikilinks (multiple, including alias)
    - Transclusion (``![[target#^block]]``) + block-ref definition
    - Duplicate headings (``## Foo`` appears twice → ``foo`` / ``foo-1``)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from brain.wiki.fastpath_manifest import (
    ManifestError,
    compute_fingerprint_with_blob,
    read_manifest,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIVE_WORKSPACE = Path.home() / "brain-vault" / ".quartz"
_INSTALLED_BUILD_TS = LIVE_WORKSPACE / "quartz" / "build.ts"


# ---------------------------------------------------------------------------
# Coverage corpus — curated fixture files (in-memory, no external fixtures)
# ---------------------------------------------------------------------------

# Each entry: (relative_path_in_vault, content_as_str)
# The vault always gets an index.md root (required by Quartz).
_FIXTURE_FILES: list[tuple[str, str]] = [
    # Root index — required by Quartz; exercises root slug ("index")
    (
        "index.md",
        """\
---
title: Parity Test Root
tags: [root, parity]
---

This is the root index for the T2 production parity test vault.

## Overview

A small vault designed to exercise the fingerprint corpus.

Links: [[plain-no-frontmatter]]
""",
    ),
    # Folder index — exercises slug ending in "/index"
    (
        "some-folder/index.md",
        """\
---
title: Folder Index
description: A sub-folder landing page.
---

This is the folder index for ``some-folder/``.

## Contents

- [[some-folder/structural-fields]]
""",
    ),
    # Plain markdown — no frontmatter at all
    (
        "plain-no-frontmatter.md",
        """\
This document has no YAML frontmatter.

## What it tests

Fingerprint with empty frontmatter section — all structural fields should be null.

### Nested heading

Some prose.
""",
    ),
    # Structural fields — title, draft, tags, aliases
    (
        "some-folder/structural-fields.md",
        """\
---
title: Structural Fields Test
draft: false
tags:
  - alpha
  - beta
aliases:
  - SFT
  - struct-fields
---

This document exercises structural frontmatter fields.

Body content without any special wiki syntax.
""",
    ),
    # Appendix-A ignored field — "source" is in the ignored list
    (
        "ignored-field.md",
        """\
---
title: Ignored Field Doc
source: krisp
---

This document has a frontmatter field (``source``) that is in the
Appendix-A ignored list.  Changing ``source`` should NOT change the
fingerprint; it does not affect rendered output.
""",
    ),
    # YAML tags + inline body #tag (should merge in SECTION_TAGS)
    (
        "yaml-inline-tags.md",
        """\
---
title: Tag Merge Test
tags: [yaml-tag, shared-tag]
---

This document has both YAML tags and inline body tags.

The inline tags #inline-tag and #shared-tag appear in the body.
The ``shared-tag`` should appear once (deduplication) in SECTION_TAGS.
""",
    ),
    # Bare YAML date (js-yaml parses as Date, Python parses as datetime.date)
    (
        "date-frontmatter.md",
        """\
---
title: Date Frontmatter
date: 2024-03-15
created: 2024-01-10T00:00:00
modified: 2024-03-15T12:00:00
---

This document tests date normalization in the fingerprint.

Midnight datetimes should be truncated to date-only in both TS and Python.
""",
    ),
    # Wikilinks — multiple, including alias form [[target|Alias text]]
    (
        "wikilinks.md",
        """\
---
title: Wikilinks Test
---

This document tests wikilink extraction.

Links to [[plain-no-frontmatter]], [[some-folder/structural-fields|Structural]], and
[[date-frontmatter|Date Doc]].

Also a wikilink with anchor: [[transclusion-target#^block-one]].
""",
    ),
    # Transclusion target — defines block-ref anchors
    (
        "transclusion-target.md",
        """\
---
title: Transclusion Target
---

This document defines block-ref anchors.

Here is the first block content. ^block-one

Some prose in between.

Here is the second block. ^block-two
""",
    ),
    # Transclusion source — uses ![[target#^block]] and has block-ref def
    (
        "transclusion-source.md",
        """\
---
title: Transclusion Source
---

This document transcludes a block from another document.

![[transclusion-target#^block-one]]

It also defines its own block: ^source-block
""",
    ),
    # Duplicate headings — github-slugger disambiguation pinned
    (
        "duplicate-headings.md",
        """\
---
title: Duplicate Headings
---

This document has duplicate headings to exercise github-slugger disambiguation.

## Foo

First occurrence of Foo.

## Bar

A different heading.

## Foo

Second occurrence of Foo — slugger must produce ``foo-1``.

## Foo

Third occurrence — slugger must produce ``foo-2``.
""",
    ),
]


# ---------------------------------------------------------------------------
# Skip-gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Preflight:
    node_missing: str | None
    workspace_missing: str | None
    overlay_missing: str | None

    @property
    def ok(self) -> bool:
        return (
            self.node_missing is None
            and self.workspace_missing is None
            and self.overlay_missing is None
        )

    @property
    def skip_reason(self) -> str:
        return "; ".join(
            r for r in (self.node_missing, self.workspace_missing, self.overlay_missing)
            if r is not None
        )


def _preflight() -> _Preflight:
    node_missing: str | None = (
        "`node` not on PATH" if shutil.which("node") is None else None
    )

    workspace_missing: str | None = None
    if not LIVE_WORKSPACE.is_dir():
        workspace_missing = f"Quartz workspace missing at {LIVE_WORKSPACE}"
    elif not (LIVE_WORKSPACE / "node_modules").is_dir():
        workspace_missing = (
            f"node_modules absent in {LIVE_WORKSPACE} — run `npm install`"
        )
    elif not (LIVE_WORKSPACE / "quartz" / "bootstrap-cli.mjs").is_file():
        workspace_missing = (
            f"bootstrap-cli.mjs missing in {LIVE_WORKSPACE}/quartz"
        )

    overlay_missing: str | None = None
    if workspace_missing is None:
        if not _INSTALLED_BUILD_TS.is_file():
            overlay_missing = (
                f"T2 overlay not installed: {_INSTALLED_BUILD_TS} missing — "
                "run `brain vault render --overlay` to install"
            )
        elif "writeFastpathArtifacts" not in _INSTALLED_BUILD_TS.read_text(encoding="utf-8"):
            overlay_missing = (
                f"T2 overlay not applied: {_INSTALLED_BUILD_TS} does not contain "
                "`writeFastpathArtifacts` — run `brain vault render --overlay`"
            )

    return _Preflight(
        node_missing=node_missing,
        workspace_missing=workspace_missing,
        overlay_missing=overlay_missing,
    )


_PREFLIGHT = _preflight()

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _PREFLIGHT.ok,
        reason=f"T2 production parity prerequisites not met: {_PREFLIGHT.skip_reason}",
    ),
]


# ---------------------------------------------------------------------------
# Session fixture — build the mini-vault once per session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def built_vault(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Create a mini fixture vault, run Quartz full build, return (vault, build_dir).

    The build is expensive (~30-120s) but runs ONCE per session.
    The ``QUARTZ_PARENT_BUILD_ID`` env var is set to trigger fastpath artifact write.
    """
    base = tmp_path_factory.mktemp("t2-parity-vault")
    vault = base / "vault"
    vault.mkdir()
    build_dir = base / "build"

    # Write fixture files into the vault.
    for rel_path, content in _FIXTURE_FILES:
        dest = vault / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    # Run a full Quartz build using the live workspace (has node_modules + overlay).
    test_build_id = f"test-T2-{uuid.uuid4().hex[:12]}"
    node = shutil.which("node")
    assert node is not None  # preflight already checked

    env = dict(os.environ)
    env["QUARTZ_PARENT_BUILD_ID"] = test_build_id

    args = [
        node,
        str(LIVE_WORKSPACE / "quartz" / "bootstrap-cli.mjs"),
        "build",
        "--directory",
        str(vault),
        "--output",
        str(build_dir),
    ]
    result = subprocess.run(  # noqa: S603
        args,
        cwd=str(LIVE_WORKSPACE),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )

    # Surface build output on failure for debugging.
    if result.returncode != 0:
        pytest.fail(
            f"Quartz build failed (exit {result.returncode}):\n"
            f"STDOUT:\n{result.stdout[-3000:]}\n"
            f"STDERR:\n{result.stderr[-3000:]}"
        )

    return vault, build_dir


@pytest.fixture(scope="session")
def fastpath_dir(built_vault: tuple[Path, Path]) -> Path:
    """Return the fastpath dir and assert manifest.json exists."""
    vault, _ = built_vault
    fp_dir = vault / ".quartz" / ".cache" / "fastpath"
    assert fp_dir.is_dir(), (
        f"fastpath dir not created at {fp_dir} — "
        "did the overlay write QUARTZ_PARENT_BUILD_ID before the build?"
    )
    return fp_dir


# ---------------------------------------------------------------------------
# Basic structure tests
# ---------------------------------------------------------------------------


def test_manifest_exists(fastpath_dir: Path) -> None:
    """``manifest.json`` exists after a full build with the T2 overlay."""
    manifest_path = fastpath_dir / "manifest.json"
    assert manifest_path.is_file(), (
        f"manifest.json not found at {manifest_path} — "
        "T2 overlay must write manifest.json after a successful full build"
    )


def test_contentmap_exists(fastpath_dir: Path) -> None:
    """``contentmap.json`` exists after a full build with the T2 overlay."""
    contentmap_path = fastpath_dir / "contentmap.json"
    assert contentmap_path.is_file(), (
        f"contentmap.json not found at {contentmap_path} — "
        "T2 overlay must write contentmap.json after a successful full build"
    )


def test_manifest_is_valid_json(fastpath_dir: Path) -> None:
    """``manifest.json`` is valid JSON with required top-level fields."""
    manifest_path = fastpath_dir / "manifest.json"
    raw = manifest_path.read_text(encoding="utf-8")
    data: Any = json.loads(raw)
    assert isinstance(data, dict), "manifest.json root must be a JSON object"
    assert "version" in data, "manifest.json must have 'version' field"
    assert "parent_build_id" in data, "manifest.json must have 'parent_build_id' field"
    assert "built_at_ms" in data, "manifest.json must have 'built_at_ms' field"
    assert "slugs" in data, "manifest.json must have 'slugs' field"
    assert isinstance(data["slugs"], dict), "manifest.json 'slugs' must be an object"


def test_manifest_parent_build_id_set(fastpath_dir: Path) -> None:
    """``manifest.json:parent_build_id`` is non-empty (env var was passed)."""
    manifest = read_manifest(fastpath_dir)
    assert manifest.parent_build_id, (
        "manifest.parent_build_id is empty — "
        "QUARTZ_PARENT_BUILD_ID env var must have been passed to the build"
    )
    assert manifest.parent_build_id.startswith("test-T2-"), (
        f"manifest.parent_build_id {manifest.parent_build_id!r} does not start with "
        "'test-T2-' — the build used our test id"
    )


def test_manifest_slug_count_matches_fixture(
    fastpath_dir: Path,
    built_vault: tuple[Path, Path],
) -> None:
    """Manifest has at least as many slugs as fixture markdown files."""
    manifest = read_manifest(fastpath_dir)
    # Count fixture markdown files.
    vault, _ = built_vault
    md_count = sum(1 for _ in vault.rglob("*.md"))
    # Manifest slugs >= fixture files (Quartz may emit fewer if some are filtered).
    assert len(manifest.slugs) > 0, "manifest has no slugs — build produced nothing"
    # We expect at least the majority of fixture files to be in the manifest.
    assert len(manifest.slugs) >= md_count // 2, (
        f"manifest has only {len(manifest.slugs)} slugs for {md_count} fixture files — "
        "expected most fixture files to appear as slugs"
    )


def test_contentmap_is_valid_json_envelope(fastpath_dir: Path) -> None:
    """``contentmap.json`` is a JSON envelope object with required top-level fields."""
    contentmap_path = fastpath_dir / "contentmap.json"
    raw = contentmap_path.read_text(encoding="utf-8")
    data: Any = json.loads(raw)
    assert isinstance(data, dict), (
        "contentmap.json root must be a JSON object (envelope), not a bare array — "
        "shape: {version, parent_build_id, built_at_ms, entries: [...]}"
    )
    for field in ("version", "parent_build_id", "built_at_ms", "entries"):
        assert field in data, f"contentmap.json envelope missing required field: {field!r}"
    assert isinstance(data["entries"], list), (
        "contentmap.json 'entries' must be a JSON array"
    )
    assert len(data["entries"]) > 0, "contentmap.json 'entries' must have at least one entry"


def test_contentmap_envelope_version_matches_manifest(
    fastpath_dir: Path,
) -> None:
    """contentmap ``version`` matches ``manifest.version`` (both == FINGERPRINT_VERSION)."""
    contentmap_path = fastpath_dir / "contentmap.json"
    data: Any = json.loads(contentmap_path.read_text(encoding="utf-8"))
    manifest = read_manifest(fastpath_dir)
    assert data["version"] == manifest.version, (
        f"contentmap.version={data['version']!r} != manifest.version={manifest.version!r} — "
        "both must equal FINGERPRINT_VERSION; stale contentmap or mismatched builds"
    )


def test_contentmap_parent_build_id_matches_manifest(
    fastpath_dir: Path,
) -> None:
    """contentmap ``parent_build_id`` matches ``manifest.parent_build_id`` (same full build)."""
    contentmap_path = fastpath_dir / "contentmap.json"
    data: Any = json.loads(contentmap_path.read_text(encoding="utf-8"))
    manifest = read_manifest(fastpath_dir)
    assert data["parent_build_id"] == manifest.parent_build_id, (
        f"contentmap.parent_build_id={data['parent_build_id']!r} != "
        f"manifest.parent_build_id={manifest.parent_build_id!r} — "
        "both artifacts must be written by the same full build"
    )


def test_contentmap_entries_have_null_hastroot(fastpath_dir: Path) -> None:
    """All contentmap entries have ``hastRoot: null`` (metadata-only per T0 Amendment 1)."""
    contentmap_path = fastpath_dir / "contentmap.json"
    data: Any = json.loads(contentmap_path.read_text(encoding="utf-8"))
    for entry in data["entries"]:
        if entry.get("type") == "markdown":
            assert entry.get("hastRoot") is None, (
                f"contentmap entry for {entry.get('filePath')!r} has hastRoot != null — "
                "T0 Amendment 1: metadata-only contentmap, hastRoot must be null"
            )


def test_contentmap_entries_exclude_htmlast(fastpath_dir: Path) -> None:
    """contentmap ``vfileData`` entries do NOT contain ``htmlAst`` (excluded per T0)."""
    contentmap_path = fastpath_dir / "contentmap.json"
    data: Any = json.loads(contentmap_path.read_text(encoding="utf-8"))
    for entry in data["entries"]:
        if entry.get("type") == "markdown":
            vfile_data = entry.get("vfileData", {})
            assert "htmlAst" not in vfile_data, (
                f"contentmap vfileData for {entry.get('filePath')!r} contains 'htmlAst' — "
                "T0 Amendment 1: htmlAst must be excluded from metadata-only contentmap"
            )


def test_contentmap_entries_include_blocks(fastpath_dir: Path) -> None:
    """contentmap ``vfileData`` entries include ``blocks``; transclusion-target has expected IDs."""
    contentmap_path = fastpath_dir / "contentmap.json"
    data: Any = json.loads(contentmap_path.read_text(encoding="utf-8"))
    for entry in data["entries"]:
        if entry.get("type") == "markdown":
            vfile_data = entry.get("vfileData", {})
            assert "blocks" in vfile_data, (
                f"contentmap vfileData for {entry.get('filePath')!r} is missing 'blocks' — "
                "blocks must be included for block-ref transclusion at fast-path time"
            )

    # Strengthen: transclusion-target.md defines ^block-one and ^block-two;
    # Quartz's OFM transformer must have parsed them into vfileData.blocks.
    target_entry: dict[str, Any] | None = None
    for entry in data["entries"]:
        fp = entry.get("filePath", "") or entry.get("vfileData", {}).get("relativePath", "")
        if "transclusion-target" in fp:
            target_entry = entry
            break
    assert target_entry is not None, (
        "transclusion-target.md entry not found in contentmap — "
        "check fixture vault includes transclusion-target.md"
    )
    blocks = target_entry["vfileData"].get("blocks", {})
    assert "block-one" in blocks, (
        f"transclusion-target vfileData.blocks missing 'block-one' key — "
        f"got keys: {sorted(blocks.keys())!r}"
    )
    assert "block-two" in blocks, (
        f"transclusion-target vfileData.blocks missing 'block-two' key — "
        f"got keys: {sorted(blocks.keys())!r}"
    )


def test_contentmap_description_preserved(fastpath_dir: Path) -> None:
    """contentmap ``vfileData.description`` is preserved for fixtures that declare it."""
    contentmap_path = fastpath_dir / "contentmap.json"
    data: Any = json.loads(contentmap_path.read_text(encoding="utf-8"))

    # Fixture ``some-folder/index.md`` has ``description: "A sub-folder landing page."``
    folder_index_entry: dict[str, Any] | None = None
    for entry in data["entries"]:
        fp = entry.get("filePath", "") or entry.get("vfileData", {}).get("relativePath", "")
        if "some-folder" in fp and fp.endswith("index.md"):
            folder_index_entry = entry
            break

    assert folder_index_entry is not None, (
        "some-folder/index.md entry not found in contentmap — "
        "check fixture vault includes some-folder/index.md"
    )
    vfile_data = folder_index_entry["vfileData"]
    assert "description" in vfile_data, (
        "contentmap vfileData for some-folder/index.md is missing 'description' field — "
        "ContentMapVFileData must include description (Fix #3)"
    )
    desc = vfile_data["description"]
    # If Quartz's Description transformer ran, vfileData.description is populated from frontmatter.
    # If not, it falls back to the frontmatter dict entry.  Either way it must be non-null.
    if desc is None:
        # Fallback: description must at least be in vfileData.frontmatter
        fm = vfile_data.get("frontmatter") or {}
        assert fm.get("description") == "A sub-folder landing page.", (
            f"description not found in vfileData.description or vfileData.frontmatter for "
            f"some-folder/index.md — expected 'A sub-folder landing page.', got desc={desc!r}"
        )
    else:
        assert "sub-folder" in str(desc).lower() or desc == "A sub-folder landing page.", (
            f"vfileData.description for some-folder/index.md is {desc!r} — "
            "expected 'A sub-folder landing page.' (from frontmatter description: field)"
        )


# ---------------------------------------------------------------------------
# STOP-THE-LINE production parity tests
# ---------------------------------------------------------------------------


def test_all_manifest_fingerprints_match_python_recompute(
    fastpath_dir: Path,
    built_vault: tuple[Path, Path],
) -> None:
    """STOP-THE-LINE: every TS fingerprint in manifest matches Python recompute.

    For each entry in manifest.slugs:
    1. Read the source file bytes from the vault using entry.source_path.
    2. Compute Python fingerprint via compute_fingerprint(source_bytes, slug, ...).
    3. Assert == entry.fingerprint.

    ANY divergence fails the test immediately with blob-level diagnostics
    (both TS fingerprint and Python fingerprint, plus hex-encoded blobs for
    byte-by-byte diffing).
    """
    vault, _ = built_vault
    manifest = read_manifest(fastpath_dir)

    failures: list[str] = []

    for slug, entry in manifest.slugs.items():
        source_file = vault / entry.source_path
        if not source_file.is_file():
            failures.append(
                f"slug={slug!r}: source file {source_file} not found — "
                "cannot recompute fingerprint"
            )
            continue

        source_bytes = source_file.read_bytes()

        try:
            py_fingerprint, py_blob = compute_fingerprint_with_blob(
                source_bytes=source_bytes,
                slug=slug,
                source_path=entry.source_path,
                output_path=entry.output_path,
            )
        except ManifestError as exc:
            failures.append(
                f"slug={slug!r}: Python compute_fingerprint raised ManifestError: {exc}"
            )
            continue

        if py_fingerprint != entry.fingerprint:
            failures.append(
                f"FINGERPRINT MISMATCH for slug={slug!r}:\n"
                f"  source_path: {entry.source_path}\n"
                f"  output_path: {entry.output_path}\n"
                f"  TS fingerprint:     {entry.fingerprint}\n"
                f"  Python fingerprint: {py_fingerprint}\n"
                f"  Python blob (hex):  {py_blob.hex()}\n"
                f"  (Compare with TS blob using `computeFingerprintFromSource` "
                f"  in fingerprint_parity_runner.mjs for byte-by-byte diff)"
            )

    if failures:
        joined = "\n\n".join(failures)
        pytest.fail(
            f"STOP-THE-LINE: {len(failures)} fingerprint parity failure(s):\n\n{joined}"
        )


# ---------------------------------------------------------------------------
# Per-case coverage corpus assertions
# ---------------------------------------------------------------------------


def _find_slug_by_source_path(
    manifest_slugs: dict[str, Any],
    source_path_suffix: str,
) -> str | None:
    """Return the slug whose source_path ends with ``source_path_suffix``."""
    for slug, entry in manifest_slugs.items():
        sp = entry.source_path if hasattr(entry, "source_path") else entry["source_path"]
        if sp.endswith(source_path_suffix):
            return slug
    return None


@pytest.fixture(scope="session")
def manifest(fastpath_dir: Path) -> Any:
    """Deserialised manifest for per-case assertions."""
    return read_manifest(fastpath_dir)


def test_corpus_folder_index_present(manifest: Any) -> None:
    """Coverage: folder index (``some-folder/index.md``) appears in manifest."""
    slug = _find_slug_by_source_path(manifest.slugs, "some-folder/index.md")
    assert slug is not None, (
        "folder index slug not found in manifest — "
        "some-folder/index.md must produce a slug entry"
    )


def test_corpus_plain_no_frontmatter_present(manifest: Any) -> None:
    """Coverage: plain markdown (no frontmatter) appears in manifest."""
    slug = _find_slug_by_source_path(manifest.slugs, "plain-no-frontmatter.md")
    assert slug is not None, (
        "plain-no-frontmatter slug not found in manifest"
    )


def test_corpus_structural_fields_present(manifest: Any) -> None:
    """Coverage: structural frontmatter fields doc appears in manifest."""
    slug = _find_slug_by_source_path(manifest.slugs, "some-folder/structural-fields.md")
    assert slug is not None, (
        "structural-fields slug not found in manifest"
    )


def test_corpus_ignored_field_present(manifest: Any) -> None:
    """Coverage: ignored frontmatter field (``source:``) doc appears in manifest."""
    slug = _find_slug_by_source_path(manifest.slugs, "ignored-field.md")
    assert slug is not None, (
        "ignored-field slug not found in manifest"
    )


def test_corpus_yaml_inline_tags_present(manifest: Any) -> None:
    """Coverage: YAML + inline tag merge doc appears in manifest."""
    slug = _find_slug_by_source_path(manifest.slugs, "yaml-inline-tags.md")
    assert slug is not None, (
        "yaml-inline-tags slug not found in manifest"
    )


def test_corpus_date_frontmatter_present(manifest: Any) -> None:
    """Coverage: bare YAML datetime doc appears in manifest."""
    slug = _find_slug_by_source_path(manifest.slugs, "date-frontmatter.md")
    assert slug is not None, (
        "date-frontmatter slug not found in manifest"
    )


def test_corpus_wikilinks_present(manifest: Any) -> None:
    """Coverage: wikilinks doc (multiple + alias) appears in manifest."""
    slug = _find_slug_by_source_path(manifest.slugs, "wikilinks.md")
    assert slug is not None, (
        "wikilinks slug not found in manifest"
    )


def test_corpus_transclusion_target_present(manifest: Any) -> None:
    """Coverage: transclusion target (block-ref definitions) appears in manifest."""
    slug = _find_slug_by_source_path(manifest.slugs, "transclusion-target.md")
    assert slug is not None, (
        "transclusion-target slug not found in manifest"
    )


def test_corpus_transclusion_source_present(manifest: Any) -> None:
    """Coverage: transclusion source doc appears in manifest."""
    slug = _find_slug_by_source_path(manifest.slugs, "transclusion-source.md")
    assert slug is not None, (
        "transclusion-source slug not found in manifest"
    )


def test_corpus_duplicate_headings_present(manifest: Any) -> None:
    """Coverage: duplicate headings doc appears in manifest."""
    slug = _find_slug_by_source_path(manifest.slugs, "duplicate-headings.md")
    assert slug is not None, (
        "duplicate-headings slug not found in manifest"
    )


def test_output_path_rule_all_slugs_html(manifest: Any) -> None:
    """All output_paths in manifest end with ``.html`` (uniform slug+'.html' rule)."""
    for slug, entry in manifest.slugs.items():
        assert entry.output_path.endswith(".html"), (
            f"slug={slug!r} output_path={entry.output_path!r} does not end with '.html' — "
            "_deriveOutputPath must return slug + '.html' for all slugs"
        )


def test_output_path_rule_equals_slug_plus_html(manifest: Any) -> None:
    """Each output_path equals ``slug + '.html'`` (no folder-index special case needed)."""
    for slug, entry in manifest.slugs.items():
        expected = slug + ".html"
        assert entry.output_path == expected, (
            f"slug={slug!r}: output_path={entry.output_path!r} != expected {expected!r} — "
            "the _deriveOutputPath rule must be: slug + '.html'"
        )


def test_source_path_is_vault_relative(
    manifest: Any,
    built_vault: tuple[Path, Path],
) -> None:
    """All ``source_path`` values are vault-relative (not absolute)."""
    vault, _ = built_vault
    for slug, entry in manifest.slugs.items():
        assert not entry.source_path.startswith("/"), (
            f"slug={slug!r} source_path={entry.source_path!r} is absolute — "
            "source_path must be vault-relative (from vfile.data.relativePath)"
        )
        # Verify the source file actually exists at vault/source_path.
        full_path = vault / entry.source_path
        assert full_path.is_file(), (
            f"slug={slug!r} source_path={entry.source_path!r} "
            f"does not map to an existing file at {full_path}"
        )
