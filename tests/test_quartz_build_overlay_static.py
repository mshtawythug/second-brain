"""Static smoke tests for quartz_overrides/quartz/build.ts (T2 overlay).

No JS toolchain required — checks source shape only (substring / regex
assertions), following the pattern established by
``test_quartz_fastpath_manifest_static.py``.

Covered contracts:
- ``quartz_overrides/quartz/build.ts`` exists at the expected path.
- It imports from ``./util/fastpath_manifest`` (T1's manifest writer).
- It declares a ``writeFastpathArtifacts(`` function.
- ``writeFastpathArtifacts`` reads ``QUARTZ_PARENT_BUILD_ID`` env var.
- ``writeFastpathArtifacts`` is called AFTER ``emitContent(`` in the source.
- The call is wrapped in try/catch (artifact failure must NOT crash the build).
- ``_deriveOutputPath(`` helper exists (uniform slug→html rule).
- The contentmap uses ``hastRoot: null`` (metadata-only per T0 Amendment 1).
- Both manifest + contentmap are written atomically (renameSync).
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_TS = REPO_ROOT / "quartz_overrides" / "quartz" / "build.ts"


# ---------------------------------------------------------------------------
# Module-level fixture — read file once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def build_ts_source() -> str:
    """Read ``build.ts`` once per module."""
    assert BUILD_TS.is_file(), (
        f"missing quartz_overrides/quartz/build.ts at {BUILD_TS} — was T2 implemented?"
    )
    return BUILD_TS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File-exists guard
# ---------------------------------------------------------------------------


def test_build_ts_exists() -> None:
    """``quartz_overrides/quartz/build.ts`` exists at the expected overlay path."""
    assert BUILD_TS.is_file(), (
        f"build.ts not found at {BUILD_TS} — T2 overlay not created"
    )


# ---------------------------------------------------------------------------
# Import from fastpath_manifest
# ---------------------------------------------------------------------------


def test_imports_fastpath_manifest(build_ts_source: str) -> None:
    """The overlay imports from ``./util/fastpath_manifest`` (T1's manifest writer)."""
    assert "./util/fastpath_manifest" in build_ts_source, (
        "expected import from './util/fastpath_manifest' in build.ts — "
        "T2 overlay must import T1's manifest writer"
    )


def test_imports_compute_fingerprint(build_ts_source: str) -> None:
    """The overlay imports ``computeFingerprint`` from fastpath_manifest."""
    assert "computeFingerprint" in build_ts_source, (
        "expected `computeFingerprint` imported in build.ts — "
        "overlay must call computeFingerprint for each filteredContent entry"
    )


def test_imports_write_manifest(build_ts_source: str) -> None:
    """The overlay imports ``writeManifest`` from fastpath_manifest."""
    assert "writeManifest" in build_ts_source, (
        "expected `writeManifest` imported in build.ts — "
        "overlay must call writeManifest to write the manifest atomically"
    )


def test_imports_fingerprint_version(build_ts_source: str) -> None:
    """The overlay imports ``FINGERPRINT_VERSION`` from fastpath_manifest."""
    assert "FINGERPRINT_VERSION" in build_ts_source, (
        "expected `FINGERPRINT_VERSION` imported in build.ts — "
        "manifest object must set version = FINGERPRINT_VERSION"
    )


# ---------------------------------------------------------------------------
# writeFastpathArtifacts function
# ---------------------------------------------------------------------------


def test_write_fastpath_artifacts_function_exists(build_ts_source: str) -> None:
    """``writeFastpathArtifacts(`` function is declared in the overlay."""
    assert "writeFastpathArtifacts(" in build_ts_source, (
        "expected `writeFastpathArtifacts(` function declaration in build.ts"
    )


def test_write_fastpath_artifacts_reads_env_var(build_ts_source: str) -> None:
    """``writeFastpathArtifacts`` reads ``QUARTZ_PARENT_BUILD_ID`` from env."""
    assert "QUARTZ_PARENT_BUILD_ID" in build_ts_source, (
        "expected `QUARTZ_PARENT_BUILD_ID` in build.ts — "
        "Strategy A: Python build_swap passes parent_build_id via this env var"
    )


def test_write_fastpath_artifacts_called_after_emit_content(build_ts_source: str) -> None:
    """``writeFastpathArtifacts`` is called AFTER ``emitContent(`` in the source.

    We verify positional ordering: ``emitContent(`` must appear BEFORE
    ``writeFastpathArtifacts(`` in the source text, confirming that the
    artifact write hook fires only after the full build completes.
    """
    emit_pos = build_ts_source.find("await emitContent(ctx, filteredContent)")
    artifacts_pos = build_ts_source.find("await writeFastpathArtifacts(")
    assert emit_pos != -1, (
        "expected `await emitContent(ctx, filteredContent)` in build.ts"
    )
    assert artifacts_pos != -1, (
        "expected `await writeFastpathArtifacts(` call in build.ts"
    )
    assert emit_pos < artifacts_pos, (
        "writeFastpathArtifacts must be called AFTER emitContent in build.ts — "
        f"emitContent at offset {emit_pos}, writeFastpathArtifacts at {artifacts_pos}"
    )


def test_write_fastpath_artifacts_wrapped_in_try_catch(build_ts_source: str) -> None:
    """The ``writeFastpathArtifacts`` call is wrapped in try/catch.

    Artifact write failure must NEVER crash the build — the try/catch guard
    ensures a failing write logs a warning and continues.
    """
    # Locate the try block that wraps writeFastpathArtifacts.
    # We look for the pattern: try { ... writeFastpathArtifacts ... } catch
    try_pos = build_ts_source.find("try {")
    assert try_pos != -1, "expected try { block in build.ts"

    artifacts_pos = build_ts_source.find("writeFastpathArtifacts(", try_pos)
    assert artifacts_pos != -1, (
        "expected writeFastpathArtifacts inside a try { block in build.ts"
    )

    catch_pos = build_ts_source.find("} catch (", artifacts_pos)
    assert catch_pos != -1, (
        "expected } catch ( after writeFastpathArtifacts in build.ts — "
        "artifact write failure must be caught and logged, not propagated"
    )


# ---------------------------------------------------------------------------
# _deriveOutputPath helper
# ---------------------------------------------------------------------------


def test_derive_output_path_helper_exists(build_ts_source: str) -> None:
    """``_deriveOutputPath(`` helper function is declared in the overlay."""
    assert "_deriveOutputPath(" in build_ts_source, (
        "expected `_deriveOutputPath(` helper in build.ts — "
        "uniform slug→html output path rule must be explicit and documented"
    )


def test_derive_output_path_returns_slug_plus_html(build_ts_source: str) -> None:
    """``_deriveOutputPath`` returns ``slug + ".html"`` for all slugs."""
    # Locate the function body and check it returns slug + ".html".
    start = build_ts_source.find("function _deriveOutputPath(")
    assert start != -1, "expected `function _deriveOutputPath(` in build.ts"
    end = build_ts_source.find("\n}", start)
    section = build_ts_source[start : end + 2]
    assert '.html"' in section, (
        'expected slug + ".html" return in _deriveOutputPath body'
    )


# ---------------------------------------------------------------------------
# Metadata-only contentmap (hastRoot: null)
# ---------------------------------------------------------------------------


def test_contentmap_hastroot_is_null(build_ts_source: str) -> None:
    """The contentmap entries set ``hastRoot: null`` (metadata-only per T0 Amendment 1).

    Full HAST trees are 273 MB on a 1100-doc vault. Storing null and re-parsing
    the changed file at fast-path time keeps contentmap.json at ~1.3 MB.
    """
    assert "hastRoot: null" in build_ts_source, (
        "expected `hastRoot: null` in build.ts contentmap entries — "
        "T0 Amendment 1 mandates metadata-only contentmap (null hastRoot)"
    )


# ---------------------------------------------------------------------------
# Atomic write for contentmap (renameSync)
# ---------------------------------------------------------------------------


def test_contentmap_uses_rename_sync(build_ts_source: str) -> None:
    """contentmap.json is written atomically via ``renameSync`` (tmp → final)."""
    assert "renameSync" in build_ts_source, (
        "expected `renameSync` in build.ts — "
        "contentmap.json must be written atomically (write tmp then rename)"
    )


# ---------------------------------------------------------------------------
# Fastpath dir path uses argv.directory (not argv.output)
# ---------------------------------------------------------------------------


def test_fastpath_dir_uses_argv_directory(build_ts_source: str) -> None:
    """Fastpath dir is rooted at ``argv.directory`` (vault root), NOT ``argv.output``.

    Per T0 F5.3: the manifest lives at ``<vault>/.quartz/.cache/fastpath/``,
    not under the ephemeral build output dir. Using argv.output would place
    artifacts inside the build tree (which is deleted before each build).
    """
    # Look for the fastpath dir construction using argv.directory.
    assert "argv.directory" in build_ts_source, (
        "expected `argv.directory` in build.ts fastpath dir construction — "
        "fastpath artifacts must live under <vault>/.quartz/, not <build_dir>/"
    )
    assert '".quartz"' in build_ts_source or ".quartz" in build_ts_source, (
        'expected ".quartz" path component in build.ts — '
        "fastpath dir is <vault>/.quartz/.cache/fastpath/"
    )


# ---------------------------------------------------------------------------
# Upstream functionality preserved — key upstream identifiers present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment",
    [
        "buildQuartz",
        "startWatching",
        "rebuild",
        "emitContent",
        "filterContent",
        "parseMarkdown",
        "randomIdNonSecure",
        "export default",
    ],
)
def test_upstream_identifiers_preserved(build_ts_source: str, fragment: str) -> None:
    """Upstream build.ts functions and exports are preserved in the overlay."""
    assert fragment in build_ts_source, (
        f"expected upstream identifier `{fragment}` in build.ts overlay — "
        "the overlay must preserve all upstream functionality"
    )
