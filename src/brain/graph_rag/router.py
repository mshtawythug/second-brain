"""Pure heuristic auto-router for graph retrieval (spec §6d / §17b decisions 3-4).

:func:`route` is a **pure, deterministic, DB-free** function: given the query,
the requested ``mode``, an optional explicit ``person``, and a pre-fetched list
of :class:`KnownPerson` candidates, it decides which retrieval path
:func:`brain.graph_rag.retrieve.graph_rag_search` must dispatch to. Keeping it
pure means its full branch matrix is unit-testable without a database — the
caller resolves the DB-derived inputs (the known person entities) and passes
them in (dependency inversion).

The rule set (spec §17b decision 3; §17c Q6 — the G3-e flip; §17d Q1 — fuse):

1. **Explicit modes are honored** — including an explicit ``global`` request,
   which now returns :data:`GLOBAL_MODE` (community summaries shipped in G3, so
   the router dispatches to them; the G2 rejection is gone — spec §17c Q6), and
   an explicit ``fuse`` request (:data:`FUSE_MODE`; wave G4-c, spec §17d Q1).
   ``fuse`` is honored ONLY as an explicit request — it is **never** an
   auto-routed target (the auto branches stay local/themes/global).
2. **Thematic intent** is detected by a **closed regex grammar** (not an
   open-ended keyword list): the normalized query matches
   ``\\bthemes?\\b|\\btopics?\\b|\\bpatterns?\\b|\\btrends?\\b|\\brecurring\\b``
   **OR** ``\\bhow\\s+(has|have|did|does)\\b.{0,80}\\b(evolve|evolved|change|
   changed|shift|shifted)\\b`` (case-insensitive).
3. **Person resolution precedence:** an explicit ``person`` argument first;
   otherwise a token-boundary scan of the ``known_persons`` against the query.
4. **Query-match tie-break** among scanned persons: longest matched span →
   highest ``doc_count`` → lexicographically smallest ``canonical_key``.
5. **Branches (auto):** ``thematic AND person → themes``;
   ``thematic AND no person → global`` (the real global community path now — G3-e
   flipped the former G2 ``global→local`` degradation off); else → ``local``.

**G2 degradation machinery is KEEP-DORMANT (spec §17c Q6):** the
:data:`DEGRADED_FROM_GLOBAL` / :data:`DEGRADATION_REASON_G2` constants, the
:class:`RoutingDecision` ``degraded_from`` / ``degradation_reason`` fields, and
:class:`~brain.errors.GraphModeUnavailable` all **stay defined** for wire
stability, but G3 **never populates / raises** them — every path below leaves the
degradation fields ``None`` and no path raises ``GraphModeUnavailable``.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "AUTO_MODE",
    "DEGRADATION_REASON_G2",
    "DEGRADED_FROM_GLOBAL",
    "FUSE_MODE",
    "GLOBAL_MODE",
    "LOCAL_MODE",
    "THEMES_MODE",
    "KnownPerson",
    "RoutedPerson",
    "RoutingDecision",
    "route",
]

# The retrieval-mode vocabulary (canonical home; re-exported by retrieve.py /
# the package __init__ so existing ``LOCAL_MODE`` / ``THEMES_MODE`` imports keep
# working). ``auto`` triggers this router; ``global`` is the community path the
# router now dispatches to for an explicit request AND the auto
# thematic-no-person branch (the G3-e flip; spec §17c Q6).
LOCAL_MODE = "local"
THEMES_MODE = "themes"
GLOBAL_MODE = "global"
AUTO_MODE = "auto"
# Fuse (wave G4-c; spec §17d Q1): RRF of the local-graph doc leg with the
# vector/FTS hybrid doc leg. Honored ONLY as an explicit request — the auto
# router never targets it (auto stays local/themes/global).
FUSE_MODE = "fuse"

# Degradation signals (spec §17b decision 4) — KEEP-DORMANT after the G3-e flip
# (spec §17c Q6): retained for wire stability but no longer stamped onto any
# ``GraphContext`` / ``RoutingDecision`` (the auto thematic-no-person branch now
# routes to the real GLOBAL_MODE instead of degrading to local).
DEGRADED_FROM_GLOBAL = "global"
DEGRADATION_REASON_G2 = "global_unavailable_g2"

# Closed thematic-intent grammar (spec §17b decision 3 — NOT an open keyword
# list). Compiled once at import; case-insensitive.
_THEMATIC_KEYWORDS = re.compile(
    r"\bthemes?\b|\btopics?\b|\bpatterns?\b|\btrends?\b|\brecurring\b",
    re.IGNORECASE,
)
_THEMATIC_EVOLUTION = re.compile(
    r"\bhow\s+(has|have|did|does)\b.{0,80}\b"
    r"(evolve|evolved|change|changed|shift|shifted)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class KnownPerson:
    """A candidate person entity for the router's token-boundary scan.

    Pre-fetched (tenant-scoped) by the caller from ``graph_entities`` so the
    router stays DB-free. ``canonical_key`` is the lowercased People-Hub display
    name (the dedup key + lexicographic tie-break), ``display_name`` the entity's
    stored name, and ``doc_count`` the derived mention count (the doc-count
    tie-break input).
    """

    canonical_key: str
    display_name: str
    doc_count: int = 0


@dataclass(frozen=True)
class RoutedPerson:
    """The person a query routed to (spec §17b decision 3).

    ``source`` is ``"explicit"`` (from the ``--person`` / MCP ``person`` arg) or
    ``"scanned"`` (a token-boundary match in the query). For the explicit case
    ``canonical_key`` / ``doc_count`` are unknown (``None``) — only the caller's
    stripped person string is carried in ``display_name``. For the scanned case
    all three mirror the winning :class:`KnownPerson`.
    """

    display_name: str
    source: str
    canonical_key: str | None = None
    doc_count: int | None = None


@dataclass(frozen=True)
class RoutingDecision:
    """The pure router's verdict (spec §6d / §17b decisions 3-4).

    ``executed_mode`` is the mode the caller must dispatch to —
    :data:`LOCAL_MODE`, :data:`THEMES_MODE`, or :data:`GLOBAL_MODE` (the G3-e flip
    made ``global`` dispatchable for explicit ``mode='global'`` and the auto
    thematic-no-person branch). ``requested_mode`` echoes the caller's input mode.
    ``is_thematic`` records the regex-grammar verdict. ``resolved_person`` is the
    person the query routed to (or ``None``). ``degraded_from`` /
    ``degradation_reason`` are **KEEP-DORMANT** (spec §17c Q6): defined for wire
    stability but never populated by G3 — always ``None``.
    """

    executed_mode: str
    requested_mode: str
    is_thematic: bool
    resolved_person: RoutedPerson | None = None
    degraded_from: str | None = None
    degradation_reason: str | None = None


def route(
    query: str,
    *,
    mode: str,
    person: str | None,
    known_persons: Sequence[KnownPerson],
) -> RoutingDecision:
    """Decide the retrieval mode for one query (pure / deterministic).

    See the module docstring for the full rule set. ``known_persons`` is only
    consulted for the ``auto`` thematic-person scan; explicit modes ignore it.
    Never raises :class:`~brain.errors.GraphModeUnavailable` — the G3-e flip made
    ``global`` a dispatchable mode (spec §17c Q6).

    Raises:
        ValueError: an unrecognized ``mode`` (caller bug).
    """
    if mode != AUTO_MODE:
        return _route_explicit(query, mode, person)
    return _route_auto(query, person, known_persons)


def _route_explicit(query: str, mode: str, person: str | None) -> RoutingDecision:
    """Honor an explicit (non-``auto``) mode, ``global`` / ``fuse`` included.

    The G2 ``global`` rejection is gone: an explicit ``mode='global'`` now returns
    a :data:`GLOBAL_MODE` decision (the caller dispatches to the community path).
    An explicit ``mode='fuse'`` returns a :data:`FUSE_MODE` decision (wave G4-c,
    spec §17d Q1 — the caller fuses the graph + hybrid doc legs). Local / themes
    are honored unchanged; an unknown mode is still a caller bug.
    """
    if mode not in (LOCAL_MODE, THEMES_MODE, GLOBAL_MODE, FUSE_MODE):
        raise ValueError(
            f"unknown graph retrieval mode {mode!r} (expected one of "
            f"{AUTO_MODE!r} / {LOCAL_MODE!r} / {THEMES_MODE!r} / "
            f"{GLOBAL_MODE!r} / {FUSE_MODE!r})"
        )
    return RoutingDecision(
        executed_mode=mode,
        requested_mode=mode,
        is_thematic=_is_thematic(query),
        resolved_person=_explicit_person(person),
    )


def _route_auto(
    query: str, person: str | None, known_persons: Sequence[KnownPerson]
) -> RoutingDecision:
    """Run the heuristic branches for ``mode='auto'`` (spec dec. 3; §17c Q6 flip)."""
    is_thematic = _is_thematic(query)
    resolved = _explicit_person(person) or _scan_person(query, known_persons)

    if is_thematic and resolved is not None:
        return RoutingDecision(
            executed_mode=THEMES_MODE,
            requested_mode=AUTO_MODE,
            is_thematic=True,
            resolved_person=resolved,
        )
    if is_thematic:
        # Thematic but no resolvable person → global (the real community path).
        # G3-e flipped this off the former G2 ``global→local`` degradation: no
        # degradation signals are stamped (spec §17c Q6 keeps them dormant).
        return RoutingDecision(
            executed_mode=GLOBAL_MODE,
            requested_mode=AUTO_MODE,
            is_thematic=True,
            resolved_person=None,
        )
    return RoutingDecision(
        executed_mode=LOCAL_MODE,
        requested_mode=AUTO_MODE,
        is_thematic=False,
        resolved_person=resolved,
    )


def _is_thematic(query: str) -> bool:
    """``True`` iff the query matches the closed thematic-intent grammar."""
    if not query:
        return False
    return bool(_THEMATIC_KEYWORDS.search(query) or _THEMATIC_EVOLUTION.search(query))


def _explicit_person(person: str | None) -> RoutedPerson | None:
    """Wrap a non-blank explicit ``person`` arg as the resolved person."""
    if person is None:
        return None
    stripped = person.strip()
    if not stripped:
        return None
    return RoutedPerson(display_name=stripped, source="explicit")


def _scan_person(
    query: str, known_persons: Sequence[KnownPerson]
) -> RoutedPerson | None:
    """Token-boundary scan of ``known_persons`` against the query (spec dec. 3-4).

    For each known person, match its ``canonical_key`` and ``display_name``
    case-insensitively at word boundaries; the person's matched span is the
    longest matching key. The winner is chosen by longest matched span → highest
    ``doc_count`` → lexicographically smallest ``canonical_key`` (the
    ``canonical_key`` is unique per tenant, so the ordering is total and the
    result deterministic). Returns ``None`` when nothing matches.
    """
    if not query or not known_persons:
        return None
    lowered = query.lower()
    best: tuple[int, int, str] | None = None
    winner: KnownPerson | None = None
    for candidate in known_persons:
        span = _best_match_span(lowered, candidate)
        if span <= 0:
            continue
        # Order key: maximize span, then doc_count, then minimize canonical_key.
        key = (-span, -candidate.doc_count, candidate.canonical_key)
        if best is None or key < best:
            best = key
            winner = candidate
    if winner is None:
        return None
    return RoutedPerson(
        display_name=winner.display_name,
        source="scanned",
        canonical_key=winner.canonical_key,
        doc_count=winner.doc_count,
    )


def _best_match_span(lowered_query: str, candidate: KnownPerson) -> int:
    """Longest word-boundary match length of a person's keys (0 = no match)."""
    best = 0
    for needle in (candidate.canonical_key, candidate.display_name):
        normalized = (needle or "").strip().lower()
        if not normalized:
            continue
        pattern = r"\b" + re.escape(normalized) + r"\b"
        if re.search(pattern, lowered_query):
            best = max(best, len(normalized))
    return best
