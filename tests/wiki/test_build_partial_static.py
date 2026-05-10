"""Static guards for T4 — ``build-partial`` CLI overlay files.

No JS toolchain required: all checks are substring / regex assertions on the
source text of the four overlay files + the Explorer.tsx Amendment #8 fix.
Follows the pattern established by ``test_quartz_build_overlay_static.py``
and ``test_quartz_fastpath_manifest_static.py``.

Covered contracts:
- All four overlay files exist at the expected paths.
- Explorer.tsx overlay exists and removes the numExplorers counter.
- handlers.js exports ``partialBuildContent``.
- args.js declares the ``build-partial`` subcommand with ``--slug`` flag.
- bootstrap-cli.mjs wires ``build-partial`` → ``partialBuildContent``.
- build_partial_handler.js imports from ``../util/fastpath_manifest`` (T1+T2 helper).
- build_partial_handler.js explicitly references ``_atomicWriteJson`` (shared helper).
- build_partial_handler.js does NOT call ``ContentIndex.emit`` (Option C exclusion).
- build_partial_handler.js contains both ``manifest.version`` AND
  ``manifest.parent_build_id`` comparisons against contentmap counterparts
  (Codex envelope check contract).
- Explorer.tsx does NOT contain ``numExplorers++`` (counter removed).
- Explorer.tsx uses a slug-derived id (``explorer-${slugId}`` pattern).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CLI_OVERLAY = REPO_ROOT / "quartz_overrides" / "quartz" / "cli"
QUARTZ_OVERLAY = REPO_ROOT / "quartz_overrides" / "quartz"

HANDLERS_JS = CLI_OVERLAY / "handlers.js"
ARGS_JS = CLI_OVERLAY / "args.js"
BOOTSTRAP_MJS = QUARTZ_OVERLAY / "bootstrap-cli.mjs"
BUILD_PARTIAL_HANDLER_JS = CLI_OVERLAY / "build_partial_handler.js"
EXPLORER_TSX = QUARTZ_OVERLAY / "components" / "Explorer.tsx"


# ---------------------------------------------------------------------------
# Module-level fixtures — read files once per module
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def handlers_source() -> str:
    assert HANDLERS_JS.is_file(), (
        f"missing quartz_overrides/quartz/cli/handlers.js at {HANDLERS_JS}"
        " — was T4 implemented?"
    )
    return HANDLERS_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def args_source() -> str:
    assert ARGS_JS.is_file(), (
        f"missing quartz_overrides/quartz/cli/args.js at {ARGS_JS}"
        " — was T4 implemented?"
    )
    return ARGS_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bootstrap_source() -> str:
    assert BOOTSTRAP_MJS.is_file(), (
        f"missing quartz_overrides/quartz/bootstrap-cli.mjs at {BOOTSTRAP_MJS}"
        " — was T4 implemented?"
    )
    return BOOTSTRAP_MJS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def handler_source() -> str:
    assert BUILD_PARTIAL_HANDLER_JS.is_file(), (
        f"missing quartz_overrides/quartz/cli/build_partial_handler.js"
        f" at {BUILD_PARTIAL_HANDLER_JS} — was T4 implemented?"
    )
    return BUILD_PARTIAL_HANDLER_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def explorer_source() -> str:
    assert EXPLORER_TSX.is_file(), (
        f"missing quartz_overrides/quartz/components/Explorer.tsx at {EXPLORER_TSX}"
        " — was T4 Amendment #8 implemented?"
    )
    return EXPLORER_TSX.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File-exists guards (fail fast without fixture overhead)
# ---------------------------------------------------------------------------


def test_handlers_js_exists() -> None:
    """``handlers.js`` overlay exists at the expected path."""
    assert HANDLERS_JS.is_file(), (
        f"handlers.js not found at {HANDLERS_JS} — T4 overlay not created"
    )


def test_args_js_exists() -> None:
    """``args.js`` overlay exists at the expected path."""
    assert ARGS_JS.is_file(), (
        f"args.js not found at {ARGS_JS} — T4 overlay not created"
    )


def test_bootstrap_cli_mjs_exists() -> None:
    """``bootstrap-cli.mjs`` overlay exists at the expected path."""
    assert BOOTSTRAP_MJS.is_file(), (
        f"bootstrap-cli.mjs not found at {BOOTSTRAP_MJS} — T4 overlay not created"
    )


def test_build_partial_handler_js_exists() -> None:
    """``build_partial_handler.js`` exists at the expected path."""
    assert BUILD_PARTIAL_HANDLER_JS.is_file(), (
        f"build_partial_handler.js not found at {BUILD_PARTIAL_HANDLER_JS}"
        " — T4 overlay not created"
    )


def test_explorer_tsx_overlay_exists() -> None:
    """``Explorer.tsx`` overlay exists (Amendment #8 — slug-derived deterministic ID)."""
    assert EXPLORER_TSX.is_file(), (
        f"Explorer.tsx not found at {EXPLORER_TSX}"
        " — T4 Amendment #8 overlay not created"
    )


# ---------------------------------------------------------------------------
# handlers.js: exports partialBuildContent
# ---------------------------------------------------------------------------


def test_handlers_exports_partial_build_content(handlers_source: str) -> None:
    """``handlers.js`` exports ``partialBuildContent``."""
    assert "partialBuildContent" in handlers_source, (
        "expected `partialBuildContent` exported from handlers.js — "
        "bootstrap-cli.mjs imports this symbol to wire the build-partial command"
    )


def test_handlers_imports_from_build_partial_handler(handlers_source: str) -> None:
    """``handlers.js`` imports ``handlePartialBuild`` from ``build_partial_handler.js``."""
    assert "build_partial_handler" in handlers_source, (
        "expected `build_partial_handler` reference in handlers.js — "
        "single-responsibility: full machinery lives in build_partial_handler.js"
    )


# ---------------------------------------------------------------------------
# args.js: declares build-partial subcommand + --slug flag
# ---------------------------------------------------------------------------


def test_args_declares_build_partial_argv(args_source: str) -> None:
    """``args.js`` declares ``BuildPartialArgv`` for the build-partial subcommand."""
    assert "BuildPartialArgv" in args_source, (
        "expected `BuildPartialArgv` export in args.js — "
        "yargs argument spec for the build-partial subcommand is missing"
    )


def test_args_has_build_partial_string(args_source: str) -> None:
    """``args.js`` references ``build-partial`` (as comment or string)."""
    assert "build-partial" in args_source, (
        "expected 'build-partial' reference in args.js — "
        "the subcommand name should appear in the argument spec file"
    )


def test_args_has_slug_flag(args_source: str) -> None:
    """``args.js`` defines a ``slug`` argument."""
    assert "slug" in args_source, (
        "expected `slug` argument definition in args.js — "
        "--slug <string> is the required flag for build-partial"
    )


def test_args_slug_is_required(args_source: str) -> None:
    """``args.js`` marks the ``slug`` argument as required (``demandOption``)."""
    assert "demandOption" in args_source, (
        "expected `demandOption` in args.js slug definition — "
        "slug is required for the build-partial subcommand"
    )


# ---------------------------------------------------------------------------
# bootstrap-cli.mjs: wires build-partial → partialBuildContent
# ---------------------------------------------------------------------------


def test_bootstrap_imports_partial_build_content(bootstrap_source: str) -> None:
    """``bootstrap-cli.mjs`` imports ``partialBuildContent`` from handlers."""
    assert "partialBuildContent" in bootstrap_source, (
        "expected `partialBuildContent` import in bootstrap-cli.mjs — "
        "the handler must be wired to the CLI entry point"
    )


def test_bootstrap_imports_build_partial_argv(bootstrap_source: str) -> None:
    """``bootstrap-cli.mjs`` imports ``BuildPartialArgv`` from args."""
    assert "BuildPartialArgv" in bootstrap_source, (
        "expected `BuildPartialArgv` import in bootstrap-cli.mjs — "
        "the arg spec must be wired to the build-partial yargs command"
    )


def test_bootstrap_declares_build_partial_command(bootstrap_source: str) -> None:
    """``bootstrap-cli.mjs`` declares the ``build-partial`` command."""
    assert '"build-partial"' in bootstrap_source or "'build-partial'" in bootstrap_source, (
        "expected 'build-partial' command string in bootstrap-cli.mjs — "
        "the subcommand must be registered with yargs"
    )


def test_bootstrap_calls_partial_build_content(bootstrap_source: str) -> None:
    """``bootstrap-cli.mjs`` calls ``partialBuildContent`` in the command handler."""
    assert "partialBuildContent(argv)" in bootstrap_source, (
        "expected `partialBuildContent(argv)` call in bootstrap-cli.mjs — "
        "the command handler must invoke the handler function"
    )


# ---------------------------------------------------------------------------
# build_partial_handler.js: imports + references + exclusion + envelope check
# ---------------------------------------------------------------------------


def test_handler_imports_fastpath_manifest(handler_source: str) -> None:
    """``build_partial_handler.js`` references ``../util/fastpath_manifest`` (T1+T2 helper).

    The TypeScript executor source embedded in ``_PARTIAL_BUILD_TS`` imports from
    this path, so the string appears in the file even before esbuild compilation.
    """
    assert "../util/fastpath_manifest" in handler_source, (
        "expected `../util/fastpath_manifest` reference in build_partial_handler.js — "
        "the TypeScript executor must import from the T1+T2 helper module"
    )


def test_handler_references_atomic_write_json(handler_source: str) -> None:
    """``build_partial_handler.js`` explicitly defines or references ``_atomicWriteJson``.

    The function is re-implemented here in plain JS (mirrors util/fastpath_manifest.ts)
    for the artifact writes (contentmap.json, manifest.json) performed by this handler.
    """
    assert "_atomicWriteJson" in handler_source, (
        "expected `_atomicWriteJson` in build_partial_handler.js — "
        "atomic JSON write (mirrors util/fastpath_manifest.ts) must be explicit here"
    )


def test_handler_does_not_call_content_index_emit(handler_source: str) -> None:
    """``build_partial_handler.js`` does NOT call ``ContentIndex.emit``.

    Option C exclusion (T0 benchmark M2): ContentIndex is full-build-only.
    The emitter walk skips ContentIndex by name; no .emit call is made.

    Note: The handler's doc-comment explains the exclusion and may mention
    the phrase "ContentIndex.emit" in prose.  We check for the callable
    invocation form ``ContentIndex.emit(`` (with open paren) which can only
    appear as an actual JS function call.
    """
    assert "ContentIndex.emit(" not in handler_source, (
        "found `ContentIndex.emit(` call in build_partial_handler.js — "
        "Option C: ContentIndex is full-build-only and must NOT be called in partial builds"
    )


def test_handler_uses_deny_list_set_for_exclusions(handler_source: str) -> None:
    """``build_partial_handler.js`` uses a ``_PARTIAL_EMIT_EXCLUDED`` Set deny-list.

    The single-emitter ``if (emitter.name === "ContentIndex")`` check was replaced
    with a deny-list Set to support multiple exclusions cleanly.  The constant name
    ``_PARTIAL_EMIT_EXCLUDED`` and a ``has()`` lookup must be present.
    """
    assert "_PARTIAL_EMIT_EXCLUDED" in handler_source, (
        "expected `_PARTIAL_EMIT_EXCLUDED` Set constant in build_partial_handler.js — "
        "Option C deny-list must be a named Set, not individual equality checks"
    )
    assert "_PARTIAL_EMIT_EXCLUDED.has(emitter.name)" in handler_source, (
        "expected `_PARTIAL_EMIT_EXCLUDED.has(emitter.name)` in emitter walk — "
        "deny-list lookup must use Set.has() so new exclusions only update the Set"
    )


def test_handler_deny_list_contains_all_three_exclusions(handler_source: str) -> None:
    """``_PARTIAL_EMIT_EXCLUDED`` contains ContentIndex, TagPage, and FolderPage.

    All three emitters dereference c[0] (HAST AST) for unchanged corpus entries:
    - ContentIndex: full-build search index (Option C / T0 M2 decision)
    - TagPage:      computeTagInfo at tagPage.tsx:48-55 reads unchanged custom
                    tags/<tag>.md tree; TagContent.tsx:111 crashes on null.children.length
    - FolderPage:   computeFolderInfo at folderPage.tsx:81-85 same pattern;
                    FolderContent.tsx:99 crashes on null.children.length
    """
    assert '"ContentIndex"' in handler_source, (
        "expected '\"ContentIndex\"' in _PARTIAL_EMIT_EXCLUDED deny-list"
    )
    assert '"TagPage"' in handler_source, (
        "expected '\"TagPage\"' in _PARTIAL_EMIT_EXCLUDED deny-list"
    )
    assert '"FolderPage"' in handler_source, (
        "expected '\"FolderPage\"' in _PARTIAL_EMIT_EXCLUDED deny-list"
    )


def test_handler_skips_tagpage_folderpage_in_emitter_walk(handler_source: str) -> None:
    """TagPage and FolderPage MUST be skipped — they dereference c[0].children.

    Per Codex's third T4 phase review (file:line citations in build_partial_handler.js
    contract comment): tagPage.tsx:48-55 + folderPage.tsx:81-85 dereference unchanged
    entries' tree from c[0]. Synthesized unchanged entries have AST=null,
    so TagContent.tsx:111 / FolderContent.tsx:99 would crash on null.children.length.
    """
    assert '"TagPage"' in handler_source, "expected 'TagPage' in exclusion deny-list"
    assert '"FolderPage"' in handler_source, "expected 'FolderPage' in exclusion deny-list"
    assert "_PARTIAL_EMIT_EXCLUDED" in handler_source, (
        "expected _PARTIAL_EMIT_EXCLUDED Set constant for emitter exclusion deny-list"
    )


def test_handler_has_envelope_check_version(handler_source: str) -> None:
    """``build_partial_handler.js`` compares ``manifest.version`` (envelope check)."""
    assert "manifest.version" in handler_source, (
        "expected `manifest.version` comparison in build_partial_handler.js — "
        "Codex pre-flight contract: both version and parent_build_id must be cross-checked"
    )


def test_handler_has_envelope_check_parent_build_id(handler_source: str) -> None:
    """``build_partial_handler.js`` compares ``manifest.parent_build_id`` (envelope check)."""
    assert "manifest.parent_build_id" in handler_source, (
        "expected `manifest.parent_build_id` comparison in build_partial_handler.js — "
        "Codex pre-flight contract: parent_build_id cross-check is non-negotiable"
    )


def test_handler_checks_contentmap_version(handler_source: str) -> None:
    """``build_partial_handler.js`` compares ``contentmap.version`` (envelope check)."""
    assert "contentmap.version" in handler_source, (
        "expected `contentmap.version` in envelope check — "
        "the mismatch message must include the contentmap version"
    )


def test_handler_checks_contentmap_parent_build_id(handler_source: str) -> None:
    """``build_partial_handler.js`` compares ``contentmap.parent_build_id`` (envelope check)."""
    assert "contentmap.parent_build_id" in handler_source, (
        "expected `contentmap.parent_build_id` in envelope check — "
        "the mismatch message must include the contentmap parent_build_id"
    )


def test_handler_has_correct_exit_codes(handler_source: str) -> None:
    """``build_partial_handler.js`` exits with the correct codes (1-6)."""
    assert "process.exit(1)" in handler_source, "expected exit(1) for manifest missing"
    assert "process.exit(2)" in handler_source, "expected exit(2) for envelope mismatch"
    assert "process.exit(3)" in handler_source, "expected exit(3) for slug not in manifest"
    assert "process.exit(4)" in handler_source, "expected exit(4) for slug not in contentmap"
    assert "process.exit(5)" in handler_source, (
        "expected exit(5) for emitter failure (Fix #2 fail-fast) — "
        "partial emit must abort before artifact writes on any emitter exception"
    )
    assert "process.exit(6)" in handler_source, (
        "expected exit(6) for unsupported-slug scope violation (Codex T4 review #4) — "
        "tag/folder/index slugs must be refused before the executor runs"
    )


def test_handler_write_order_contentmap_before_manifest(handler_source: str) -> None:
    """Contentmap is written BEFORE manifest (Codex write-order contract).

    manifest.json acts as the commit marker; it is written LAST so T3/T4 can
    rely on the invariant: if manifest.json exists, contentmap.json is present
    and consistent.
    """
    cm_pos = handler_source.find('_atomicWriteJson(fastpathDir, "contentmap.json"')
    mf_pos = handler_source.find('_atomicWriteJson(fastpathDir, "manifest.json"')
    assert cm_pos != -1, (
        'expected `_atomicWriteJson(fastpathDir, "contentmap.json"` call in handler'
    )
    assert mf_pos != -1, (
        'expected `_atomicWriteJson(fastpathDir, "manifest.json"` call in handler'
    )
    assert cm_pos < mf_pos, (
        f"contentmap write (offset {cm_pos}) must appear BEFORE manifest write "
        f"(offset {mf_pos}) — manifest is the commit marker (Codex write-order contract)"
    )


def test_handler_uses_manifest_slugs_for_output_path(handler_source: str) -> None:
    """Output paths come from ``manifest.slugs[slug]``, NOT re-derived.

    Codex contract: use manifest.slugs[slug].output_path for writes.
    """
    assert "manifest.slugs" in handler_source or "slugEntry" in handler_source, (
        "expected output path sourced from manifest.slugs[slug] in handler — "
        "Codex: do NOT re-derive output paths"
    )


def test_handler_does_not_modify_parent_build_id(handler_source: str) -> None:
    """Partial emit preserves ``parent_build_id`` from the full build.

    Checking that the handler does NOT overwrite manifest.parent_build_id with a
    new value (it spreads the existing manifest object, keeping parent_build_id).
    """
    # The handler should NOT assign a NEW parent_build_id (only timestamps update).
    assert "parent_build_id:" not in handler_source, (
        "found `parent_build_id:` assignment in handler — "
        "partial emit must NOT change parent_build_id; it inherits it from the full build"
    )


# ---------------------------------------------------------------------------
# Explorer.tsx: Amendment #8 — slug-derived deterministic ID
# ---------------------------------------------------------------------------


def test_explorer_removes_num_explorers_counter(explorer_source: str) -> None:
    """``Explorer.tsx`` overlay does NOT contain the ``numExplorers++`` counter."""
    assert "numExplorers++" not in explorer_source, (
        "found `numExplorers++` in Explorer.tsx overlay — "
        "Amendment #8: the module-level counter must be replaced with slug-derived id"
    )


def test_explorer_removes_num_explorers_let(explorer_source: str) -> None:
    """``Explorer.tsx`` overlay does NOT declare ``let numExplorers``.

    The file header comment explains what the OLD code looked like (mentioning
    ``let numExplorers = 0``).  We therefore strip single-line ``//`` comments
    before checking, so only executable code is scanned.
    """
    executable = re.sub(r"//[^\n]*", "", explorer_source)
    assert "let numExplorers" not in executable, (
        "found `let numExplorers` in executable code of Explorer.tsx overlay — "
        "Amendment #8: the module-level counter must be removed entirely"
    )


def test_explorer_uses_slug_derived_id(explorer_source: str) -> None:
    """``Explorer.tsx`` overlay uses a slug-derived id for the Explorer component."""
    assert "slugId" in explorer_source or "fileData?.slug" in explorer_source, (
        "expected slug-derived id logic in Explorer.tsx overlay — "
        "Amendment #8: id must be deterministic based on slug, not render order"
    )


def test_explorer_uses_file_data_prop(explorer_source: str) -> None:
    """``Explorer.tsx`` overlay destructures ``fileData`` from props."""
    assert "fileData" in explorer_source, (
        "expected `fileData` in Explorer.tsx component props — "
        "slug-derived id requires the current page's fileData"
    )


# ---------------------------------------------------------------------------
# Fix #1 — Full corpus reconstruction (backlinks + transclusion safety)
# ---------------------------------------------------------------------------


def test_handler_filteredcontent_includes_full_corpus(handler_source: str) -> None:
    """filteredContent is built from changed file + all unchanged files (Fix #1).

    The old bug was ``filteredContent = parsed`` — a 1-element array containing only
    the changed file.  Emitters like ContentPage.partialEmit derive
    ``allFiles = content.map(c => c[1].data)`` from this array; with only one entry
    Backlinks and transclusion resolution lost all cross-file context.

    The fix synthesizes unchanged entries from contentmap and spreads them in.
    """
    assert "synthesizedUnchanged" in handler_source, (
        "expected `synthesizedUnchanged` in build_partial_handler.js — "
        "Fix #1: unchanged contentmap entries must be synthesized and included in "
        "filteredContent for backlinks + transclusion to work correctly"
    )
    assert "filteredContent = [parsed[0]" in handler_source or \
           "filteredContent = [parsed[0], ...synthesizedUnchanged]" in handler_source, (
        "expected `filteredContent = [parsed[0], ...synthesizedUnchanged]` pattern — "
        "Fix #1: changed file must be first entry; unchanged entries follow"
    )


def test_handler_full_corpus_date_rehydration(handler_source: str) -> None:
    """Synthesized unchanged entries rehydrate Date strings to Date instances (Fix #1).

    The contentmap stores dates as ISO-string JSON.  Emitters that compare or render
    dates (e.g. FolderPage, TagPage) expect Date instances.  The synthesis must
    mirror parse.ts's rehydrateDates pattern.
    """
    # The synthesis block must contain new Date() calls for each date field.
    assert "new Date(d.dates.created)" in handler_source or \
           "new Date(d.dates" in handler_source, (
        "expected Date rehydration in synthesized unchanged entries — "
        "Fix #1: dates in contentmap are ISO strings; must be rehydrated to Date instances"
    )


def test_handler_filteredcontent_null_ast_for_unchanged(handler_source: str) -> None:
    """Synthesized unchanged entries document that AST (index 0) is null (Fix #1).

    Only the changed file's entry carries a real HAST Root.  Unchanged entries
    have ``null`` at index 0.  ContentPage.partialEmit only accesses ``c[0]``
    (the HAST) for slugs in ``changedSlugs`` — so null AST for unchanged entries
    is safe for ContentPage and the asset/alias/static emitters.  Emitters that
    DO read c[0] for unchanged corpus entries (ContentIndex, TagPage, FolderPage)
    are excluded via ``_PARTIAL_EMIT_EXCLUDED``; edits to slugs they own
    (``tags/*``, ``*/index``, root ``index``) are refused at handler entry with
    exit 6 so the watcher falls back to full build.
    """
    assert "[null, { data:" in handler_source or \
           "return [null," in handler_source, (
        "expected `[null, { data: ... }]` shape for synthesized unchanged entries — "
        "Fix #1: AST slot must be null for metadata-only contentmap entries"
    )


# ---------------------------------------------------------------------------
# Fix #2 — Fail-fast on emitter exception (Plan B contract)
# ---------------------------------------------------------------------------


def test_handler_fail_fast_exit_code_5(handler_source: str) -> None:
    """``build_partial_handler.js`` exits with code 5 on emitter failure (Fix #2).

    Exit code 5 is new and distinct from 1-4 (manifest/envelope/slug guards).
    T5 (the Python wrapper) uses this to trigger a full-build fallback.
    """
    assert "process.exit(5)" in handler_source, (
        "expected `process.exit(5)` in build_partial_handler.js — "
        "Fix #2: emitter exceptions must exit 5 (distinct from 1-4 guard failures)"
    )


def test_handler_partial_emit_failed_message(handler_source: str) -> None:
    """``build_partial_handler.js`` writes 'partial emit failed in' to stderr (Fix #2).

    The message must contain the emitter name so T5/operators can diagnose which
    emitter threw.
    """
    assert "partial emit failed in" in handler_source, (
        "expected 'partial emit failed in' stderr message in build_partial_handler.js — "
        "Fix #2: fail-fast catch block must name the failing emitter"
    )


def test_handler_fail_fast_exit_before_artifact_writes(handler_source: str) -> None:
    """process.exit(5) appears in source BEFORE the artifact write block (Fix #2).

    The runtime contract: process.exit(5) inside the TS executor aborts the
    Node.js process before the outer handler reaches _atomicWriteJson calls.
    This static check confirms the source layout preserves that invariant —
    exit(5) is embedded in _PARTIAL_BUILD_TS (compiled executor), which appears
    earlier in the file than the _atomicWriteJson calls in handlePartialBuild.
    """
    exit5_pos = handler_source.find("process.exit(5)")
    contentmap_write_pos = handler_source.find('_atomicWriteJson(fastpathDir, "contentmap.json"')
    assert exit5_pos != -1, "expected `process.exit(5)` in handler source"
    assert contentmap_write_pos != -1, (
        'expected `_atomicWriteJson(fastpathDir, "contentmap.json"` in handler source'
    )
    assert exit5_pos < contentmap_write_pos, (
        f"process.exit(5) at offset {exit5_pos} must appear BEFORE "
        f"contentmap write at offset {contentmap_write_pos} — "
        "Fix #2: emitter failure must abort before any artifact is written"
    )


def test_handler_no_per_emitter_catch_continue(handler_source: str) -> None:
    """The old per-emitter try/catch-and-continue pattern is removed (Fix #2).

    The prior code wrapped each emitter invocation in its own try/catch and called
    ``console.warn(... + " error (continuing): " + err)`` on failure, letting the
    emitter walk complete and the artifact writes proceed.  That pattern is replaced
    by a single outer try/catch that exits immediately.
    """
    assert "error (continuing)" not in handler_source, (
        "found 'error (continuing)' in build_partial_handler.js — "
        "Fix #2: per-emitter catch-and-continue is removed; "
        "any emitter exception must exit 5 before artifact writes"
    )


# ---------------------------------------------------------------------------
# Fix #3 — ChangeEvent.path uses vault-relative source_path
# ---------------------------------------------------------------------------


def test_handler_change_event_path_is_vault_relative(handler_source: str) -> None:
    """ChangeEvent.path uses vault-relative ``slugEntry.source_path`` (Fix #3).

    Upstream chokidar watcher records vault-relative paths (build.ts:416-425) and
    rebuild() emits those same relative keys as ChangeEvent.path (build.ts:499-507).
    Using absoluteSourcePath here violated the Quartz event contract.
    """
    assert "path: slugEntry.source_path as FilePath" in handler_source, (
        "expected `path: slugEntry.source_path as FilePath` in ChangeEvent — "
        "Fix #3: path must be vault-relative (slugEntry.source_path), not absolute"
    )


def test_handler_change_event_not_absolute_path(handler_source: str) -> None:
    """ChangeEvent.path does NOT use the old absoluteSourcePath value (Fix #3).

    The old code had ``path: absoluteSourcePath as FilePath``.
    After Fix #3 this must not appear in the ChangeEvent literal.
    """
    assert "path: absoluteSourcePath as FilePath" not in handler_source, (
        "found `path: absoluteSourcePath as FilePath` in ChangeEvent — "
        "Fix #3: must use vault-relative slugEntry.source_path, not absolute path"
    )


# ---------------------------------------------------------------------------
# Fix #4 — Unsupported-slug scope guard (Codex T4 review #4)
# ---------------------------------------------------------------------------


def test_handler_refuses_tag_folder_index_slugs(handler_source: str) -> None:
    """Handler must explicitly refuse slug.startsWith('tags/') or .endsWith('/index').

    Codex T4 review #4: ContentPage.partialEmit skips slugs starting with "tags/"
    or ending with "/index" (contentPage.tsx:112-117), AND our deny-list also skips
    TagPage/FolderPage which own those outputs.  A fast-path edit to such a slug would
    advance manifest/contentmap/.build-id while leaving the rendered HTML stale.
    The guard must exit 6 before the executor runs.
    """
    assert 'startsWith("tags/")' in handler_source, (
        "expected tags/ slug refusal in handler — Codex T4 review #4: "
        "slugs starting with 'tags/' must exit 6 before executor runs"
    )
    assert 'endsWith("/index")' in handler_source, (
        "expected /index slug refusal in handler — Codex T4 review #4: "
        "slugs ending with '/index' must exit 6 before executor runs"
    )
    assert "process.exit(6)" in handler_source, (
        "expected exit code 6 for unsupported-slug scope violation — "
        "Codex T4 review #4: tag/folder/index pages must be refused with exit 6"
    )
