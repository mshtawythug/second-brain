"""Cross-language parity tests for the fastpath fingerprint canonical blob.

Asserts that the TypeScript implementation (fingerprint_parity_runner.mjs)
and the Python implementation (brain.wiki.fastpath_manifest.compute_fingerprint)
produce byte-identical canonical blobs (and therefore identical sha256 hashes)
for every case in the golden corpus.

Skip-gate: requires ``node`` on PATH. When node is absent, all tests skip.

The corpus is designed to exercise every section of the canonical blob:
  SECTION_VERSION, SECTION_SLUG, SECTION_SOURCE_PATH, SECTION_OUTPUT_PATH,
  SECTION_FRONTMATTER, SECTION_TAGS, SECTION_WIKILINKS, SECTION_TRANSCLUSIONS,
  SECTION_BLOCK_REFS, SECTION_HEADING_ANCHORS.

Note on the "unknown frontmatter field" corpus case: the Python classifier
raises ManifestError for unknown fields (correct behaviour — forces non-trivial).
The TS side would silently ignore the field and produce a hash. The parity test
validates only the Python error path for this case; it does not assert TS==Python
for this input (the test case is marked xfail-on-parity-for-unknown-field).
"""
from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from brain.wiki.fastpath_manifest import (
    FINGERPRINT_VERSION,
    ManifestError,
    compute_fingerprint,
    compute_fingerprint_with_blob,
)

# ---------------------------------------------------------------------------
# Skip gate: node must be on PATH
# ---------------------------------------------------------------------------

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE is None,
    reason="`node` not on PATH — cross-language parity tests skipped",
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER = REPO_ROOT / "tests" / "wiki" / "fixtures" / "fingerprint_parity_runner.mjs"

# ---------------------------------------------------------------------------
# Session fixture — spawn the Node runner once for the whole test session.
# This drops per-test node-startup overhead (~80ms × N tests → ~50ms total).
# ---------------------------------------------------------------------------

_SESSION_RUNNER: Callable[[str, str, str, str], str] | None = None


@pytest.fixture(scope="session", autouse=True)
def _init_session_runner() -> Iterator[None]:
    """Start the parity runner Node process once and wire it to _ts_fingerprint."""
    global _SESSION_RUNNER
    if _NODE is None:
        yield
        return

    proc = subprocess.Popen(
        [_NODE, str(RUNNER), "--session"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
    )

    def _call(slug: str, source_path: str, output_path: str, source_text: str) -> str:
        payload = json.dumps(
            {
                "slug": slug,
                "source_path": source_path,
                "output_path": output_path,
                "source_text": source_text,
            }
        )
        assert proc.stdin is not None
        proc.stdin.write(payload + "\n")
        proc.stdin.flush()
        assert proc.stdout is not None
        return proc.stdout.readline().strip()

    _SESSION_RUNNER = _call
    yield

    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"shutdown": True}) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
    except BrokenPipeError:
        pass
    finally:
        proc.wait(timeout=5)
        _SESSION_RUNNER = None


# ---------------------------------------------------------------------------
# Helper: run node runner (uses session process; falls back to subprocess spawn)
# ---------------------------------------------------------------------------


