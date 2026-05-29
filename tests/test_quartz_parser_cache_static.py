"""Static smoke tests for the Quartz parser cache (Plan A incremental builds).

No JS toolchain is available in this test image, so we cannot compile
the TypeScript or exercise it at runtime. Instead, this file follows
the pattern established in ``tests/test_quartz_email_thread_static.py``
and ``tests/test_quartz_search_static.py``: regex / substring assertions
directly against the TS source files in ``src/brain/quartz_overrides/``.

Covered contracts:
- ``src/brain/quartz_overrides/quartz/processors/parser_cache.ts`` exists and
  exports a numeric ``CACHE_VERSION`` constant plus ``cacheKey``,
  ``cachePath``, ``getCached``, and ``putCached`` with the expected
  function signatures.
- The sha256 cache key mixes ``CACHE_VERSION`` into the hash via a
  4-byte big-endian write before the slug and file bytes.
- ``src/brain/quartz_overrides/quartz/processors/parse.ts`` exists, and its
  ``createFileParser`` body contains both ``getCached`` and ``putCached``
  (i.e. the cache hooks are wired in).
- ``createMarkdownParser`` in ``parse.ts`` does NOT contain ``getCached``
  or ``putCached`` — the HTML phase is left identical to upstream (Issue 3
  from the quality review stays fixed).
- The ``serializableCtx`` literal in ``parse.ts`` includes ``cacheDir``
  in the pattern ``cacheDir: <ident>.cacheDir``.
- ``src/brain/quartz_overrides/quartz/util/ctx.ts`` exists and declares ``cacheDir``
  inside the ``BuildCtx`` interface block.
- ``src/brain/quartz_overrides/quartz/plugins/transformers/index.ts`` documents the
  transformer-purity contract, references ``CACHE_VERSION``, and names
  ``parser_cache.ts``.

Limitations: source-shape only. A full end-to-end test would require
invoking ``npx quartz build`` against a fixture vault with a JS toolchain
not available on the test image.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_DIR = REPO_ROOT / "src" / "brain" / "quartz_overrides"
PROCESSORS_DIR = OVERRIDES_DIR / "quartz" / "processors"
PARSER_CACHE_TS = PROCESSORS_DIR / "parser_cache.ts"
PARSE_TS = PROCESSORS_DIR / "parse.ts"
CTX_TS = OVERRIDES_DIR / "quartz" / "util" / "ctx.ts"
TRANSFORMERS_INDEX_TS = OVERRIDES_DIR / "quartz" / "plugins" / "transformers" / "index.ts"


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _slice_between(source: str, start_marker: str, end_marker: str) -> str:
    """Return the source slice from ``start_marker`` up to ``end_marker``.

    Raises ``AssertionError`` when ``start_marker`` is absent (end_marker
    absence just returns the remainder of the source).
    """
    start = source.find(start_marker)
    assert start != -1, f"start marker not found in source: {start_marker!r}"
    end = source.find(end_marker, start + len(start_marker))
    if end == -1:
        # end_marker absent: slice extends to EOF (covers the last function in a file).
        return source[start:]
    return source[start:end]


# ---------------------------------------------------------------------------
# Module-level fixtures — read each file once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parser_cache_source() -> str:
    """Read ``parser_cache.ts`` once per module."""
    assert PARSER_CACHE_TS.is_file(), f"missing parser_cache.ts at {PARSER_CACHE_TS}"
    return PARSER_CACHE_TS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parse_source() -> str:
    """Read ``parse.ts`` once per module."""
    assert PARSE_TS.is_file(), f"missing parse.ts at {PARSE_TS}"
    return PARSE_TS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ctx_source() -> str:
    """Read ``ctx.ts`` once per module."""
    assert CTX_TS.is_file(), f"missing ctx.ts at {CTX_TS}"
    return CTX_TS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def transformers_index_source() -> str:
    """Read the transformers barrel once per module."""
    assert TRANSFORMERS_INDEX_TS.is_file(), (
        f"missing transformers index.ts at {TRANSFORMERS_INDEX_TS}"
    )
    return TRANSFORMERS_INDEX_TS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parser_cache.ts — file exists + exported symbols
# ---------------------------------------------------------------------------


def test_parser_cache_file_exists() -> None:
    """``parser_cache.ts`` exists at the expected overlay path."""
    assert PARSER_CACHE_TS.is_file(), (
        f"parser_cache.ts not found at {PARSER_CACHE_TS} — "
        "was the Task 1 overlay file committed?"
    )


def test_cache_version_is_numeric_export(parser_cache_source: str) -> None:
    """``CACHE_VERSION`` is exported as a numeric literal constant.

    Anchor on ``export const CACHE_VERSION`` followed by ``=`` and a
    number so a stale string value or a removed export trips the test.
    """
    assert re.search(r"export const CACHE_VERSION\s*=\s*\d+", parser_cache_source), (
        "expected `export const CACHE_VERSION = <number>` in parser_cache.ts"
    )


@pytest.mark.parametrize(
    "fn_signature",
    [
        # Each string is a unique prefix of the exported function signature;
        # enough to pin both the name and the argument shape.
        "export function cacheKey(fileBytes: Buffer, slug: string): string",
        "export function cachePath(cacheDir: string, key: string): string",
        "export function getCached<T>(cacheDir: string, key: string): T | null",
        "export function putCached<T>(cacheDir: string, key: string, value: T): void",
    ],
)
def test_parser_cache_exports_expected_function(
    parser_cache_source: str, fn_signature: str
) -> None:
    """Each core function is exported with the expected signature prefix."""
    assert fn_signature in parser_cache_source, (
        f"expected function signature `{fn_signature}` in parser_cache.ts"
    )


# ---------------------------------------------------------------------------
# parser_cache.ts — sha256 keying mixes CACHE_VERSION before slug + bytes
# ---------------------------------------------------------------------------


def test_cache_key_uses_sha256(parser_cache_source: str) -> None:
    """``cacheKey`` calls ``createHash("sha256")``."""
    assert 'createHash("sha256")' in parser_cache_source, (
        'expected `createHash("sha256")` call inside cacheKey in parser_cache.ts'
    )


def test_cache_key_mixes_version_into_hash(parser_cache_source: str) -> None:
    """``CACHE_VERSION`` is written into the hash as a 4-byte big-endian before slug/bytes.

    The mixing pattern is: allocate a 4-byte Buffer, write CACHE_VERSION
    via ``writeUInt32BE``, then ``hash.update(versionBuf)`` — all within
    the ``cacheKey`` function body, before the slug and file bytes updates.
    Pin on ``writeUInt32BE(CACHE_VERSION`` so a rename of the constant or
    a change to a non-version-mixing approach trips the test.
    """
    cache_key_section = _slice_between(
        parser_cache_source,
        "export function cacheKey(",
        "\nexport function cachePath(",
    )
    assert "writeUInt32BE(CACHE_VERSION" in cache_key_section, (
        "expected `writeUInt32BE(CACHE_VERSION, ...)` inside cacheKey — "
        "CACHE_VERSION must be mixed into the hash as a 4-byte big-endian value"
    )
    assert "hash.update(versionBuf)" in cache_key_section, (
        "expected `hash.update(versionBuf)` inside cacheKey — "
        "the version buffer must be fed into the hash before slug + bytes"
    )


# ---------------------------------------------------------------------------
# parse.ts — file exists
# ---------------------------------------------------------------------------


def test_parse_ts_file_exists() -> None:
    """``parse.ts`` exists at the expected overlay path."""
    assert PARSE_TS.is_file(), f"parse.ts not found at {PARSE_TS}"


# ---------------------------------------------------------------------------
# parse.ts — createFileParser body contains cache hooks
# ---------------------------------------------------------------------------


def test_create_file_parser_uses_get_cached(parse_source: str) -> None:
    """``createFileParser`` body contains a ``getCached`` call (cache read path).

    Scans only the text between ``export function createFileParser`` and
    the next top-level ``export function createMarkdownParser`` to avoid
    false positives from the import line.
    """
    section = _slice_between(
        parse_source,
        "export function createFileParser(",
        "\nexport function createMarkdownParser(",
    )
    assert "getCached" in section, (
        "expected `getCached(...)` call inside createFileParser — "
        "the MDAST cache read path is missing"
    )


def test_create_file_parser_uses_put_cached(parse_source: str) -> None:
    """``createFileParser`` body contains a ``putCached`` call (cache write path)."""
    section = _slice_between(
        parse_source,
        "export function createFileParser(",
        "\nexport function createMarkdownParser(",
    )
    assert "putCached" in section, (
        "expected `putCached(...)` call inside createFileParser — "
        "the MDAST cache write path is missing"
    )


# ---------------------------------------------------------------------------
# parse.ts — createMarkdownParser body must NOT contain cache hooks
# ---------------------------------------------------------------------------


def test_create_markdown_parser_has_no_get_cached(parse_source: str) -> None:
    """``createMarkdownParser`` body has NO ``getCached`` call (Issue 3 regression guard).

    Only ``createFileParser`` (the MDAST phase) gets cache hooks; the
    HTML phase (``createMarkdownParser``) is left identical to upstream.
    """
    section = _slice_between(
        parse_source,
        "export function createMarkdownParser(",
        "\nexport async function parseMarkdown(",
    )
    assert "getCached" not in section, (
        "getCached must NOT appear in createMarkdownParser body — "
        "the HTML phase must remain cache-free (Issue 3 from quality review)"
    )


def test_create_markdown_parser_has_no_put_cached(parse_source: str) -> None:
    """``createMarkdownParser`` body has NO ``putCached`` call (Issue 3 regression guard)."""
    section = _slice_between(
        parse_source,
        "export function createMarkdownParser(",
        "\nexport async function parseMarkdown(",
    )
    assert "putCached" not in section, (
        "putCached must NOT appear in createMarkdownParser body — "
        "the HTML phase must remain cache-free (Issue 3 from quality review)"
    )


# ---------------------------------------------------------------------------
# parse.ts — Date rehydration on cache hit (regression guard for the
# `date.toISOString is not a function` crash from JSON-stringified Dates)
# ---------------------------------------------------------------------------


def test_parse_ts_defines_rehydrate_dates_helper(parse_source: str) -> None:
    """``rehydrateDates`` helper exists in parse.ts.

    Regression guard: JSON.stringify turns Date objects into ISO strings,
    so cache hits return ``data.dates.{created,modified,published}`` as
    strings rather than Dates. ``Date.tsx`` calls ``.toISOString()`` on
    the value and crashes if it's a string. The helper rebuilds Date
    instances in place after every cache hit.
    """
    assert "function rehydrateDates" in parse_source, (
        "expected a `rehydrateDates` helper in parse.ts — without it, "
        "cache hits return stringified dates and components like "
        "Date.tsx crash with `date.toISOString is not a function`"
    )


def test_rehydrate_dates_covers_three_date_fields(parse_source: str) -> None:
    """``rehydrateDates`` rebuilds Dates for created, modified, published."""
    section = _slice_between(
        parse_source,
        "function rehydrateDates(",
        "\nexport function createFileParser(",
    )
    for field in ("created", "modified", "published"):
        assert f'"{field}"' in section, (
            f"rehydrateDates must list `{field}` — the upstream lastmod "
            f"transformer populates data.dates.{field} with a Date instance"
        )
    assert "new Date(" in section, (
        "rehydrateDates must call `new Date(...)` to rebuild Date instances "
        "from the stringified ISO values"
    )


def test_create_file_parser_calls_rehydrate_dates_on_hit(parse_source: str) -> None:
    """The cache-hit branch in ``createFileParser`` calls ``rehydrateDates``."""
    section = _slice_between(
        parse_source,
        "export function createFileParser(",
        "\nexport function createMarkdownParser(",
    )
    assert "rehydrateDates(" in section, (
        "expected `rehydrateDates(...)` call inside createFileParser cache-hit "
        "branch — without it, cached data.dates.{created,modified,published} "
        "stay as strings and Date.tsx crashes"
    )


# ---------------------------------------------------------------------------
# parse.ts — serializableCtx includes cacheDir
# ---------------------------------------------------------------------------


def test_serializable_ctx_includes_cache_dir(parse_source: str) -> None:
    """The ``serializableCtx`` worker literal includes ``cacheDir: <ctx>.cacheDir``.

    Workers share the same on-disk cache; without this field, worker
    threads always see ``cacheDir`` as ``undefined`` and the cache is
    silently disabled for multi-threaded builds. Pin the pattern
    ``cacheDir: <ident>.cacheDir`` so a rename or omission trips this test.
    """
    assert re.search(r"cacheDir:\s*\w+\.cacheDir", parse_source), (
        "expected `cacheDir: <ctx>.cacheDir` pattern inside the "
        "serializableCtx literal in parse.ts — workers must receive cacheDir"
    )


# ---------------------------------------------------------------------------
# ctx.ts — file exists + BuildCtx interface declares cacheDir
# ---------------------------------------------------------------------------


def test_ctx_ts_file_exists() -> None:
    """``ctx.ts`` exists at the expected overlay path."""
    assert CTX_TS.is_file(), f"ctx.ts not found at {CTX_TS}"


def test_build_ctx_interface_declares_cache_dir(ctx_source: str) -> None:
    """The ``BuildCtx`` interface block declares the ``cacheDir`` field.

    Scans only between ``export interface BuildCtx {`` and the next
    closing brace to avoid matching ``cacheDir`` in a comment outside
    the interface (e.g. in the file-level docstring).
    """
    interface_block = _slice_between(ctx_source, "export interface BuildCtx {", "\n}")
    assert "cacheDir" in interface_block, (
        "expected `cacheDir` field inside `export interface BuildCtx { ... }` "
        "in ctx.ts — needed for workers + callers that pass a custom cache path"
    )


# ---------------------------------------------------------------------------
# transformers/index.ts — purity-contract docstring
# ---------------------------------------------------------------------------


def test_transformers_index_documents_purity_contract(transformers_index_source: str) -> None:
    """The transformers barrel documents the purity contract: pure functions of (file bytes, slug).

    Any dev adding a cross-file transformer at parse time would read this
    comment and be warned. Anchor on the literal phrase so a future
    docstring rewrite that drops the contract surfaces here.
    """
    assert "pure functions of" in transformers_index_source, (
        "expected 'pure functions of' in transformers/index.ts purity contract docstring"
    )
    assert "(file bytes, slug)" in transformers_index_source, (
        "expected '(file bytes, slug)' in transformers/index.ts purity contract docstring"
    )


def test_transformers_index_references_cache_version(transformers_index_source: str) -> None:
    """The transformers barrel mentions ``CACHE_VERSION`` in its docstring.

    The version-bumping instruction is only useful if devs can discover it
    from the barrel they edit. Pinning the name means a find-replace that
    renames the constant must also update the docstring.
    """
    assert "CACHE_VERSION" in transformers_index_source, (
        "expected 'CACHE_VERSION' reference in transformers/index.ts — "
        "version-bumping instructions belong in the barrel"
    )


def test_transformers_index_references_parser_cache_file(transformers_index_source: str) -> None:
    """The transformers barrel names ``parser_cache.ts`` in its docstring.

    Makes the cache module discoverable from the barrel that governs its
    correctness contract.
    """
    assert "parser_cache.ts" in transformers_index_source, (
        "expected 'parser_cache.ts' filename reference in transformers/index.ts docstring"
    )


# ---------------------------------------------------------------------------
# Cache-path contract — TS default must align with bash --clean-cache target
# ---------------------------------------------------------------------------

# The --clean-cache target now lives in the Python orchestrator
# (brain.maintenance.run_stages) instead of the retired bash template.
MAINTENANCE_PY = REPO_ROOT / "src" / "brain" / "maintenance.py"


@pytest.fixture(scope="module")
def maintenance_source() -> str:
    """Read ``src/brain/maintenance.py`` once per module."""
    assert MAINTENANCE_PY.is_file(), f"missing maintenance module at {MAINTENANCE_PY}"
    return MAINTENANCE_PY.read_text(encoding="utf-8")


def test_parse_ts_default_cache_dir_matches_python_clean_cache_target(
    parse_source: str, maintenance_source: str
) -> None:
    """TS default cache-dir and Python ``--clean-cache`` rm target resolve to the same path.

    Plan A specifies ``<vault>/.quartz/.cache/parser/`` as the canonical path.
    ``parseMarkdown`` in ``parse.ts`` must default there; ``brain.maintenance.run_stages``
    (the Python orchestrator) must ``shutil.rmtree`` the same path when
    ``--clean-cache`` is requested.  Any drift between the two sides means
    cold-cache wipes miss the actual cache directory.

    Verifications:
    1. The TS default contains the segments ``.quartz``, ``.cache``, and
       ``parser`` in that order, rooted at ``argv.directory``.
    2. The Python rm target references ``.quartz``, ``.cache``, and ``parser``
       as Path division steps (``vault_path / ".quartz" / ".cache" / "parser"``).
    3. Both resolve to ``<base>/.quartz/.cache/parser`` with the same base.
    """
    # --- TS side ---
    # Extract the default cacheDir path.join(...) call from parseMarkdown.
    ts_section = _slice_between(
        parse_source,
        "export async function parseMarkdown(",
        "\nconst clamp",  # first top-level const after the function — EOF sentinel
    )
    # The literal must use path.join(argv.directory, ".quartz", ".cache", "parser").
    # Check the three sub-segments appear in the expected order within the join call.
    join_match = re.search(
        r'path\.join\(argv\.directory\s*,([^)]+)\)',
        ts_section,
    )
    assert join_match is not None, (
        "expected `path.join(argv.directory, ...)` call for default cacheDir in parseMarkdown"
    )
    join_args = join_match.group(1)
    # Each segment must appear as a quoted string literal in the correct order.
    quartz_pos = join_args.find('".quartz"')
    cache_pos = join_args.find('".cache"')
    parser_pos = join_args.find('"parser"')
    assert quartz_pos != -1, (
        'expected `".quartz"` segment in default cacheDir path.join call in parseMarkdown'
    )
    assert cache_pos != -1, (
        'expected `".cache"` segment in default cacheDir path.join call in parseMarkdown'
    )
    assert parser_pos != -1, (
        'expected `"parser"` segment in default cacheDir path.join call in parseMarkdown'
    )
    assert quartz_pos < cache_pos < parser_pos, (
        'expected segment order .quartz → .cache → parser in default cacheDir path.join'
    )

    # --- Python side (import-based — not a substring scan) ---
    # _PARSER_CACHE_RELPATH is the single source of truth for the wipe target.
    # Importing it and comparing its string form proves the Python side agrees
    # with the TS default on the exact relative path.
    from brain.maintenance import _PARSER_CACHE_RELPATH  # noqa: PLC0415

    assert str(_PARSER_CACHE_RELPATH) == ".quartz/.cache/parser", (
        f"brain.maintenance._PARSER_CACHE_RELPATH must equal '.quartz/.cache/parser'; "
        f"got {str(_PARSER_CACHE_RELPATH)!r} — update the constant or the TS overlay."
    )

    # --- Cross-file contract: both TS overlay files must reference the same path ---
    # ctx.ts declares the cacheDir interface field; parse.ts sets its default.
    # Both must contain the canonical suffix so a rename of either side fails fast.
    for ts_rel in ("quartz/util/ctx.ts", "quartz/processors/parse.ts"):
        ts_text = (OVERRIDES_DIR / ts_rel).read_text(encoding="utf-8")
        assert ".quartz/.cache/parser" in ts_text, (
            f"expected '.quartz/.cache/parser' in {ts_rel} — "
            "it must match brain.maintenance._PARSER_CACHE_RELPATH"
        )
