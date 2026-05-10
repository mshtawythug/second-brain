"""Static smoke tests for quartz_overrides/quartz/util/fastpath_manifest.ts.

No JS toolchain needed — checks source-shape only (regex / substring
assertions), following the pattern in test_quartz_parser_cache_static.py.

Covered contracts:
- File exists at the expected overlay path.
- Exports ``FINGERPRINT_VERSION`` as a numeric constant equal to 1.
- Exports ``computeFingerprint``, ``writeManifest``, ``readManifest``.
- ``computeFingerprint`` body calls ``createHash("sha256")``.
- ``writeManifest`` body calls ``renameSync`` (atomic write guarantee).
- ``FINGERPRINT_VERSION`` is used in the canonical blob (``_u32be(FINGERPRINT_VERSION)``).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_UTIL = REPO_ROOT / "quartz_overrides" / "quartz" / "util"
MANIFEST_TS = OVERRIDES_UTIL / "fastpath_manifest.ts"


# ---------------------------------------------------------------------------
# Module-level fixture — read file once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def manifest_ts_source() -> str:
    """Read ``fastpath_manifest.ts`` once per module."""
    assert MANIFEST_TS.is_file(), (
        f"missing fastpath_manifest.ts at {MANIFEST_TS} — was T1 implemented?"
    )
    return MANIFEST_TS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File-exists guard (fails fast without the fixture overhead)
# ---------------------------------------------------------------------------


def test_fastpath_manifest_ts_exists() -> None:
    """``fastpath_manifest.ts`` exists at the expected overlay path."""
    assert MANIFEST_TS.is_file(), (
        f"fastpath_manifest.ts not found at {MANIFEST_TS}"
    )


# ---------------------------------------------------------------------------
# FINGERPRINT_VERSION export
# ---------------------------------------------------------------------------


def test_fingerprint_version_is_numeric_export(manifest_ts_source: str) -> None:
    """``FINGERPRINT_VERSION`` is exported as a numeric constant equal to 1."""
    assert re.search(
        r"export const FINGERPRINT_VERSION\s*:\s*number\s*=\s*1",
        manifest_ts_source,
    ), (
        "expected `export const FINGERPRINT_VERSION: number = 1` "
        "in fastpath_manifest.ts"
    )


def test_fingerprint_version_equals_one(manifest_ts_source: str) -> None:
    """``FINGERPRINT_VERSION`` is set to ``1`` (the initial version)."""
    # Explicit value check — a later bump would trip this test so maintainers
    # remember to also bump the Python constant and update memory files.
    match = re.search(r"export const FINGERPRINT_VERSION\s*[=:][^=].*?(\d+)", manifest_ts_source)
    assert match, "could not find FINGERPRINT_VERSION assignment in fastpath_manifest.ts"
    assert match.group(1) == "1", (
        f"FINGERPRINT_VERSION must be 1, got {match.group(1)!r} — "
        "if you bumped the version, update this test intentionally"
    )


# ---------------------------------------------------------------------------
# Public function exports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "export_fragment",
    [
        "export function computeFingerprint(",
        "export function writeManifest(",
        "export function readManifest(",
        "export function computeFingerprintFromSource(",
    ],
)
def test_exported_function_present(manifest_ts_source: str, export_fragment: str) -> None:
    """Each required public function is exported with the expected name."""
    assert export_fragment in manifest_ts_source, (
        f"expected exported function starting with {export_fragment!r} "
        "in fastpath_manifest.ts"
    )


# ---------------------------------------------------------------------------
# sha256 usage inside computeFingerprint
# ---------------------------------------------------------------------------


def _slice_between(source: str, start: str, end: str) -> str:
    s = source.find(start)
    assert s != -1, f"start marker not found: {start!r}"
    e = source.find(end, s + len(start))
    return source[s:] if e == -1 else source[s:e]


def test_compute_fingerprint_uses_sha256(manifest_ts_source: str) -> None:
    """``createHash("sha256")`` is called within the fingerprint computation path."""
    assert 'createHash("sha256")' in manifest_ts_source, (
        'expected `createHash("sha256")` in fastpath_manifest.ts — '
        "fingerprint must use sha256"
    )


# ---------------------------------------------------------------------------
# Atomic write: _atomicWriteJson uses renameSync; writeManifest delegates to it
# ---------------------------------------------------------------------------


def test_atomic_write_json_uses_rename_sync(manifest_ts_source: str) -> None:
    """``_atomicWriteJson`` body calls ``renameSync`` (atomic tmp→final rename)."""
    section = _slice_between(
        manifest_ts_source,
        "export function _atomicWriteJson(",
        "\nexport function writeManifest(",
    )
    assert "renameSync" in section, (
        "expected `renameSync(...)` inside _atomicWriteJson — "
        "atomic writes must use write-tmp + rename strategy"
    )


def test_write_manifest_delegates_to_atomic_write_json(manifest_ts_source: str) -> None:
    """``writeManifest`` delegates to ``_atomicWriteJson`` (DRY helper)."""
    section = _slice_between(
        manifest_ts_source,
        "export function writeManifest(",
        "\nexport function readManifest(",
    )
    assert "_atomicWriteJson" in section, (
        "expected `_atomicWriteJson(...)` inside writeManifest — "
        "writeManifest must delegate to the shared atomic-write helper"
    )


# ---------------------------------------------------------------------------
# FINGERPRINT_VERSION used in blob construction
# ---------------------------------------------------------------------------


def test_fingerprint_version_used_in_blob(manifest_ts_source: str) -> None:
    """``FINGERPRINT_VERSION`` is fed into the canonical blob via ``_u32be``."""
    assert "_u32be(FINGERPRINT_VERSION)" in manifest_ts_source, (
        "expected `_u32be(FINGERPRINT_VERSION)` in fastpath_manifest.ts — "
        "the version must be the first section of the canonical blob"
    )


# ---------------------------------------------------------------------------
# Structural field order constant present and in correct order
# ---------------------------------------------------------------------------


def test_structural_fields_order_present(manifest_ts_source: str) -> None:
    """The ``_STRUCTURAL_FIELDS`` array lists the canonical key order."""
    assert "_STRUCTURAL_FIELDS" in manifest_ts_source, (
        "expected `_STRUCTURAL_FIELDS` array in fastpath_manifest.ts"
    )
    # Spot-check first and last field to pin the canonical order.
    fields_section = _slice_between(
        manifest_ts_source,
        "_STRUCTURAL_FIELDS",
        "] as const",
    )
    assert '"title"' in fields_section, (
        'expected "title" as first structural field in _STRUCTURAL_FIELDS'
    )
    assert '"published"' in fields_section, (
        'expected "published" as last structural field in _STRUCTURAL_FIELDS'
    )


# ---------------------------------------------------------------------------
# Parity runner exists
# ---------------------------------------------------------------------------


PARITY_RUNNER = REPO_ROOT / "tests" / "wiki" / "fixtures" / "fingerprint_parity_runner.mjs"


def test_parity_runner_exists() -> None:
    """``fingerprint_parity_runner.mjs`` exists for the cross-language parity tests."""
    assert PARITY_RUNNER.is_file(), (
        f"parity runner not found at {PARITY_RUNNER} — "
        "tests/wiki/test_fastpath_fingerprint_parity.py needs this file"
    )


def test_parity_runner_has_matching_version(manifest_ts_source: str) -> None:
    """Parity runner ``FINGERPRINT_VERSION`` matches the TS overlay file."""
    runner_source = PARITY_RUNNER.read_text(encoding="utf-8")
    assert "FINGERPRINT_VERSION = 1" in runner_source, (
        "parity runner must declare FINGERPRINT_VERSION = 1 to match the TS overlay"
    )


# ---------------------------------------------------------------------------
# Shape-mirror: TS and MJS declare identical constants
# ---------------------------------------------------------------------------


def _extract_quoted_list(source: str, varname: str) -> list[str]:
    """Extract double-quoted items from ``const <varname> = [...]`` (or `as const`)."""
    m = re.search(
        rf"(?:const\s+)?_?{re.escape(varname)}\s*(?:[^=\[]*=\s*)?\[(.*?)\]",
        source,
        re.DOTALL,
    )
    assert m, f"could not locate array constant {varname!r} in source"
    return re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', m.group(1))


def _extract_set_members(source: str, varname: str) -> set[str]:
    """Extract double-quoted items from ``const <varname> = new Set([...])``."""
    m = re.search(
        rf"(?:const\s+)?_?{re.escape(varname)}\s*=\s*new Set\(\[(.*?)\]\)",
        source,
        re.DOTALL,
    )
    assert m, f"could not locate Set constant {varname!r} in source"
    return set(re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', m.group(1)))


def test_runner_and_ts_declare_identical_constants(manifest_ts_source: str) -> None:
    """TS overlay and MJS parity runner declare identical shape constants.

    If a future change updates one but not the other, this test fails before
    the parity tests even run, providing a clear "which constant drifted" error.
    """
    runner_source = PARITY_RUNNER.read_text(encoding="utf-8")

    # FINGERPRINT_VERSION
    ts_ver_m = re.search(
        r"export const FINGERPRINT_VERSION\s*:\s*number\s*=\s*(\d+)", manifest_ts_source
    )
    mjs_ver_m = re.search(r"const FINGERPRINT_VERSION\s*=\s*(\d+)", runner_source)
    assert ts_ver_m and mjs_ver_m, "FINGERPRINT_VERSION not found in one or both files"
    assert ts_ver_m.group(1) == mjs_ver_m.group(1), (
        f"FINGERPRINT_VERSION mismatch: TS={ts_ver_m.group(1)!r} "
        f"MJS={mjs_ver_m.group(1)!r}"
    )

    # _STRUCTURAL_FIELDS / STRUCTURAL_FIELDS (order matters — list, not set)
    ts_struct = _extract_quoted_list(manifest_ts_source, "STRUCTURAL_FIELDS")
    mjs_struct = _extract_quoted_list(runner_source, "STRUCTURAL_FIELDS")
    assert ts_struct == mjs_struct, (
        f"STRUCTURAL_FIELDS mismatch:\n  TS:  {ts_struct}\n  MJS: {mjs_struct}"
    )

    # _ARRAY_FIELDS / ARRAY_FIELDS
    ts_array = _extract_set_members(manifest_ts_source, "ARRAY_FIELDS")
    mjs_array = _extract_set_members(runner_source, "ARRAY_FIELDS")
    assert ts_array == mjs_array, (
        f"ARRAY_FIELDS mismatch: TS={sorted(ts_array)} MJS={sorted(mjs_array)}"
    )

    # _BOOL_FIELDS / BOOL_FIELDS
    ts_bool = _extract_set_members(manifest_ts_source, "BOOL_FIELDS")
    mjs_bool = _extract_set_members(runner_source, "BOOL_FIELDS")
    assert ts_bool == mjs_bool, (
        f"BOOL_FIELDS mismatch: TS={sorted(ts_bool)} MJS={sorted(mjs_bool)}"
    )

    # _DATE_FIELDS / DATE_FIELDS
    ts_date = _extract_set_members(manifest_ts_source, "DATE_FIELDS")
    mjs_date = _extract_set_members(runner_source, "DATE_FIELDS")
    assert ts_date == mjs_date, (
        f"DATE_FIELDS mismatch: TS={sorted(ts_date)} MJS={sorted(mjs_date)}"
    )

    # _STRIP_ASCII_SET / STRIP_ASCII_SET
    ts_strip = _extract_set_members(manifest_ts_source, "STRIP_ASCII_SET")
    mjs_strip = _extract_set_members(runner_source, "STRIP_ASCII_SET")
    assert ts_strip == mjs_strip, (
        f"STRIP_ASCII_SET mismatch:\n  TS:  {sorted(ts_strip)}\n  MJS: {sorted(mjs_strip)}"
    )


# ---------------------------------------------------------------------------
# Production-path regression guards (HIGH bugs caught in Codex phase review)
# ---------------------------------------------------------------------------


def test_extract_body_helper_exists(manifest_ts_source: str) -> None:
    """``_extractBody`` helper strips YAML frontmatter from raw source markdown.

    Shared by ``computeFingerprint`` (ProcessedContent path) and
    ``computeFingerprintFromSource`` (raw-source path) to guarantee both operate
    on identical body text.
    """
    assert "function _extractBody(" in manifest_ts_source, (
        "expected `function _extractBody(` in fastpath_manifest.ts — "
        "shared helper for stripping frontmatter from raw markdown must exist"
    )


def test_compute_fingerprint_uses_extract_body_not_vfile_data_text(
    manifest_ts_source: str,
) -> None:
    """``computeFingerprint`` must call ``_extractBody``, not read ``vfile.data["text"]``.

    ``vfile.data.text`` (set by ``hast-util-to-string``) is rendered plain text with
    all OFM syntax stripped.  The body must come from ``_extractBody(vfile.value)``.

    We test the positive contract (``_extractBody(`` is called and ``const body``
    is set to its result) rather than a string-exclusion, because the comment block
    legitimately names the forbidden pattern as a warning.
    """
    section = _slice_between(
        manifest_ts_source,
        "export function computeFingerprint(",
        "\nexport function computeFingerprintFromSource(",
    )
    assert "_extractBody(" in section, (
        "computeFingerprint must call _extractBody(fileSource) to derive the markdown body"
    )
    # The old bug was: const body = String((vfile.data as Record<...>)["text"] ?? "")
    # Verify that pattern is gone — the ["text"] index-access form is the red flag.
    assert '["text"]' not in section, (
        'computeFingerprint must not index vfile.data["text"] — '
        "that is rendered plain text; use _extractBody(fileSource) instead"
    )


def test_compute_fingerprint_accepts_paths_parameter(manifest_ts_source: str) -> None:
    """``computeFingerprint`` must accept an explicit ``paths`` parameter.

    The caller (T2 full-build hook) supplies both ``sourcePath`` (vault-relative,
    from ``vfile.data.relativePath``) and ``outputPath`` (which may be
    ``slug/index.html`` for folder-index files, not just ``slug.html``).
    Embedding these as caller-supplied parameters prevents ``filePath`` (full disk
    path) from silently leaking into the fingerprint blob.
    """
    assert re.search(
        r"export function computeFingerprint\s*\(\s*\w+\s*:\s*ProcessedContent\s*,"
        r"\s*paths\s*:",
        manifest_ts_source,
    ), (
        "computeFingerprint must declare a `paths: { sourcePath: string; outputPath: string }` "
        "second parameter — callers supply vault-relative source_path and the correct "
        "output_path (slug.html vs slug/index.html for folder indexes)"
    )


def test_source_path_jsdoc_references_relative_path(manifest_ts_source: str) -> None:
    """``SlugEntry.source_path`` JSDoc must mention ``vfile.data.relativePath``.

    Prevents callers from accidentally supplying ``vfile.data.filePath`` (full
    absolute disk path), which would break the vault-relative contract.
    """
    jsdoc_section = _slice_between(
        manifest_ts_source,
        "export interface SlugEntry",
        "\nexport interface Manifest",
    )
    assert "relativePath" in jsdoc_section, (
        "SlugEntry.source_path JSDoc must reference vfile.data.relativePath "
        "(vault-relative path) — callers must not use vfile.data.filePath (full disk path)"
    )
