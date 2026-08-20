"""Fast-path manifest: per-slug structural fingerprint + manifest I/O.

Python counterpart to ``brain/quartz_overrides/quartz/util/fastpath_manifest.ts``.
Both implementations MUST produce byte-identical canonical blobs for the same
input — verified by ``tests/wiki/test_fastpath_fingerprint_parity.py``.

Canonical-blob format: ``docs/specs/2026-05-09-fastpath-fingerprint.md``.

Design constraints:
- Pure Python; no Node/subprocess.
- Reads manifest.json written by the TS full-build hook.
- Computes fingerprint at classifier time (T3) for the current file on disk.
- Ships no vault-specific frontmatter keys: which non-structural keys may be
  ignored lives in :mod:`brain.wiki.ignored_fields`, defaulting to a generic
  set and extensible per-vault. Unknown keys still fail closed.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from brain.errors import BrainError
from brain.vault.frontmatter import parse_frontmatter
from brain.wiki.ignored_fields import (
    DEFAULT_IGNORE_RULES,
    IGNORE_FILE_NAME,
    IgnoreRules,
)

from ._github_slugger import Slugger

# Bumped 1 -> 2 when the ignore set moved out of this module and into
# `brain.wiki.ignored_fields` + an optional per-vault file.
#
# Measured, not assumed: the ignore set never reaches the canonical blob. Its
# only consumer is the unknown-key guard in `_compute_canonical_blob`; the blob
# is built from `_STRUCTURAL_FIELD_ORDER` alone. So changing which keys are
# ignored cannot change any fingerprint VALUE — it only moves a key between
# "raises ManifestError" and "computes". Both destinations are safe: a raise
# becomes NON_TRIVIAL (full build) in `classify_edit`, and an ignored key is
# absent from the blob, so the value computed still equals the one the TS full
# build stored. The TS side has no ignore list at all and never raises on an
# unknown key, which is why the two implementations stay byte-identical
# regardless of this list.
#
# The bump is therefore a deliberate belt-and-braces choice, not a correctness
# requirement, and this comment exists so the next reader does not re-derive
# that the hard way. It costs one full rebuild and buys a clean version
# boundary: every cached fingerprint predates the change, so nothing is
# evaluated under a mix of old and new rules.
#
# All four sites move together or the fast path silently dies:
#   src/brain/wiki/fastpath_manifest.py            (here)
#   src/brain/quartz_overrides/quartz/util/fastpath_manifest.ts
#   tests/wiki/fixtures/fingerprint_parity_runner.mjs
#   the asserting tests in tests/test_quartz_fastpath_manifest_static.py
#     and tests/wiki/test_fastpath_manifest.py
# tests/wiki/test_fastpath_fingerprint_parity.py pins Python against the TS
# constant, which nothing did before this bump.
FINGERPRINT_VERSION: int = 2

# ---------------------------------------------------------------------------
# Structural frontmatter fields — canonical key order (spec §Frontmatter blob).
# Changes to this list must bump FINGERPRINT_VERSION.
# ---------------------------------------------------------------------------

_STRUCTURAL_FIELD_ORDER: tuple[str, ...] = (
    "title", "draft", "publish", "tags", "aliases", "permalink", "slug",
    "lang", "cssclasses", "socialImage", "enableToc", "comments", "kind",
    "description", "socialDescription", "date", "created", "modified",
    "updated", "published",
)

_STRUCTURAL_FIELDS: frozenset[str] = frozenset(_STRUCTURAL_FIELD_ORDER)

# Array fields: JSON value is a sorted string array (or null if absent).
_ARRAY_FIELDS: frozenset[str] = frozenset(("tags", "aliases", "cssclasses"))

# Boolean fields: JSON value is bool (or null if absent).
_BOOL_FIELDS: frozenset[str] = frozenset(("draft", "publish", "enableToc", "comments"))

# Date fields: JSON value is iso8601 string (or null if absent).
_DATE_FIELDS: frozenset[str] = frozenset(
    ("date", "created", "modified", "updated", "published")
)

# ---------------------------------------------------------------------------
# Regex patterns for body parsing
# ---------------------------------------------------------------------------

# Non-transclusion wikilinks: [[target]] or [[target|alias]].
# Negative lookbehind for ! excludes transclusions.
# Group 1 = raw target text (including #anchor if present).
_WIKILINK_RE: re.Pattern[str] = re.compile(r"(?<!!)\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]")

# Transclusions: ![[target]] including anchors (#Heading, #^block).
# Group 1 = raw target text.
_TRANSCLUSION_RE: re.Pattern[str] = re.compile(r"!\[\[([^\[\]]+?)\]\]")

# Block-ref IDs DEFINED in body: ^blockid at end of line.
# Negative lookbehind for [ and # to avoid matching inside wikilinks.
_BLOCKREF_DEF_RE: re.Pattern[str] = re.compile(
    r"(?<![\[#])\^([A-Za-z0-9][A-Za-z0-9-]*)(?=\s*$)", re.MULTILINE
)

# ATX headings: # text (with optional trailing ##).
_HEADING_RE: re.Pattern[str] = re.compile(
    r"^#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?$", re.MULTILINE
)

# Inline body tags: #tagname preceded by whitespace or start of line.
# Excludes headings (which also start with #) because headings have space AFTER #.
# This matches Obsidian-style inline tags.
_INLINE_TAG_RE: re.Pattern[str] = re.compile(
    r"(?:^|(?<=\s))#([A-Za-zÀ-ɏͰ-Ͽ一-龥_]"
    r"[^\s#,;!@$%^&*()\[\]{}'\"<>?/\\|`]*)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Public exceptions + types
# ---------------------------------------------------------------------------


class ManifestError(BrainError):
    """Raised on manifest read/parse failures or version mismatches."""


@dataclass(frozen=True)
class SlugEntry:
    """Per-slug record stored in the manifest."""

    fingerprint: str
    output_path: str
    source_path: str


@dataclass(frozen=True)
class Manifest:
    """Deserialised fastpath ``manifest.json``."""

    version: int
    parent_build_id: str
    built_at_ms: int
    slugs: dict[str, SlugEntry]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_manifest(dir: Path) -> Manifest:
    """Read + parse ``manifest.json`` from ``dir``.

    Raises :class:`ManifestError` on missing file, JSON parse error, or
    ``version`` mismatch.
    """
    path = dir / "manifest.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest at {path}: {exc}") from exc
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"malformed manifest JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(
            f"manifest root must be a JSON object, got {type(data).__name__}"
        )
    version = data.get("version")
    if version != FINGERPRINT_VERSION:
        raise ManifestError(
            f"manifest version {version!r} != current FINGERPRINT_VERSION "
            f"{FINGERPRINT_VERSION}"
        )
    try:
        slugs: dict[str, SlugEntry] = {
            k: SlugEntry(
                fingerprint=str(v["fingerprint"]),
                output_path=str(v["output_path"]),
                source_path=str(v["source_path"]),
            )
            for k, v in data.get("slugs", {}).items()
        }
    except (KeyError, TypeError) as exc:
        raise ManifestError(f"malformed manifest slugs at {path}: {exc}") from exc
    return Manifest(
        version=int(data["version"]),
        parent_build_id=str(data.get("parent_build_id", "")),
        built_at_ms=int(data.get("built_at_ms", 0)),
        slugs=slugs,
    )


def compute_fingerprint(
    *,
    source_bytes: bytes,
    slug: str,
    source_path: str,
    output_path: str,
    ignore_rules: IgnoreRules = DEFAULT_IGNORE_RULES,
) -> str:
    """Compute the structural fingerprint for a vault file.

    Decodes ``source_bytes`` (UTF-8), parses YAML frontmatter, validates all
    keys against the structural and ignored lists, extracts wikilinks /
    transclusions / block-refs / heading anchors from the body, builds the
    canonical blob per the fingerprint spec, and returns sha256 hex.

    ``slug`` and ``output_path`` are caller-supplied because the classifier
    (T3) reads them directly from the manifest entry it is validating — the
    manifest already stores both values from the full build, so the classifier
    never needs to re-implement Quartz's ``slugifyFilePath()`` or
    output-path derivation.

    Raises :class:`ManifestError` on:
    - ``source_bytes`` not valid UTF-8
    - YAML frontmatter parse error
    - Unknown frontmatter field (not in structural or Appendix A ignored list)
    - Any other unrecoverable input issue
    """
    blob = _compute_canonical_blob(
        source_bytes=source_bytes,
        slug=slug,
        source_path=source_path,
        output_path=output_path,
        ignore_rules=ignore_rules,
    )
    return hashlib.sha256(blob).hexdigest()


def compute_fingerprint_with_blob(
    *,
    source_bytes: bytes,
    slug: str,
    source_path: str,
    output_path: str,
    ignore_rules: IgnoreRules = DEFAULT_IGNORE_RULES,
) -> tuple[str, bytes]:
    """Like :func:`compute_fingerprint` but also returns the raw canonical blob.

    Intended for debugging parity failures — the blob hex lets maintainers
    diff TS and Python byte-by-byte to identify which section diverges.
    """
    blob = _compute_canonical_blob(
        source_bytes=source_bytes,
        slug=slug,
        source_path=source_path,
        output_path=output_path,
        ignore_rules=ignore_rules,
    )
    return hashlib.sha256(blob).hexdigest(), blob


# ---------------------------------------------------------------------------
# Private helpers — shared computation core
# ---------------------------------------------------------------------------


def _compute_canonical_blob(
    *,
    source_bytes: bytes,
    slug: str,
    source_path: str,
    output_path: str,
    ignore_rules: IgnoreRules,
) -> bytes:
    """Parse ``source_bytes``, validate frontmatter, build and return the blob.

    Shared by :func:`compute_fingerprint` and :func:`compute_fingerprint_with_blob`.
    """
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"source bytes are not valid UTF-8: {exc}") from exc

    try:
        fields, body = parse_frontmatter(source)
    except (yaml.YAMLError, ValueError) as exc:
        raise ManifestError(f"YAML frontmatter parse error: {exc}") from exc

    # Validate all frontmatter keys — unknown keys force non-trivial.
    for key in fields:
        if key not in _STRUCTURAL_FIELDS and not ignore_rules.matches(key):
            raise ManifestError(
                f"unknown frontmatter field {key!r} — not structural and not "
                f"ignored (rules from: {ignore_rules.origin}). If changing it "
                f"cannot change rendered HTML, add a line {key!r} (or a glob "
                f"covering it) to <vault>/{IGNORE_FILE_NAME}; otherwise it "
                "belongs in the structural list, which bumps FINGERPRINT_VERSION."
            )

    # SECTION_FRONTMATTER: structural fields + YAML-only tags.
    fm_json = _build_frontmatter_json(fields)

    # SECTION_TAGS: merged YAML tags + inline body tags, sorted, deduped.
    yaml_tags = _get_str_list(fields.get("tags"))
    inline_tags = _extract_inline_tags(body)
    merged_tags = sorted(set(yaml_tags) | set(inline_tags))
    tags_str = "\n".join(merged_tags)

    # SECTION_WIKILINKS
    wikilinks_str = "\n".join(sorted(set(_extract_wikilinks(body))))

    # SECTION_TRANSCLUSIONS
    transclusions_str = "\n".join(sorted(set(_extract_transclusions(body))))

    # SECTION_BLOCK_REFS
    block_refs_str = "\n".join(sorted(set(_extract_block_refs(body))))

    # SECTION_HEADING_ANCHORS (document order — NOT sorted)
    headings_str = "\n".join(_extract_heading_anchors(body))

    return _build_canonical_blob(
        slug=slug,
        source_path=source_path,
        output_path=output_path,
        frontmatter_json=fm_json,
        tags_str=tags_str,
        wikilinks_str=wikilinks_str,
        transclusions_str=transclusions_str,
        block_refs_str=block_refs_str,
        headings_str=headings_str,
    )


# ---------------------------------------------------------------------------
# Private helpers — canonical blob encoding
# ---------------------------------------------------------------------------


def _u32be(n: int) -> bytes:
    return struct.pack(">I", n)


def _encode_section(s: str) -> bytes:
    encoded = s.encode("utf-8")
    return _u32be(len(encoded)) + encoded


def _build_canonical_blob(
    *,
    slug: str,
    source_path: str,
    output_path: str,
    frontmatter_json: str,
    tags_str: str,
    wikilinks_str: str,
    transclusions_str: str,
    block_refs_str: str,
    headings_str: str,
) -> bytes:
    """Assemble the 10-section canonical blob per the fingerprint spec."""
    return b"".join([
        _u32be(FINGERPRINT_VERSION),           # SECTION_VERSION: just u32be, no length prefix
        _encode_section(slug),                 # SECTION_SLUG
        _encode_section(source_path),          # SECTION_SOURCE_PATH
        _encode_section(output_path),          # SECTION_OUTPUT_PATH
        _encode_section(frontmatter_json),     # SECTION_FRONTMATTER
        _encode_section(tags_str),             # SECTION_TAGS
        _encode_section(wikilinks_str),        # SECTION_WIKILINKS
        _encode_section(transclusions_str),    # SECTION_TRANSCLUSIONS
        _encode_section(block_refs_str),       # SECTION_BLOCK_REFS
        _encode_section(headings_str),         # SECTION_HEADING_ANCHORS
    ])


# ---------------------------------------------------------------------------
# Private helpers — frontmatter JSON blob
# ---------------------------------------------------------------------------


def _normalize_date_val(val: Any) -> str | None:
    """Serialize a YAML date/datetime/string to ISO 8601, or None.

    Midnight ``datetime.datetime`` objects are truncated to date-only to match
    TS ``_normalizeDateVal`` behaviour: js-yaml parses bare YAML date scalars
    (e.g. ``date: 2024-03-15``) as UTC-midnight ``Date`` objects, which the TS
    side normalises back to a ``"YYYY-MM-DD"`` string.  Python pyyaml produces
    ``datetime.date`` for bare dates (already date-only) and
    ``datetime.datetime`` for bare date-times; truncating the midnight case
    keeps both sides byte-identical.

    Examples::

        datetime.date(2024, 3, 15)              → "2024-03-15"
        datetime.datetime(2024, 3, 15, 0, 0, 0) → "2024-03-15"   # midnight truncated
        datetime.datetime(2024, 3, 15, 12, 0, 0)→ "2024-03-15T12:00:00"
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        # Truncate midnight datetimes to date-only, matching TS _normalizeDateVal.
        if (
            val.hour == 0
            and val.minute == 0
            and val.second == 0
            and val.microsecond == 0
        ):
            return val.date().isoformat()
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, str):
        return val
    return str(val)


