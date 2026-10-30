"""Tag mapping engine: synonym mappings, the per-document rewrite loop, and merge suggestions.

RM-21 moved this out of ``cli.py`` (``brain backfill normalize-tags``: ``_load_tag_mapping``,
``_apply_tag_mapping`` and the command's per-document loop) so that ``brain tags rename`` and
``brain tags merge`` (:mod:`brain.cli_tags`) run the SAME loop instead of a copy of it.

**The loop** (:func:`run_mapping`) is per document and idempotent, in the caller's autocommit
connection. For each document whose mapped, normalized tags differ, it issues one ``UPDATE
documents SET tags``, calls :func:`~brain.queries.sync_chunk_search_metadata` (which refreshes
``chunks.tags_text``, the tag text search reads since migration 009) and, when the document has a
``vault_path``, rewrites that file's frontmatter with
:func:`~brain.vault.frontmatter.rewrite_tags`. There is deliberately no single transaction
(RM-21.D2): a crash part-way leaves some documents done, and a re-run finishes the rest. The
``UPDATE`` does not touch ``updated_at``: tag maintenance is not new knowledge (migration 025).

**A bad file fails that document's file write, not the run.** The file path is checked to stay
inside the vault (:func:`~brain.vault.paths.assert_within_vault`), and an unreadable file, bad
frontmatter or an escaping path is counted in :attr:`MappingReport.files_failed` and passed to
``on_file_error``, and the loop goes on. The row was already updated, so a re-run will NOT retry
that file: the caller reports it so the owner can fix it.

**Saved searches** (:func:`rewrite_watch_filters`) keep matching after a rename or merge: a watch
whose ``tag`` or ``without_tag`` filter names a mapped tag is rewritten to the new tag.

**Suggestions** (:func:`suggest_merges`) are pure: pairs of tags whose spelling is close
(:class:`difflib.SequenceMatcher` ratio ≥ :data:`SIMILARITY_THRESHOLD`), singular/plural pairs,
and pairs whose document sets overlap (:func:`~brain.set_similarity.jaccard` ≥
:data:`JACCARD_THRESHOLD`, on at least :data:`MIN_SHARED_DOCS` shared documents), ranked, each
with its evidence.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any

import psycopg
import yaml

from .errors import BrainError, VaultPathEscape
from .queries import sync_chunk_search_metadata
from .set_similarity import jaccard
from .tags import normalize_tag, normalize_tags
from .vault.frontmatter import rewrite_tags
from .vault.paths import assert_within_vault

__all__ = [
    "JACCARD_THRESHOLD",
    "MIN_SHARED_DOCS",
    "SIMILARITY_THRESHOLD",
    "FILE_ERRORS",
    "MappingReport",
    "MergeSuggestion",
    "TagChange",
    "TagMappingError",
    "WatchFilterChange",
    "apply_tag_mapping",
    "build_merge_mapping",
    "build_rename_mapping",
    "load_tag_mapping",
    "parse_tag_mapping",
    "read_document_tags",
    "rewrite_watch_filters",
    "run_mapping",
    "suggest_merges",
    "tag_counts",
]

#: A pair of tags whose ``SequenceMatcher`` ratio reaches this is "similar spelling".
SIMILARITY_THRESHOLD = 0.85
#: A pair of tags whose document sets reach this Jaccard overlap "co-occur".
JACCARD_THRESHOLD = 0.8
#: The co-occurrence rule needs this many shared documents. Without it, every pair of tags that
#: appear once, on the same document, overlaps at 1.0, and the list is noise.
MIN_SHARED_DOCS = 2

#: The filter keys of a saved search (migration 029) that hold a tag.
_WATCH_TAG_KEYS = ("tag", "without_tag")


class TagMappingError(BrainError):
    """A mapping file or a rename/merge request is unusable; the message says why."""


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------


def parse_tag_mapping(raw: str) -> dict[str, str]:
    """Parse and validate a ``{from: to}`` JSON mapping.

    Keys and values pass through :func:`normalize_tag`, so the JSON may use any casing or
    separator (``{"Recruiters": "Recruiter"}`` becomes ``recruiters → recruiter``). An entry
    whose key or value normalizes to empty is dropped.

    Raises:
        TagMappingError: malformed JSON, a non-object payload, or a non-string entry.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TagMappingError(f"mapping file is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise TagMappingError("mapping file must be a JSON object of {from: to} strings")
    cleaned: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise TagMappingError(
                "mapping entries must be strings; "
                f"got {type(k).__name__} → {type(v).__name__}"
            )
        canonical_from = normalize_tag(k)
        canonical_to = normalize_tag(v)
        if not canonical_from or not canonical_to:
            continue
        cleaned[canonical_from] = canonical_to
    return cleaned


def load_tag_mapping(path: Path) -> dict[str, str]:
    """Read ``path`` and :func:`parse_tag_mapping` it.

    Raises:
        TagMappingError: an unreadable file, or anything :func:`parse_tag_mapping` refuses.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise TagMappingError(f"could not read mapping file: {e}") from e
    return parse_tag_mapping(raw)


def apply_tag_mapping(tags: Iterable[str], mapping: Mapping[str, str]) -> list[str]:
    """Map each tag (canonicalized first, so the lookup ignores case and separators).

    Tags with no entry come back canonicalized. The caller still runs the result through
    :func:`normalize_tags` for dedupe and empty-drop.
    """
    out: list[str] = []
    for tag in tags:
        canonical = normalize_tag(tag)
        out.append(mapping.get(canonical, canonical))
    return out


def build_rename_mapping(old: str, new: str) -> dict[str, str]:
    """``{old: new}``, both normalized.

    Raises:
        TagMappingError: either tag is empty after normalizing, or they are the same tag.
    """
    old_n, new_n = normalize_tag(old), normalize_tag(new)
    if not old_n or not new_n:
        raise TagMappingError("tags must not be empty")
    if old_n == new_n:
        raise TagMappingError(f"{old!r} and {new!r} are the same tag ({old_n!r})")
    return {old_n: new_n}


def build_merge_mapping(sources: Sequence[str], into: str) -> dict[str, str]:
    """``{source: into}`` for every source other than ``into``, all normalized.

    ``into`` may be one of the sources (``merge a b --into a``); it maps to itself and is left
    out. Repeated sources collapse.

    Raises:
        TagMappingError: an empty tag, or no source that differs from ``into``.
    """
    target = normalize_tag(into)
    normalized = [normalize_tag(s) for s in sources]
    if not target or not all(normalized):
        raise TagMappingError("tags must not be empty")
    mapping = {s: target for s in dict.fromkeys(normalized) if s != target}
    if not mapping:
        raise TagMappingError(f"nothing to merge: every source is already {target!r}")
    return mapping


# ---------------------------------------------------------------------------
# The per-document loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TagChange:
    """One document whose tags the mapping changes."""

    doc_id: str
    title: str
    before: list[str]
    after: list[str]
    vault_path: str | None


@dataclass(frozen=True)
class MappingReport:
    """What :func:`run_mapping` did (or, on a dry run, would do) to documents."""

    changed: int
    files_rewritten: int
    files_missing: int
    unchanged: int
    #: Documents whose row was updated but whose file could not be rewritten (``on_file_error``).
    files_failed: int = 0


#: What a file rewrite can raise for one bad file: unreadable (``OSError``), not UTF-8, bad YAML,
#: frontmatter that is not a mapping (``ValueError``), or a ``vault_path`` outside the vault.
FILE_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    UnicodeDecodeError,
    yaml.YAMLError,
    ValueError,
    VaultPathEscape,
)


_TAGGED_DOCS_SQL = (
    "SELECT id::text, title, tags, vault_path "
    "FROM documents "
    "WHERE tags IS NOT NULL AND array_length(tags, 1) > 0 "
    "ORDER BY id"
)


def _write_row(conn: psycopg.Connection[Any], change: TagChange) -> None:
    """Write one change's row and its chunks."""
    conn.execute("UPDATE documents SET tags = %s WHERE id = %s", (change.after, change.doc_id))
    # Migration 009 denormalizes documents.tags onto chunks.tags_text so the weighted tsv
    # reflects tag changes. This loop is a full replace (not apply_tags' add/remove diff), so
    # the chunk sync happens here. The helper's IS DISTINCT FROM guards make it a no-op when
    # only the order changed.
    sync_chunk_search_metadata(conn, change.doc_id)


def _write_file(change: TagChange, vault_root: Path) -> bool | None:
    """Rewrite one change's file.

    Returns ``True`` when the file was rewritten, ``False`` when it was already in sync or the
    document has no file, and ``None`` when ``vault_path`` names a file that is not on disk.

    Raises:
        VaultPathEscape: ``vault_path`` resolves outside ``vault_root`` (checked before any
            read or write), or anything else in :data:`FILE_ERRORS` for a bad file.
    """
    if change.vault_path is None:
        return False
    abs_path = vault_root / change.vault_path
    assert_within_vault(abs_path, vault_root)
    if not abs_path.exists():
        return None
    return rewrite_tags(abs_path, change.after)


def run_mapping(
    conn: psycopg.Connection[Any],
    mapping: Mapping[str, str],
    *,
    vault_root: Path,
    apply: bool,
    only_mapped: bool = False,
    on_change: Callable[[TagChange], None] | None = None,
    on_missing_file: Callable[[TagChange], None] | None = None,
    on_file_error: Callable[[TagChange, Exception], None] | None = None,
) -> MappingReport:
    """Map and normalize every tagged document's tags; write the ones that change if ``apply``.

    ``conn`` should be in autocommit, so each document's writes commit on their own (the
    module docstring says why). ``only_mapped`` restricts the walk to documents that carry a
    tag the mapping names, so a rename touches nothing else; without it every document is
    normalized too (``brain backfill normalize-tags``). ``on_change`` is called for each
    changed document, before its writes, and ``on_missing_file`` for each one whose
    ``vault_path`` is not on disk (only when applying). ``on_file_error`` is called with the
    exception for each document whose file could not be rewritten (:data:`FILE_ERRORS`); the
    row stays updated and the loop goes on. A document skipped by ``only_mapped`` is not
    counted.
    """
    changed = files_rewritten = files_missing = unchanged = files_failed = 0
    for doc_id, title, current_tags, vault_path in conn.execute(_TAGGED_DOCS_SQL).fetchall():
        current = list(current_tags or [])
        if only_mapped and not any(normalize_tag(tag) in mapping for tag in current):
            continue
        new_tags = normalize_tags(apply_tag_mapping(current, mapping))
        if new_tags == current:
            unchanged += 1
            continue
        change = TagChange(doc_id, title, current, new_tags, vault_path)
        changed += 1
        if on_change is not None:
            on_change(change)
        if not apply:
            continue
        _write_row(conn, change)
        try:
            outcome = _write_file(change, vault_root)
        except FILE_ERRORS as exc:
            files_failed += 1
            if on_file_error is not None:
                on_file_error(change, exc)
            continue
        if outcome is None:
            files_missing += 1
            if on_missing_file is not None:
                on_missing_file(change)
        elif outcome:
            files_rewritten += 1
    return MappingReport(changed, files_rewritten, files_missing, unchanged, files_failed)


# ---------------------------------------------------------------------------
# Saved searches
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchFilterChange:
    """One saved-search filter a mapping rewrites."""

    watch: str
    key: str
    before: str
    after: str


def _watches_present(conn: psycopg.Connection[Any]) -> bool:
    row = conn.execute("SELECT to_regclass('saved_searches') IS NOT NULL").fetchone()
    return bool(row and row[0])


def rewrite_watch_filters(
    conn: psycopg.Connection[Any], mapping: Mapping[str, str], *, apply: bool
) -> list[WatchFilterChange]:
    """Rewrite every saved search whose ``tag`` / ``without_tag`` filter names a mapped tag.

    Returns the changes (made, or on a dry run planned), by watch name. Each watch is one
    ``UPDATE`` guarded on the filters it read, so a re-run is a no-op and a concurrent edit is
    not overwritten. Other filter keys, including ones this version does not know, are kept.
    Without migration 029 there is nothing to rewrite.
    """
    if not _watches_present(conn):
        return []
    rows = conn.execute("SELECT id::text, name, filters FROM saved_searches ORDER BY name")
    changes: list[WatchFilterChange] = []
    for watch_id, name, filters in rows.fetchall():
        current: dict[str, Any] = dict(filters or {})
        updated = dict(current)
        for key in _WATCH_TAG_KEYS:
            value = current.get(key)
            if not isinstance(value, str):
                continue
            target = mapping.get(normalize_tag(value))
            if target is not None and target != value:
                updated[key] = target
                changes.append(WatchFilterChange(name, key, value, target))
        if apply and updated != current:
            conn.execute(
                "UPDATE saved_searches SET filters = %s::jsonb "
                "WHERE id = %s AND filters = %s::jsonb",
                (json.dumps(updated), watch_id, json.dumps(current)),
            )
    return changes


# ---------------------------------------------------------------------------
# Reads: counts and the document → tags map
# ---------------------------------------------------------------------------


def tag_counts(
    conn: psycopg.Connection[Any], *, sensitivity: str | None = None
) -> list[tuple[str, int]]:
    """Every tag and the number of documents carrying it, most used first, then by name.

    ``sensitivity`` (``"normal"`` from the MCP lens) restricts the count to that tier, in the
    predicate, so a tag found only on withheld documents is not named at all.
    """
    rows = conn.execute(
        "SELECT t.tag, count(DISTINCT d.id)::int "
        "FROM documents d CROSS JOIN LATERAL unnest(d.tags) AS t(tag) "
        "WHERE (%s::text IS NULL OR d.sensitivity = %s) "
        "GROUP BY t.tag ORDER BY 2 DESC, 1",
        (sensitivity, sensitivity),
    ).fetchall()
    return [(str(tag), int(count)) for tag, count in rows]


def read_document_tags(
    conn: psycopg.Connection[Any], *, sensitivity: str | None = None
) -> dict[str, list[str]]:
    """``{document id: tags}`` for every tagged document, under the same lens as above."""
    rows = conn.execute(
        "SELECT id::text, tags FROM documents "
        "WHERE tags IS NOT NULL AND array_length(tags, 1) > 0 "
        "AND (%s::text IS NULL OR sensitivity = %s) ORDER BY id",
        (sensitivity, sensitivity),
    ).fetchall()
    return {str(doc_id): list(tags) for doc_id, tags in rows}


# ---------------------------------------------------------------------------
# Suggestions (pure)
# ---------------------------------------------------------------------------

REASON_SPELLING = "similar-spelling"
REASON_PLURAL = "singular-plural"
REASON_COOCCURRENCE = "co-occurrence"


@dataclass(frozen=True)
class MergeSuggestion:
    """Two tags that look like one, with the evidence.

    ``keep`` is the more used of the two (ties: the shorter, then the first by name), so the
    suggested command is ``brain tags merge <merge> --into <keep>``.
    """

    keep: str
    merge: str
    keep_count: int
    merge_count: int
    reasons: tuple[str, ...]
    ratio: float
    overlap: float
    shared: int

    def to_json(self) -> dict[str, Any]:
        """The suggestion as a JSON-ready dict, ratios rounded to 3 places."""
        return {
            "keep": self.keep,
            "merge": self.merge,
            "keep_count": self.keep_count,
            "merge_count": self.merge_count,
            "reasons": list(self.reasons),
            "ratio": round(self.ratio, 3),
            "overlap": round(self.overlap, 3),
            "shared": self.shared,
        }


def _singular_candidates(tag: str) -> list[str]:
    """English singular forms ``tag`` could be the plural of (``ies``→``y``, ``es``, ``s``)."""
    out: list[str] = []
    if tag.endswith("ies") and len(tag) > 3:
        out.append(f"{tag[:-3]}y")
    if tag.endswith(("ses", "xes", "zes", "ches", "shes")):
        out.append(tag[:-2])
    if tag.endswith("s") and not tag.endswith("ss") and len(tag) > 1:
        out.append(tag[:-1])
    return out


def _spelling_pairs(tags: Sequence[str]) -> dict[tuple[str, str], float]:
    """Pairs whose ratio reaches the threshold, with the ratio.

    **O(T²) in the number of distinct tags T** in the worst case (every pair of tags of similar
    length is compared). Sorted by length, so the length bound ``2·min/(la+lb)`` (an upper bound
    on the ratio) ends each inner loop early, and ``real_quick_ratio`` and ``quick_ratio`` prune
    before ``ratio``; a personal corpus has hundreds to a few thousand tags.
    """
    ordered = sorted(tags, key=lambda t: (len(t), t))
    out: dict[tuple[str, str], float] = {}
    matcher = SequenceMatcher(autojunk=False)
    for i, a in enumerate(ordered):
        matcher.set_seq2(a)
        for b in ordered[i + 1 :]:
            if 2 * len(a) / (len(a) + len(b)) < SIMILARITY_THRESHOLD:
                break
            matcher.set_seq1(b)
            if (
                matcher.real_quick_ratio() >= SIMILARITY_THRESHOLD
                and matcher.quick_ratio() >= SIMILARITY_THRESHOLD
            ):
                ratio = matcher.ratio()
                if ratio >= SIMILARITY_THRESHOLD:
                    out[_pair(a, b)] = ratio
    return out


def _shared_counts(doc_tags: Mapping[str, Sequence[str]]) -> Counter[tuple[str, str]]:
    """How many documents each co-occurring pair of tags shares, in ONE pass over the documents.

    Cost: the sum over documents of (tags on it)², not a set intersection per pair per document.
    Intersecting the two tags' document sets for every document carrying both cost |D|² for one
    pair on every document (20,000 such documents took about a minute).
    """
    counts: Counter[tuple[str, str]] = Counter()
    for tags in doc_tags.values():
        counts.update(combinations(sorted(set(tags)), 2))
    return counts


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def suggest_merges(
    doc_tags: Mapping[str, Sequence[str]], *, limit: int | None = None
) -> list[MergeSuggestion]:
    """Rank pairs of tags that are probably one tag, from ``{document id: tags}``.

    A pair qualifies by any of three rules, and carries every rule it meets as a reason:
    similar spelling, singular/plural, or co-occurrence. Ranked by the number of reasons, then
    by the stronger of ratio and overlap, then by combined use, then by name.
    """
    docs_by_tag: dict[str, set[str]] = defaultdict(set)
    for doc_id, tags in doc_tags.items():
        for tag in tags:
            docs_by_tag[tag].add(doc_id)
    reasons: dict[tuple[str, str], set[str]] = defaultdict(set)
    ratios = _spelling_pairs(list(docs_by_tag))
    for pair in ratios:
        reasons[pair].add(REASON_SPELLING)
    for tag in docs_by_tag:
        for singular in _singular_candidates(tag):
            if singular in docs_by_tag:
                reasons[_pair(tag, singular)].add(REASON_PLURAL)
    for (a, b), shared in _shared_counts(doc_tags).items():
        union = len(docs_by_tag[a]) + len(docs_by_tag[b]) - shared
        if shared >= MIN_SHARED_DOCS and shared / union >= JACCARD_THRESHOLD:
            reasons[(a, b)].add(REASON_COOCCURRENCE)
    suggestions = [
        _suggestion(a, b, why, docs_by_tag, ratios) for (a, b), why in reasons.items()
    ]
    suggestions.sort(
        key=lambda s: (
            -len(s.reasons),
            -max(s.ratio, s.overlap),
            -(s.keep_count + s.merge_count),
            s.keep,
            s.merge,
        )
    )
    return suggestions if limit is None else suggestions[:limit]


def _suggestion(
    a: str,
    b: str,
    why: set[str],
    docs_by_tag: Mapping[str, set[str]],
    ratios: Mapping[tuple[str, str], float],
) -> MergeSuggestion:
    keep, merge = sorted((a, b), key=lambda t: (-len(docs_by_tag[t]), len(t), t))
    order = (REASON_SPELLING, REASON_PLURAL, REASON_COOCCURRENCE)
    ratio = ratios.get((a, b))
    if ratio is None:
        ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return MergeSuggestion(
        keep=keep,
        merge=merge,
        keep_count=len(docs_by_tag[keep]),
        merge_count=len(docs_by_tag[merge]),
        reasons=tuple(r for r in order if r in why),
        ratio=ratio,
        overlap=jaccard(docs_by_tag[a], docs_by_tag[b]),
        shared=len(docs_by_tag[a] & docs_by_tag[b]),
    )
