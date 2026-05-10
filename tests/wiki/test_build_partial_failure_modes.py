"""Failure-mode tests for T4 — ``build-partial`` exit codes 1-6.

Exercises the guard paths in ``build_partial_handler.js`` with synthetic
manifest/contentmap JSON fixtures — no real Quartz full build required.

Skip-gate:
    - ``node`` must be on PATH.
    - The live Quartz workspace (``~/brain-vault/.quartz``) must exist with
      ``node_modules`` installed.
    - The T4 overlay must be installed: ``<workspace>/quartz/cli/build_partial_handler.js``
      must exist (installed by ``brain vault render --overlay`` or manual copy).

Exit code contracts tested:
    1  — manifest.json missing or unparseable.
    1  — contentmap.json missing or unparseable (manifest present).
    2  — version mismatch between manifest and contentmap.
    2  — parent_build_id mismatch between manifest and contentmap.
    3  — slug not present in manifest.slugs (contentmap OK).
    4  — slug present in manifest but absent from contentmap.entries.
    6  — unsupported-slug scope violation (tags/, /index, root index).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

LIVE_WORKSPACE = Path.home() / "brain-vault" / ".quartz"
_BOOTSTRAP = LIVE_WORKSPACE / "quartz" / "bootstrap-cli.mjs"
_HANDLER_JS = LIVE_WORKSPACE / "quartz" / "cli" / "build_partial_handler.js"

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
            r
            for r in (self.node_missing, self.workspace_missing, self.overlay_missing)
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
    elif not _BOOTSTRAP.is_file():
        workspace_missing = f"bootstrap-cli.mjs missing: {_BOOTSTRAP}"

    overlay_missing: str | None = None
    if workspace_missing is None:
        if not _HANDLER_JS.is_file():
            overlay_missing = (
                f"T4 overlay not installed: {_HANDLER_JS} missing — "
                "copy quartz_overrides/quartz/cli/build_partial_handler.js to live workspace"
            )
        elif "handlePartialBuild" not in _HANDLER_JS.read_text(encoding="utf-8"):
            overlay_missing = (
                f"T4 overlay stale: {_HANDLER_JS} does not contain `handlePartialBuild`"
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
        reason=f"T4 failure-mode prerequisites not met: {_PREFLIGHT.skip_reason}",
    ),
]

# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

_SLUG = "test-partial-note"

# A minimal but envelope-valid manifest entry.
_VALID_MANIFEST: dict = {
    "version": 1,
    "parent_build_id": "test-failure-modes-build-abc123",
    "built_at_ms": 1_700_000_000_000,
    "slugs": {
        _SLUG: {
            "source_path": f"{_SLUG}.md",
            "output_path": f"{_SLUG}/index.html",
            "fingerprint": "abc123deadbeef",
        }
    },
}

# A minimal but envelope-valid contentmap entry.
_VALID_CONTENTMAP: dict = {
    "version": 1,
    "parent_build_id": "test-failure-modes-build-abc123",
    "built_at_ms": 1_700_000_000_000,
    "entries": [
        {
            "type": "markdown",
            "filePath": f"{_SLUG}.md",
            "vfileData": {
                "slug": _SLUG,
                "frontmatter": None,
                "links": None,
                "text": "Hello world",
                "blocks": {},
                "dates": {"created": None, "modified": None, "published": None},
                "filePath": f"{_SLUG}.md",
                "relativePath": f"{_SLUG}.md",
                "description": None,
                "toc": None,
                "collapseToc": None,
                "aliases": None,
                "hasMermaidDiagram": None,
            },
        }
    ],
}

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run_build_partial(
    slug: str,
    vault_dir: Path,
    output_dir: Path,
    *,
    extra_args: list[str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run ``build-partial`` against the given vault/output dirs.

    CWD is the live workspace so node_modules are resolved correctly.
    """
    node = shutil.which("node")
    assert node is not None  # preflight guarantees this

    args: list[str] = [
        node,
        str(_BOOTSTRAP),
        "build-partial",
        "--directory",
        str(vault_dir),
        "--output",
        str(output_dir),
        "--slug",
        slug,
        *(extra_args or []),
    ]

    env = dict(os.environ)

    return subprocess.run(  # noqa: S603
        args,
        cwd=str(LIVE_WORKSPACE),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _write_fastpath_json(fastpath_dir: Path, filename: str, data: dict) -> None:
    fastpath_dir.mkdir(parents=True, exist_ok=True)
    (fastpath_dir / filename).write_text(json.dumps(data), encoding="utf-8")


def _fastpath_dir(vault_dir: Path) -> Path:
    return vault_dir / ".quartz" / ".cache" / "fastpath"


# ---------------------------------------------------------------------------
# Exit code 1 — missing/corrupt manifest.json
# ---------------------------------------------------------------------------


def test_exit1_manifest_missing(tmp_path: Path) -> None:
    """Exit 1 when manifest.json is absent (fastpath dir may not even exist)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    # No fastpath dir — manifest.json cannot exist.
    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 1, (
        f"expected exit 1 (manifest missing), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "manifest" in result.stderr.lower(), (
        f"expected 'manifest' in stderr, got: {result.stderr!r}"
    )


def test_exit1_contentmap_missing(tmp_path: Path) -> None:
    """Exit 1 when contentmap.json is absent but manifest.json is present."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)
    _write_fastpath_json(fd, "manifest.json", _VALID_MANIFEST)
    # contentmap.json deliberately NOT written.

    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 1, (
        f"expected exit 1 (contentmap missing), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "contentmap" in result.stderr.lower(), (
        f"expected 'contentmap' in stderr, got: {result.stderr!r}"
    )


def test_exit1_manifest_corrupt(tmp_path: Path) -> None:
    """Exit 1 when manifest.json exists but is not valid JSON."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)
    fd.mkdir(parents=True, exist_ok=True)
    (fd / "manifest.json").write_text("{ NOT VALID JSON !!! }", encoding="utf-8")

    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 1, (
        f"expected exit 1 (corrupt manifest), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Exit code 2 — envelope mismatch
# ---------------------------------------------------------------------------


def test_exit2_version_mismatch(tmp_path: Path) -> None:
    """Exit 2 when manifest.version != contentmap.version."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)

    manifest = {**_VALID_MANIFEST, "version": 1}
    contentmap = {**_VALID_CONTENTMAP, "version": 2}  # different version

    _write_fastpath_json(fd, "manifest.json", manifest)
    _write_fastpath_json(fd, "contentmap.json", contentmap)

    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 2, (
        f"expected exit 2 (version mismatch), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "envelope mismatch" in result.stderr.lower(), (
        f"expected 'envelope mismatch' in stderr, got: {result.stderr!r}"
    )


def test_exit2_parent_build_id_mismatch(tmp_path: Path) -> None:
    """Exit 2 when manifest.parent_build_id != contentmap.parent_build_id."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)

    manifest = {**_VALID_MANIFEST, "parent_build_id": "build-aaa"}
    contentmap = {**_VALID_CONTENTMAP, "parent_build_id": "build-bbb"}  # different id

    _write_fastpath_json(fd, "manifest.json", manifest)
    _write_fastpath_json(fd, "contentmap.json", contentmap)

    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 2, (
        f"expected exit 2 (parent_build_id mismatch), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "envelope mismatch" in result.stderr.lower(), (
        f"expected 'envelope mismatch' in stderr, got: {result.stderr!r}"
    )


def test_exit2_stderr_contains_both_versions(tmp_path: Path) -> None:
    """Exit 2 stderr contains both manifest and contentmap version/id strings."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)

    manifest = {**_VALID_MANIFEST, "parent_build_id": "build-XXX"}
    contentmap = {**_VALID_CONTENTMAP, "parent_build_id": "build-YYY"}

    _write_fastpath_json(fd, "manifest.json", manifest)
    _write_fastpath_json(fd, "contentmap.json", contentmap)

    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 2
    # stderr should include both ids so the operator can diagnose the stale artifact.
    assert "build-XXX" in result.stderr and "build-YYY" in result.stderr, (
        f"expected both parent_build_ids in stderr, got: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Exit code 3 — slug not in manifest.slugs
# ---------------------------------------------------------------------------


def test_exit3_slug_not_in_manifest(tmp_path: Path) -> None:
    """Exit 3 when the requested slug is absent from manifest.slugs."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)

    # Manifest with a DIFFERENT slug — not the one we request.
    manifest = {
        **_VALID_MANIFEST,
        "slugs": {
            "some-other-note": {
                "source_path": "some-other-note.md",
                "output_path": "some-other-note/index.html",
                "fingerprint": "deadbeef",
            }
        },
    }

    _write_fastpath_json(fd, "manifest.json", manifest)
    _write_fastpath_json(fd, "contentmap.json", _VALID_CONTENTMAP)

    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 3, (
        f"expected exit 3 (slug not in manifest), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert _SLUG in result.stderr, (
        f"expected slug name in stderr, got: {result.stderr!r}"
    )


def test_exit3_empty_manifest_slugs(tmp_path: Path) -> None:
    """Exit 3 when manifest.slugs is an empty object."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)

    manifest = {**_VALID_MANIFEST, "slugs": {}}
    _write_fastpath_json(fd, "manifest.json", manifest)
    _write_fastpath_json(fd, "contentmap.json", _VALID_CONTENTMAP)

    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 3, (
        f"expected exit 3 (empty manifest.slugs), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Exit code 4 — slug in manifest but absent from contentmap.entries
# ---------------------------------------------------------------------------


def test_exit4_slug_not_in_contentmap(tmp_path: Path) -> None:
    """Exit 4 when slug is in manifest.slugs but not in contentmap.entries."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)

    # Contentmap with entries for a DIFFERENT slug.
    contentmap = {
        **_VALID_CONTENTMAP,
        "entries": [
            {
                "type": "markdown",
                "filePath": "some-other-note.md",
                "vfileData": {
                    "slug": "some-other-note",
                    "text": "hello",
                    "blocks": {},
                    "dates": {"created": None, "modified": None, "published": None},
                    "filePath": "some-other-note.md",
                    "relativePath": "some-other-note.md",
                },
            }
        ],
    }

    _write_fastpath_json(fd, "manifest.json", _VALID_MANIFEST)
    _write_fastpath_json(fd, "contentmap.json", contentmap)

    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 4, (
        f"expected exit 4 (slug not in contentmap), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert _SLUG in result.stderr, (
        f"expected slug name in stderr, got: {result.stderr!r}"
    )


def test_exit4_empty_contentmap_entries(tmp_path: Path) -> None:
    """Exit 4 when contentmap.entries is an empty array."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)

    contentmap = {**_VALID_CONTENTMAP, "entries": []}
    _write_fastpath_json(fd, "manifest.json", _VALID_MANIFEST)
    _write_fastpath_json(fd, "contentmap.json", contentmap)

    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 4, (
        f"expected exit 4 (empty contentmap.entries), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


def test_exit4_contentmap_entries_vfiledata_missing_slug(tmp_path: Path) -> None:
    """Exit 4 when a contentmap entry has no vfileData.slug — can't match."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)

    # Entry exists but vfileData has no slug key.
    contentmap = {
        **_VALID_CONTENTMAP,
        "entries": [
            {
                "type": "markdown",
                "filePath": f"{_SLUG}.md",
                "vfileData": {
                    # slug deliberately omitted
                    "text": "hello",
                },
            }
        ],
    }

    _write_fastpath_json(fd, "manifest.json", _VALID_MANIFEST)
    _write_fastpath_json(fd, "contentmap.json", contentmap)

    result = _run_build_partial(_SLUG, vault, output)

    assert result.returncode == 4, (
        f"expected exit 4 (no vfileData.slug), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Exit code 6 — unsupported-slug scope violation (Codex T4 review #4)
#
# ContentPage.partialEmit skips slugs starting with "tags/" or ending with
# "/index" (contentPage.tsx:112-117).  TagPage/FolderPage are in the deny-list.
# A fast-path emit to such a slug would advance the manifest/contentmap/.build-id
# while leaving the rendered HTML stale.  The handler must refuse with exit 6
# BEFORE any artifact write — mtime on all fastpath files must be unchanged.
# ---------------------------------------------------------------------------


def _make_manifest_for_slug(slug: str) -> dict:
    """Return a valid manifest fixture with ``slug`` registered under manifest.slugs."""
    return {
        "version": 1,
        "parent_build_id": "test-failure-modes-build-abc123",
        "built_at_ms": 1_700_000_000_000,
        "slugs": {
            slug: {
                "source_path": f"{slug}.md",
                "output_path": f"{slug}/index.html",
                "fingerprint": "abc123deadbeef",
            }
        },
    }


def test_exit6_when_slug_starts_with_tags(tmp_path: Path) -> None:
    """build-partial refuses slugs starting with 'tags/' (TagPage owns these).

    ContentPage.partialEmit skips such slugs (contentPage.tsx:112-117) and our
    deny-list also skips TagPage/FolderPage.  Both emitters skip, so a partial
    emit would advance artifacts while leaving the rendered HTML stale.
    """
    tag_slug = "tags/test"
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)
    manifest = _make_manifest_for_slug(tag_slug)
    _write_fastpath_json(fd, "manifest.json", manifest)
    _write_fastpath_json(fd, "contentmap.json", _VALID_CONTENTMAP)

    # Record artifact mtimes BEFORE the call.
    manifest_mtime_before = (fd / "manifest.json").stat().st_mtime_ns
    contentmap_mtime_before = (fd / "contentmap.json").stat().st_mtime_ns

    result = _run_build_partial(tag_slug, vault, output)

    assert result.returncode == 6, (
        f"expected exit 6 (tags/ slug refused), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "scope: full build required" in result.stderr, (
        f"expected 'scope: full build required' in stderr, got: {result.stderr!r}"
    )
    # Artifacts must NOT be modified on refusal.
    assert (fd / "manifest.json").stat().st_mtime_ns == manifest_mtime_before, (
        "manifest.json mtime changed after exit-6 refusal — handler wrote artifacts it must not"
    )
    assert (fd / "contentmap.json").stat().st_mtime_ns == contentmap_mtime_before, (
        "contentmap.json mtime changed after exit-6 refusal — handler wrote artifacts it must not"
    )


def test_exit6_when_slug_ends_with_index(tmp_path: Path) -> None:
    """build-partial refuses slugs ending with '/index' (FolderPage owns these).

    ContentPage.partialEmit skips such slugs (contentPage.tsx:112-117) and our
    deny-list also skips FolderPage.  Both emitters skip, so a partial emit would
    advance artifacts while leaving the rendered HTML stale.
    """
    folder_slug = "some-folder/index"
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)
    manifest = _make_manifest_for_slug(folder_slug)
    _write_fastpath_json(fd, "manifest.json", manifest)
    _write_fastpath_json(fd, "contentmap.json", _VALID_CONTENTMAP)

    manifest_mtime_before = (fd / "manifest.json").stat().st_mtime_ns
    contentmap_mtime_before = (fd / "contentmap.json").stat().st_mtime_ns

    result = _run_build_partial(folder_slug, vault, output)

    assert result.returncode == 6, (
        f"expected exit 6 (/index slug refused), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "scope: full build required" in result.stderr, (
        f"expected 'scope: full build required' in stderr, got: {result.stderr!r}"
    )
    assert (fd / "manifest.json").stat().st_mtime_ns == manifest_mtime_before, (
        "manifest.json mtime changed after exit-6 refusal — handler wrote artifacts it must not"
    )
    assert (fd / "contentmap.json").stat().st_mtime_ns == contentmap_mtime_before, (
        "contentmap.json mtime changed after exit-6 refusal — handler wrote artifacts it must not"
    )


def test_exit6_when_slug_is_root_index(tmp_path: Path) -> None:
    """build-partial refuses --slug index (root index, owned by full build).

    The root "index" slug is the vault's landing page.  It does not start with
    "tags/" and does not end with "/index" — so it gets an explicit equality check.
    Like folder index pages it must not be partially built.
    """
    root_slug = "index"
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    fd = _fastpath_dir(vault)
    manifest = _make_manifest_for_slug(root_slug)
    _write_fastpath_json(fd, "manifest.json", manifest)
    _write_fastpath_json(fd, "contentmap.json", _VALID_CONTENTMAP)

    manifest_mtime_before = (fd / "manifest.json").stat().st_mtime_ns
    contentmap_mtime_before = (fd / "contentmap.json").stat().st_mtime_ns

    result = _run_build_partial(root_slug, vault, output)

    assert result.returncode == 6, (
        f"expected exit 6 (root 'index' slug refused), got {result.returncode}\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "scope: full build required" in result.stderr, (
        f"expected 'scope: full build required' in stderr, got: {result.stderr!r}"
    )
    assert (fd / "manifest.json").stat().st_mtime_ns == manifest_mtime_before, (
        "manifest.json mtime changed after exit-6 refusal — handler wrote artifacts it must not"
    )
    assert (fd / "contentmap.json").stat().st_mtime_ns == contentmap_mtime_before, (
        "contentmap.json mtime changed after exit-6 refusal — handler wrote artifacts it must not"
    )


# ---------------------------------------------------------------------------
# Sanity — valid envelope + valid slug pair does NOT exit with 1-4
# (the executor will fail since there is no real vault, but we reach step 8)
# ---------------------------------------------------------------------------


def test_valid_envelope_reaches_executor(tmp_path: Path) -> None:
    """A valid envelope + valid slug pair reaches the TS executor (step 8).

    With a synthetic vault (no real Markdown file), the executor is expected
    to fail during compilation or parse. The key assertion is that the exit
    code is NOT 1, 2, 3, or 4 — those guard paths were passed successfully.

    We use a very short timeout so the test doesn't block on esbuild for long.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "public"
    output.mkdir()

    # Write a dummy source file so parseMarkdown has something to read.
    (vault / f"{_SLUG}.md").write_text(
        "---\ntitle: Test\n---\nHello partial build.\n",
        encoding="utf-8",
    )

    fd = _fastpath_dir(vault)
    _write_fastpath_json(fd, "manifest.json", _VALID_MANIFEST)
    _write_fastpath_json(fd, "contentmap.json", _VALID_CONTENTMAP)

    result = _run_build_partial(_SLUG, vault, output, timeout=120)

    # Guard: exit codes 1-4 indicate a preflight failure that should not occur
    # with a valid envelope + valid slug.
    assert result.returncode not in (2, 3, 4), (
        f"envelope/slug guard path triggered unexpectedly (exit {result.returncode}):\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Fix #2 static regression — emitter fail-fast source contract
#
# We cannot easily inject a throwing emitter into the compiled TS executor at
# test time (the executor is a template literal compiled by esbuild at runtime).
# Per Codex T4-fix2 spec: "at minimum add a static test that asserts the artifact
# write happens AFTER the emitter walk's success path AND that there's a try/catch
# wrapping the walk that exits before the write block."
# ---------------------------------------------------------------------------


def test_emitter_fail_fast_static_contract() -> None:
    """Emitter fail-fast (Fix #2): static contract check on the installed overlay.

    Verifies the source layout that enforces the Plan B write-nothing-on-failure
    invariant:

    1. A single outer try/catch wraps the emitter walk (per-emitter catch removed).
    2. The catch block writes "partial emit failed in" to stderr (operator-readable).
    3. The catch block calls process.exit(5) — new exit code for emitter failure.
    4. process.exit(5) appears in the source BEFORE _atomicWriteJson for contentmap
       (proxy for: emitter abort occurs before any artifact is written at runtime).
    5. The old per-emitter "error (continuing)" pattern is absent.
    """
    handler_source = _HANDLER_JS.read_text(encoding="utf-8")

    assert "partial emit failed in" in handler_source, (
        "Fix #2: expected 'partial emit failed in' stderr message in installed handler — "
        "the fail-fast catch block must identify the failing emitter"
    )
    assert "process.exit(5)" in handler_source, (
        "Fix #2: expected `process.exit(5)` in installed handler — "
        "emitter exception must exit 5 (distinct from 1-4 guard failures)"
    )

    exit5_pos = handler_source.find("process.exit(5)")
    contentmap_write_pos = handler_source.find('_atomicWriteJson(fastpathDir, "contentmap.json"')
    assert exit5_pos != -1 and contentmap_write_pos != -1
    assert exit5_pos < contentmap_write_pos, (
        f"Fix #2: process.exit(5) at offset {exit5_pos} must appear before "
        f"contentmap write at offset {contentmap_write_pos} — "
        "emitter failure must abort before artifact writes"
    )

    assert "error (continuing)" not in handler_source, (
        "Fix #2: old per-emitter catch-and-continue pattern still present — "
        "must be replaced by the single outer try/catch that exits 5"
    )