def _ts_fingerprint(slug: str, source_path: str, output_path: str, source_text: str) -> str:
    """Return the TS fingerprint for the given inputs."""
    if _SESSION_RUNNER is not None:
        return _SESSION_RUNNER(slug, source_path, output_path, source_text)
    # Fallback: spawn a fresh process (used when session fixture is unavailable).
    assert _NODE is not None
    payload = json.dumps(
        {
            "slug": slug,
            "source_path": source_path,
            "output_path": output_path,
            "source_text": source_text,
        }
    )
    result = subprocess.run(
        [_NODE, str(RUNNER)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Node runner failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def _ts_fingerprint_and_blob(
    slug: str, source_path: str, output_path: str, source_text: str
) -> tuple[str, bytes]:
    """Run runner with --emit-blob and return (fingerprint, blob_bytes).

    Always spawns a fresh process (only called on failure; avoids session
    complication with the --emit-blob flag).
    """
    assert _NODE is not None
    payload = json.dumps(
        {
            "slug": slug,
            "source_path": source_path,
            "output_path": output_path,
            "source_text": source_text,
        }
    )
    result = subprocess.run(
        [_NODE, str(RUNNER), "--emit-blob"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Node runner (--emit-blob) failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    data = json.loads(result.stdout.strip())
    return data["fingerprint"], bytes.fromhex(data["blob_hex"])


def _py_fingerprint(slug: str, source_path: str, output_path: str, source_text: str) -> str:
    """Run the Python compute_fingerprint and return the hex string."""
    return compute_fingerprint(
        source_bytes=source_text.encode("utf-8"),
        slug=slug,
        source_path=source_path,
        output_path=output_path,
    )


# ---------------------------------------------------------------------------
# Canonical-blob section identification (for failure messages)
# ---------------------------------------------------------------------------

_BLOB_SECTION_NAMES = [
    "SECTION_VERSION",
    "SECTION_SLUG",
    "SECTION_SOURCE_PATH",
    "SECTION_OUTPUT_PATH",
    "SECTION_FRONTMATTER",
    "SECTION_TAGS",
    "SECTION_WIKILINKS",
    "SECTION_TRANSCLUSIONS",
    "SECTION_BLOCK_REFS",
    "SECTION_HEADING_ANCHORS",
]


def _identify_section(blob: bytes, offset: int) -> str:
    """Return the section name for a byte offset in the canonical blob."""
    if offset < 4:
        return "SECTION_VERSION"
    pos = 4
    for name in _BLOB_SECTION_NAMES[1:]:
        if pos + 4 > len(blob):
            return f"{name} (truncated blob at pos {pos})"
        (length,) = struct.unpack(">I", blob[pos : pos + 4])
        if offset < pos + 4 + length:
            within = offset - pos - 4
            return f"{name} (byte {within} of {length})"
        pos += 4 + length
    return f"beyond blob end at offset {offset}"


def _format_parity_failure(
    slug: str, source_path: str, output_path: str, source_text: str,
    ts_fp: str, py_fp: str,
) -> str:
    """Build a detailed diff message showing blob hex and first-divergent byte."""
    try:
        ts_fp2, ts_blob = _ts_fingerprint_and_blob(slug, source_path, output_path, source_text)
        _, py_blob = compute_fingerprint_with_blob(
            source_bytes=source_text.encode("utf-8"),
            slug=slug,
            source_path=source_path,
            output_path=output_path,
        )
        first_diff: str = "identical blobs (hash collision?)"
        for i, (tb, pb) in enumerate(zip(ts_blob, py_blob, strict=False)):
            if tb != pb:
                section = _identify_section(ts_blob, i)
                first_diff = (
                    f"offset {i}: TS=0x{tb:02x} Py=0x{pb:02x} → {section}"
                )
                break
        if len(ts_blob) != len(py_blob):
            first_diff += f" (TS blob {len(ts_blob)}B vs Py blob {len(py_blob)}B)"
        return (
            f"PARITY FAILURE for slug={slug!r}\n"
            f"  TS fingerprint: {ts_fp}\n"
            f"  Py fingerprint: {py_fp}\n"
            f"  TS blob:        {ts_blob.hex()}\n"
            f"  Py blob:        {py_blob.hex()}\n"
            f"  First diff:     {first_diff}"
        )
    except Exception as exc:  # noqa: BLE001
        return (
            f"PARITY FAILURE for slug={slug!r}\n"
            f"  TS: {ts_fp}\n  Py: {py_fp}\n"
            f"  (blob detail unavailable: {exc})"
        )


# ---------------------------------------------------------------------------
# Core parity assertion
# ---------------------------------------------------------------------------


def _assert_parity(slug: str, source_path: str, output_path: str, source_text: str) -> None:
    """Assert TS and Python produce identical fingerprints for the given input."""
    ts = _ts_fingerprint(slug, source_path, output_path, source_text)
    py = _py_fingerprint(slug, source_path, output_path, source_text)
    assert ts == py, _format_parity_failure(
        slug, source_path, output_path, source_text, ts, py
    )
    # Also verify it looks like a sha256 hex string
    assert len(ts) == 64
    assert ts == ts.lower()


# ---------------------------------------------------------------------------
# Helper: build source text from frontmatter dict + body
# ---------------------------------------------------------------------------


def _src(fm_lines: str = "", body: str = "") -> str:
    if fm_lines.strip():
        return f"---\n{fm_lines.strip()}\n---\n\n{body}"
    return body


# ---------------------------------------------------------------------------
# Golden corpus — one test per case
# ---------------------------------------------------------------------------


def test_parity_empty_frontmatter_empty_body() -> None:
    """Empty frontmatter, empty body: minimal valid input."""
    _assert_parity(
        slug="empty-note",
        source_path="empty-note.md",
        output_path="empty-note.html",
        source_text=_src(""),
    )


def test_parity_no_frontmatter_at_all() -> None:
    """File with no frontmatter block at all."""
    _assert_parity(
        slug="no-fm",
        source_path="no-fm.md",
        output_path="no-fm.html",
        source_text="Just a plain body with no frontmatter.",
    )


def test_parity_all_structural_fields_populated() -> None:
    """All allow-listed structural frontmatter fields populated."""
    fm = (
        "title: My Note\n"
        "draft: false\n"
        "publish: true\n"
        "tags:\n  - work\n  - personal\n"
        "aliases:\n  - old-title\n"
        "permalink: /custom/path\n"
        "slug: custom-slug\n"
        "lang: en\n"
        "cssclasses:\n  - wide\n"
        "socialImage: /img/thumb.png\n"
        "enableToc: true\n"
        "comments: false\n"
        "kind: note\n"
        "description: A test note.\n"
        "socialDescription: Social desc.\n"
        'date: "2024-03-15"\n'
        'created: "2024-01-01"\n'
        'modified: "2024-03-15"\n'
        'updated: "2024-03-15"\n'
        'published: "2024-03-20"\n'
    )
    _assert_parity(
        slug="full-note",
        source_path="full-note.md",
        output_path="full-note.html",
        source_text=_src(fm, "Body text here."),
    )


def test_parity_ignored_field_does_not_change_hash() -> None:
    """An ignored frontmatter field (Appendix A) produces the SAME hash as without it."""
    base = _src("title: Same\n", "Body.")
    with_ignored = _src("title: Same\nowner: pat\nhits: 99\nsource: gmail\n", "Body.")
    fp_base_ts = _ts_fingerprint("same", "same.md", "same.html", base)
    fp_with_ts = _ts_fingerprint("same", "same.md", "same.html", with_ignored)
    fp_base_py = _py_fingerprint("same", "same.md", "same.html", base)
    fp_with_py = _py_fingerprint("same", "same.md", "same.html", with_ignored)
    # TS: ignored fields silently excluded → same hash
    assert fp_base_ts == fp_with_ts
    # Python: ignored fields don't error and produce same hash
    assert fp_base_py == fp_with_py
    # Parity
    assert fp_base_ts == fp_base_py
    assert fp_with_ts == fp_with_py


def test_parity_unknown_field_python_raises_ts_computes() -> None:
    """Unknown frontmatter field: Python raises ManifestError, TS produces a hash.

    This corpus entry validates ONLY the Python error path. The parity assertion
    is skipped for this case (documented asymmetry in the spec).
    """
    src = _src("title: Note\nmy_brand_new_field: value\n", "Body.")
    # Python must raise
    with pytest.raises(ManifestError, match="unknown frontmatter field"):
        _py_fingerprint("note", "note.md", "note.html", src)
    # TS should produce a fingerprint (not error)
    ts_fp = _ts_fingerprint("note", "note.md", "note.html", src)
    assert len(ts_fp) == 64


def test_parity_tags_from_frontmatter_only() -> None:
    """Tags declared only in YAML frontmatter, none in body."""
    _assert_parity(
        slug="note-fm-tags",
        source_path="note-fm-tags.md",
        output_path="note-fm-tags.html",
        source_text=_src("title: FM Tags\ntags:\n  - work\n  - meetings\n", "Plain body."),
    )


def test_parity_tags_from_unindented_yaml_block_list() -> None:
    """YAML block-list tags may be unindented under ``tags:``."""
    _assert_parity(
        slug="note-unindented-tags",
        source_path="note-unindented-tags.md",
        output_path="note-unindented-tags.html",
        source_text=_src("title: FM Tags\ntags:\n- work\n- meetings\n", "Plain body."),
    )


def test_parity_tags_from_body_inline_only() -> None:
    """Tags declared only as inline ``#tags`` in body, none in YAML."""
    _assert_parity(
        slug="note-body-tags",
        source_path="note-body-tags.md",
        output_path="note-body-tags.html",
        source_text=_src("title: Body Tags\n", "Had a great #meeting today. #work item."),
    )


def test_parity_tags_from_both_overlap_deduped() -> None:
    """Tags from both YAML and body; overlapping tag is deduped in SECTION_TAGS."""
    fm = "title: Both Tags\ntags:\n  - work\n  - shared\n"
    body = "Notes about #shared project and #personal items."
    _assert_parity(
        slug="note-both-tags",
        source_path="note-both-tags.md",
        output_path="note-both-tags.html",
        source_text=_src(fm, body),
    )


def test_parity_tags_mixed_case_preserved() -> None:
    """Tag case is preserved (Quartz slugTag does not lowercase)."""
    _assert_parity(
        slug="note-case-tags",
        source_path="note-case-tags.md",
        output_path="note-case-tags.html",
        source_text=_src(
            "title: Case Tags\ntags:\n  - Work\n  - MixedCase\n",
            "Body with #UpperTag.",
        ),
    )


def test_parity_wikilink_with_alias() -> None:
    """Wikilink with alias: target extracted, alias dropped."""
    _assert_parity(
        slug="link-alias",
        source_path="link-alias.md",
        output_path="link-alias.html",
        source_text=_src("title: Link Alias\n", "See [[target-doc|Pretty Label]] for details."),
    )


def test_parity_wikilink_same_basename_different_folders() -> None:
    """Two wikilinks with same basename in different folders — both tracked."""
    body = "Compare [[Alpha/my-doc]] versus [[Beta/my-doc]]."
    _assert_parity(
        slug="multi-folder",
        source_path="multi-folder.md",
        output_path="multi-folder.html",
        source_text=_src("title: Multi Folder\n", body),
    )


def test_parity_wikilink_with_permalink_target() -> None:
    """Wikilink to doc that uses permalink: raw target text used, not resolved permalink."""
    body = "Ref [[my-doc]] where my-doc has a custom permalink."
    _assert_parity(
        slug="permalink-ref",
        source_path="permalink-ref.md",
        output_path="permalink-ref.html",
        source_text=_src("title: Permalink Ref\n", body),
    )


def test_parity_wikilink_with_anchor() -> None:
    """Wikilink with heading anchor: anchor preserved in blob."""
    _assert_parity(
        slug="anchor-link",
        source_path="anchor-link.md",
        output_path="anchor-link.html",
        source_text=_src("title: Anchor Link\n", "See [[my-doc#some-heading]] for context."),
    )


def test_parity_block_transclusion() -> None:
    """Block transclusion (``![[target#^blockid]]``) included in SECTION_TRANSCLUSIONS."""
    _assert_parity(
        slug="block-transclude",
        source_path="block-transclude.md",
        output_path="block-transclude.html",
        source_text=_src("title: Block Transclude\n", "Embedding: ![[other-doc#^block123]]"),
    )


def test_parity_page_transclusion() -> None:
    """Page-level transclusion (``![[target]]``) included in SECTION_TRANSCLUSIONS."""
    _assert_parity(
        slug="page-transclude",
        source_path="page-transclude.md",
        output_path="page-transclude.html",
        source_text=_src("title: Page Transclude\n", "Embedding: ![[full-page]]"),
    )


def test_parity_multiple_block_refs_sorted_deduped() -> None:
    """Multiple ``^block-id`` definitions in body: sorted and deduped."""
    body = (
        "First block. ^alpha\n\n"
        "Second block. ^beta\n\n"
        "Third block. ^alpha\n"  # duplicate — deduped
    )
    _assert_parity(
        slug="multi-blocks",
        source_path="multi-blocks.md",
        output_path="multi-blocks.html",
        source_text=_src("title: Multi Blocks\n", body),
    )


def test_parity_heading_hierarchy_document_order() -> None:
    """Heading anchors appear in document order (not sorted)."""
    body = "# Top\n## Sub A\n### Deep\n## Sub B\n"
    _assert_parity(
        slug="heading-hierarchy",
        source_path="heading-hierarchy.md",
        output_path="heading-hierarchy.html",
        source_text=_src("title: Heading Hierarchy\n", body),
    )


def test_parity_duplicate_headings_disambiguated() -> None:
    """Duplicate headings get github-slugger numeric suffixes."""
    body = "## Foo\n## Foo\n## Foo\n## Bar\n"
    _assert_parity(
        slug="dup-headings",
        source_path="dup-headings.md",
        output_path="dup-headings.html",
        source_text=_src("title: Dup Headings\n", body),
    )


def test_parity_unicode_slug_and_tag_nfc() -> None:
    """Unicode in slug, tag, and heading: both sides NFC-normalise."""
    _assert_parity(
        slug="COMPANY_REDACTED/AI-adoption-at-company-id",
        source_path="COMPANY_REDACTED/AI-adoption-at-company-id.md",
        output_path="COMPANY_REDACTED/AI-adoption-at-company-id.html",
        source_text=_src(
            "title: AI Adoption at COMPANY_REDACTED\ntags:\n  - résumé\n",
            "## Réunion\nNotes from the réunion. #café",
        ),
    )


def test_parity_path_with_mixed_case_preserved() -> None:
    """Paths with mixed case are preserved (Quartz does NOT lowercase slugs)."""
    _assert_parity(
        slug="COMPANY_REDACTED/Strategy-Q3",
        source_path="COMPANY_REDACTED/Strategy-Q3.md",
        output_path="COMPANY_REDACTED/Strategy-Q3.html",
        source_text=_src("title: Strategy Q3\n", "Quarterly notes."),
    )


def test_parity_identical_content_gives_identical_hashes() -> None:
    """Ten docs with identical post-normalisation content produce the same hash."""
    source = _src("title: Same\ntags:\n  - shared\n", "## Alpha\nCommon body.")
    hashes: set[str] = set()
    for i in range(10):
        slug = f"doc-{i:02d}"
        fp = _ts_fingerprint(slug, f"{slug}.md", f"{slug}.html", source)
        fp_py = _py_fingerprint(slug, f"{slug}.md", f"{slug}.html", source)
        assert fp == fp_py, f"Parity failure for doc-{i:02d}"
        # NOTE: fingerprints DIFFER across docs because slug is in the blob.
        # This test verifies parity within each doc, not across docs.
        hashes.add(fp)
    # Each slug differs → each fingerprint must differ
    assert len(hashes) == 10


def test_parity_hierarchical_tag() -> None:
    """Hierarchical tags (``work/projects``) preserved case and separator."""
    _assert_parity(
        slug="hierarchical-tag",
        source_path="hierarchical-tag.md",
        output_path="hierarchical-tag.html",
        source_text=_src(
            "title: Hierarchical\ntags:\n  - work/projects\n  - work/meetings\n", ""
        ),
    )


def test_parity_empty_arrays_vs_null_fields() -> None:
    """Explicitly empty array differs from absent field in frontmatter blob."""
    with_empty = _src("title: Note\ntags: []\n", "Body.")
    without_tags = _src("title: Note\n", "Body.")
    fp_empty_ts = _ts_fingerprint("note", "note.md", "note.html", with_empty)
    fp_none_ts = _ts_fingerprint("note", "note.md", "note.html", without_tags)
    fp_empty_py = _py_fingerprint("note", "note.md", "note.html", with_empty)
    fp_none_py = _py_fingerprint("note", "note.md", "note.html", without_tags)
    # Parity within each case
    assert fp_empty_ts == fp_empty_py
    assert fp_none_ts == fp_none_py
    # The two cases differ ([] vs null encodes differently)
    assert fp_empty_ts != fp_none_ts


def test_parity_bare_datetime_in_frontmatter() -> None:
    """Non-midnight bare datetime normalises to 'YYYY-MM-DDTHH:MM:SS' on both sides.

    Exercises the date-normalisation path that was previously masked by the parity
    test corpus using only quoted date strings.  pyyaml parses an unquoted
    ``created: 2024-03-15T12:00:00`` scalar as ``datetime.datetime``; the parity
    runner's ``parseMinimalYaml`` keeps it as a string.  Both sides must emit
    ``"2024-03-15T12:00:00"`` in SECTION_FRONTMATTER.
    """
    _assert_parity(
        slug="bare-datetime",
        source_path="bare-datetime.md",
        output_path="bare-datetime.html",
        source_text=_src(
            "title: Bare Datetime\ncreated: 2024-03-15T12:00:00\n", "Body."
        ),
    )


def test_parity_midnight_datetime_truncated_to_date_only() -> None:
    """Midnight datetime ``2024-01-10T00:00:00`` normalises to ``"2024-01-10"`` on both sides.

    pyyaml parses ``created: 2024-01-10T00:00:00`` as
    ``datetime.datetime(2024, 1, 10, 0, 0, 0)``; Python's ``_normalize_date_val``
    truncates midnight to date-only (``"2024-01-10"``).  The parity runner's
    ``normalizeDateVal`` must apply the same truncation for the ``T00:00:00`` string
    pattern so both sides produce ``"2024-01-10"`` in SECTION_FRONTMATTER.
    """
    _assert_parity(
        slug="midnight-datetime",
        source_path="midnight-datetime.md",
        output_path="midnight-datetime.html",
        source_text=_src(
            "title: Midnight Datetime\ncreated: 2024-01-10T00:00:00\n", "Body."
        ),
    )


# ---------------------------------------------------------------------------
# Cross-language FINGERPRINT_VERSION lockstep
# ---------------------------------------------------------------------------


def test_python_and_ts_fingerprint_version_match() -> None:
    """Python ``FINGERPRINT_VERSION`` equals the TS overlay's declared value.

    ``tests/test_quartz_fastpath_manifest_static.py`` already pins TS against
    the ``.mjs`` parity runner, but nothing pinned *Python* against either.  A
    half-applied bump leaves the TS full build stamping manifests the Python
    reader rejects, which fails closed (every edit becomes a full build) and so
    shows up as a silent performance cliff rather than a test failure.
    """
    ts_source = (
        Path(__file__).resolve().parents[2]
        / "src/brain/quartz_overrides/quartz/util/fastpath_manifest.ts"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"export const FINGERPRINT_VERSION\s*:\s*number\s*=\s*(\d+)", ts_source
    )
    assert match, "FINGERPRINT_VERSION not found in fastpath_manifest.ts"
    assert int(match.group(1)) == FINGERPRINT_VERSION, (
        f"FINGERPRINT_VERSION mismatch: Python={FINGERPRINT_VERSION} "
        f"TS={match.group(1)} — bump both together"
    )