def _get_str_list(val: Any) -> list[str]:
    """Coerce a YAML value to a list of strings."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x is not None]
    if isinstance(val, str):
        return [val]
    return []


def _build_frontmatter_json(fields: dict[str, Any]) -> str:
    """Build deterministic structural frontmatter JSON (no whitespace, exact key order).

    Encodes YAML-only tags (not merged with inline body tags) per the spec.
    """
    obj: dict[str, Any] = {}
    for key in _STRUCTURAL_FIELD_ORDER:
        if key in _ARRAY_FIELDS:
            if key not in fields or fields[key] is None:
                obj[key] = None
            else:
                obj[key] = sorted(_get_str_list(fields[key]))
        elif key in _BOOL_FIELDS:
            val = fields.get(key)
            obj[key] = bool(val) if val is not None else None
        elif key in _DATE_FIELDS:
            obj[key] = _normalize_date_val(fields.get(key))
        else:
            val = fields.get(key)
            obj[key] = str(val) if val is not None else None
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Private helpers — body parsing
# ---------------------------------------------------------------------------


def _extract_inline_tags(body: str) -> list[str]:
    """Extract inline ``#tags`` from body text (Obsidian-style)."""
    return [m.group(1) for m in _INLINE_TAG_RE.finditer(body)]


def _extract_wikilinks(body: str) -> list[str]:
    """Extract non-transclusion wikilink targets (raw text, including anchors)."""
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(body) if m.group(1).strip()]


def _extract_transclusions(body: str) -> list[str]:
    """Extract transclusion targets (``![[...]]``, including anchors)."""
    return [m.group(1).strip() for m in _TRANSCLUSION_RE.finditer(body) if m.group(1).strip()]


def _extract_block_refs(body: str) -> list[str]:
    """Extract block-ref IDs defined in body (``^blockid`` at end of line)."""
    return [m.group(1) for m in _BLOCKREF_DEF_RE.finditer(body)]


def _extract_heading_anchors(body: str) -> list[str]:
    """Return heading anchors in document order using github-slugger semantics."""
    slugger = Slugger()
    return [slugger.slug(m.group(1).strip()) for m in _HEADING_RE.finditer(body)]
