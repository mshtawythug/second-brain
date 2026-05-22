"""Tests for ``brain.graph_rag.router`` — the pure heuristic auto-router (G2-g).

:func:`brain.graph_rag.router.route` is a pure, DB-free function, so this whole
suite needs **no database and no patching** — the DB-derived candidate persons
are passed in as :class:`KnownPerson` values (dependency inversion). It exercises
the full branch matrix of spec §17b decision 3 + the **G3-e flip** (spec §17c Q6):

* explicit modes honored (local / themes / global — global no longer rejected),
  unknown mode rejected;
* the closed thematic-intent regex grammar (positive keywords, the
  ``how has X evolved`` form, and near-miss negatives);
* the person token-boundary scan + all three tie-breaks (longest span →
  doc_count → lexicographic canonical_key) + explicit-over-scan precedence;
* the auto branches (thematic+person → themes; thematic+no-person → **global**
  — the G3-e flip, with NO degradation signals; non-thematic → local);
* the KEEP-DORMANT degradation machinery (constants + ``GraphModeUnavailable``
  stay defined but are never populated/raised — spec §17c Q6);
* determinism.

All people are synthetic (Dana Lee / Ana / Bob / Zoe …); no PII.
"""
from __future__ import annotations

import pytest

from brain.errors import GraphModeUnavailable
from brain.graph_rag.router import (
    AUTO_MODE,
    DEGRADATION_REASON_G2,
    DEGRADED_FROM_GLOBAL,
    FUSE_MODE,
    GLOBAL_MODE,
    LOCAL_MODE,
    THEMES_MODE,
    KnownPerson,
    RoutingDecision,
    route,
)


def _kp(canonical_key: str, display_name: str, doc_count: int = 0) -> KnownPerson:
    return KnownPerson(
        canonical_key=canonical_key, display_name=display_name, doc_count=doc_count
    )


_DANA = _kp("dana lee", "Dana Lee", doc_count=4)


# --------------------------------------------------------------------------- #
# 1. Explicit modes
# --------------------------------------------------------------------------- #
def test_explicit_local_is_honored() -> None:
    decision = route("anything at all", mode=LOCAL_MODE, person=None, known_persons=[])

    assert decision.executed_mode == LOCAL_MODE
    assert decision.requested_mode == LOCAL_MODE
    assert decision.degraded_from is None
    assert decision.degradation_reason is None
    assert decision.resolved_person is None


def test_explicit_local_ignores_thematic_grammar_but_records_it() -> None:
    """A thematic query under explicit local stays local; ``is_thematic`` is set."""
    decision = route("recurring themes", mode=LOCAL_MODE, person=None, known_persons=[])

    assert decision.executed_mode == LOCAL_MODE
    assert decision.is_thematic is True
    assert decision.degraded_from is None


def test_explicit_local_with_person_records_explicit_person() -> None:
    decision = route("x", mode=LOCAL_MODE, person=" Dana Lee ", known_persons=[])

    assert decision.executed_mode == LOCAL_MODE
    assert decision.resolved_person is not None
    assert decision.resolved_person.source == "explicit"
    assert decision.resolved_person.display_name == "Dana Lee"  # stripped
    assert decision.resolved_person.canonical_key is None


def test_explicit_blank_person_resolves_to_none() -> None:
    """A whitespace-only explicit person is treated as absent."""
    decision = route("x", mode=LOCAL_MODE, person="   ", known_persons=[])

    assert decision.executed_mode == LOCAL_MODE
    assert decision.resolved_person is None


def test_explicit_themes_is_honored_with_person() -> None:
    decision = route("x", mode=THEMES_MODE, person="Dana Lee", known_persons=[])

    assert decision.executed_mode == THEMES_MODE
    assert decision.requested_mode == THEMES_MODE
    assert decision.resolved_person is not None
    assert decision.resolved_person.display_name == "Dana Lee"
    assert decision.degraded_from is None


def test_explicit_themes_without_person_does_not_raise_in_router() -> None:
    """The router returns themes with no person; the caller validates (ValueError)."""
    decision = route("x", mode=THEMES_MODE, person=None, known_persons=[])

    assert decision.executed_mode == THEMES_MODE
    assert decision.resolved_person is None


def test_explicit_global_routes_to_global() -> None:
    """G3-e flip: explicit ``global`` now returns GLOBAL_MODE (no raise)."""
    decision = route("x", mode=GLOBAL_MODE, person=None, known_persons=[])

    assert decision.executed_mode == GLOBAL_MODE
    assert decision.requested_mode == GLOBAL_MODE
    assert decision.degraded_from is None
    assert decision.degradation_reason is None


def test_explicit_global_routes_to_global_even_with_person() -> None:
    """Explicit global routes to GLOBAL_MODE regardless of a resolvable person."""
    decision = route(
        "themes with dana", mode=GLOBAL_MODE, person="Dana Lee", known_persons=[]
    )

    assert decision.executed_mode == GLOBAL_MODE
    assert decision.requested_mode == GLOBAL_MODE
    # The explicit person is still recorded on the decision (the global path
    # ignores it, but routing stays uniform across modes).
    assert decision.resolved_person is not None
    assert decision.resolved_person.display_name == "Dana Lee"
    assert decision.degraded_from is None


def test_explicit_fuse_routes_to_fuse() -> None:
    """G4-c: explicit ``fuse`` returns FUSE_MODE (honored as an explicit mode)."""
    decision = route("x", mode=FUSE_MODE, person=None, known_persons=[])

    assert decision.executed_mode == FUSE_MODE
    assert decision.requested_mode == FUSE_MODE
    assert decision.degraded_from is None
    assert decision.degradation_reason is None


def test_auto_never_routes_to_fuse() -> None:
    """G4-c: ``fuse`` is explicit-only — the auto router never targets it."""
    # A thematic query with a resolvable person → themes (not fuse).
    themes = route(
        "themes with dana", mode=AUTO_MODE, person="Dana Lee", known_persons=[]
    )
    assert themes.executed_mode != FUSE_MODE
    # A thematic query with no person → global (not fuse).
    glob = route("recurring patterns", mode=AUTO_MODE, person=None, known_persons=[])
    assert glob.executed_mode != FUSE_MODE
    # A non-thematic query → local (not fuse).
    loc = route("acme corp", mode=AUTO_MODE, person=None, known_persons=[])
    assert loc.executed_mode != FUSE_MODE


def test_unknown_mode_raises_value_error() -> None:
    with pytest.raises(ValueError):
        route("x", mode="sideways", person=None, known_persons=[])


def test_unknown_mode_error_lists_fuse() -> None:
    """G4-c: the unknown-mode error message names ``fuse`` among valid modes."""
    with pytest.raises(ValueError, match="fuse"):
        route("x", mode="sideways", person=None, known_persons=[])


# --------------------------------------------------------------------------- #
# 2. Auto branches
# --------------------------------------------------------------------------- #
def test_auto_thematic_with_explicit_person_routes_to_themes() -> None:
    decision = route(
        "themes in my chats", mode=AUTO_MODE, person="Dana Lee", known_persons=[]
    )

    assert decision.executed_mode == THEMES_MODE
    assert decision.requested_mode == AUTO_MODE
    assert decision.is_thematic is True
    assert decision.resolved_person is not None
    assert decision.resolved_person.source == "explicit"
    assert decision.degraded_from is None
    assert decision.degradation_reason is None


def test_auto_thematic_with_scanned_person_routes_to_themes() -> None:
    decision = route(
        "what are the themes in my conversations with Dana Lee",
        mode=AUTO_MODE,
        person=None,
        known_persons=[_DANA],
    )

    assert decision.executed_mode == THEMES_MODE
    assert decision.resolved_person is not None
    assert decision.resolved_person.source == "scanned"
    assert decision.resolved_person.canonical_key == "dana lee"
    assert decision.resolved_person.doc_count == 4
    assert decision.degraded_from is None


def test_auto_thematic_no_person_routes_to_global() -> None:
    """G3-e flip: thematic + no resolvable person → GLOBAL_MODE, no degradation."""
    decision = route(
        "what are the recurring themes lately",
        mode=AUTO_MODE,
        person=None,
        known_persons=[_DANA],  # present but not named in the query
    )

    assert decision.executed_mode == GLOBAL_MODE
    assert decision.requested_mode == AUTO_MODE
    assert decision.is_thematic is True
    assert decision.resolved_person is None
    # Degradation signals are dormant after the flip (spec §17c Q6).
    assert decision.degraded_from is None
    assert decision.degradation_reason is None


def test_auto_non_thematic_routes_to_local() -> None:
    decision = route(
        "acme corporation pricing", mode=AUTO_MODE, person=None, known_persons=[_DANA]
    )

    assert decision.executed_mode == LOCAL_MODE
    assert decision.requested_mode == AUTO_MODE
    assert decision.is_thematic is False
    assert decision.degraded_from is None
    assert decision.degradation_reason is None


def test_auto_non_thematic_with_named_person_still_local_not_degraded() -> None:
    """A non-thematic query never degrades, even when it names a known person."""
    decision = route(
        "email from Dana Lee about pricing",
        mode=AUTO_MODE,
        person=None,
        known_persons=[_DANA],
    )

    assert decision.executed_mode == LOCAL_MODE
    assert decision.is_thematic is False
    assert decision.degraded_from is None


def test_auto_explicit_person_precedes_query_scan() -> None:
    """Explicit person wins over a different person named in the query (precedence)."""
    decision = route(
        "themes with Dana Lee",
        mode=AUTO_MODE,
        person="Zoe Quartz",
        known_persons=[_DANA],
    )

    assert decision.executed_mode == THEMES_MODE
    assert decision.resolved_person is not None
    assert decision.resolved_person.source == "explicit"
    assert decision.resolved_person.display_name == "Zoe Quartz"


# --------------------------------------------------------------------------- #
# 3. Thematic-intent regex grammar
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query",
    [
        "theme",
        "themes",
        "topic",
        "topics",
        "pattern",
        "patterns",
        "trend",
        "trends",
        "recurring",
        "What are the THEMES here",  # case-insensitive
        "how has my thinking evolved",
        "how have things changed",
        "how did the strategy shift",
        "how does this evolve",
        "how has the roadmap shifted over time",
        "how has our pricing approach changed since last year",
    ],
)
def test_thematic_grammar_positive(query: str) -> None:
    decision = route(query, mode=AUTO_MODE, person=None, known_persons=[])
    assert decision.is_thematic is True


@pytest.mark.parametrize(
    "query",
    [
        "",
        "pricing strategy for enterprise",
        "thematic analysis is a method",  # 'thematic' is not 'theme(s)'
        "atopic dermatitis notes",  # 'atopic' is not 'topic'
        "subtopic breakdown",  # 'subtopic' has no left boundary before 'topic'
        "trending now",  # 'trending' is not 'trend(s)'
        "patterned carpet",  # 'patterned' is not 'pattern(s)'
        "how is my thinking",  # no has/have/did/does
        "how has it been going",  # no evolve/change/shift after
        "what changed in the roadmap",  # no 'how has/have/did/does' lead-in
    ],
)
def test_thematic_grammar_negative(query: str) -> None:
    decision = route(query, mode=AUTO_MODE, person=None, known_persons=[])
    assert decision.is_thematic is False


def test_evolution_gap_within_bound_matches() -> None:
    query = "how has the broader team thinking evolved"
    assert route(query, mode=AUTO_MODE, person=None, known_persons=[]).is_thematic


def test_evolution_gap_beyond_bound_does_not_match() -> None:
    """The ``.{0,80}`` cap means a far-apart lead-in + verb is NOT thematic."""
    query = "how has " + ("word " * 20) + "evolved"  # ~100-char gap
    assert not route(query, mode=AUTO_MODE, person=None, known_persons=[]).is_thematic


# --------------------------------------------------------------------------- #
# 4. Person token-boundary scan + tie-breaks
# --------------------------------------------------------------------------- #
def test_scan_requires_token_boundary() -> None:
    """A substring inside a larger word does not match (banana !=> ana)."""
    decision = route(
        "themes about banana bread",
        mode=AUTO_MODE,
        person=None,
        known_persons=[_kp("ana", "Ana", doc_count=5)],
    )

    # No person matched at a boundary → thematic-no-person → global (G3-e flip).
    assert decision.executed_mode == GLOBAL_MODE
    assert decision.resolved_person is None
    assert decision.degraded_from is None


def test_scan_matches_display_name_not_only_canonical_key() -> None:
    """A query naming the display name resolves even if canonical_key differs."""
    person = _kp("d-lee-001", "Dana Lee", doc_count=3)
    decision = route(
        "themes with Dana Lee", mode=AUTO_MODE, person=None, known_persons=[person]
    )

    assert decision.executed_mode == THEMES_MODE
    assert decision.resolved_person is not None
    assert decision.resolved_person.canonical_key == "d-lee-001"


def test_scan_skips_empty_key_and_uses_the_other() -> None:
    """A person with a blank display_name still resolves via its canonical_key."""
    person = _kp("bob", "", doc_count=1)
    decision = route(
        "topics about bob", mode=AUTO_MODE, person=None, known_persons=[person]
    )

    assert decision.executed_mode == THEMES_MODE
    assert decision.resolved_person is not None
    assert decision.resolved_person.canonical_key == "bob"


def test_tiebreak_longest_span_wins_over_doc_count() -> None:
    """``dana lee`` (span 8) beats ``lee`` (span 3) despite a lower doc_count."""
    short = _kp("lee", "Lee", doc_count=99)
    long = _kp("dana lee", "Dana Lee", doc_count=1)
    decision = route(
        "themes with dana lee",
        mode=AUTO_MODE,
        person=None,
        known_persons=[short, long],
    )

    assert decision.resolved_person is not None
    assert decision.resolved_person.canonical_key == "dana lee"


def test_tiebreak_doc_count_breaks_equal_span() -> None:
    low = _kp("ana", "Ana", doc_count=2)
    high = _kp("bob", "Bob", doc_count=9)
    decision = route(
        "topics covering ana and bob",
        mode=AUTO_MODE,
        person=None,
        known_persons=[low, high],
    )

    assert decision.resolved_person is not None
    assert decision.resolved_person.canonical_key == "bob"
    assert decision.resolved_person.doc_count == 9


def test_tiebreak_lexicographic_canonical_key_breaks_equal_span_and_doc_count() -> None:
    aaa = _kp("aaa", "Aaa", doc_count=5)
    bbb = _kp("bbb", "Bbb", doc_count=5)
    decision = route(
        "trends in aaa and bbb",
        mode=AUTO_MODE,
        person=None,
        known_persons=[bbb, aaa],  # reversed input order to prove sort, not order
    )

    assert decision.resolved_person is not None
    assert decision.resolved_person.canonical_key == "aaa"


def test_scan_with_empty_known_persons_resolves_none() -> None:
    decision = route("themes generally", mode=AUTO_MODE, person=None, known_persons=[])

    assert decision.resolved_person is None
    # Thematic + no candidates → no resolvable person → global (G3-e flip).
    assert decision.executed_mode == GLOBAL_MODE
    assert decision.degraded_from is None


# --------------------------------------------------------------------------- #
# 5. Determinism + value-object shape
# --------------------------------------------------------------------------- #
def test_route_is_deterministic_on_ties() -> None:
    persons = [_kp("aaa", "Aaa", 5), _kp("bbb", "Bbb", 5)]
    first = route("trends in aaa bbb", mode=AUTO_MODE, person=None, known_persons=persons)
    second = route(
        "trends in aaa bbb", mode=AUTO_MODE, person=None, known_persons=persons
    )

    assert first == second  # frozen dataclass value equality


def test_routing_decision_is_frozen() -> None:
    decision = route("x", mode=LOCAL_MODE, person=None, known_persons=[])
    assert isinstance(decision, RoutingDecision)
    with pytest.raises(AttributeError):
        decision.executed_mode = THEMES_MODE  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 6. KEEP-DORMANT degradation machinery (spec §17c Q6)
# --------------------------------------------------------------------------- #
def test_degradation_machinery_kept_dormant() -> None:
    """The G2 degradation constants + exception stay DEFINED after the G3-e flip.

    Spec §17c Q6 keeps them for wire stability: ``route`` never populates the
    constants onto a decision and never raises ``GraphModeUnavailable`` for
    global anymore, but the symbols remain importable.
    """
    # Constants retained at their original values (dormant, never stamped).
    assert DEGRADED_FROM_GLOBAL == "global"
    assert DEGRADATION_REASON_G2 == "global_unavailable_g2"
    # The exception type is still defined (just no longer raised for global).
    assert issubclass(GraphModeUnavailable, Exception)


def test_no_route_path_populates_degradation_fields() -> None:
    """Across every mode/branch, ``route`` leaves the degradation fields None."""
    cases = [
        route("x", mode=LOCAL_MODE, person=None, known_persons=[]),
        route("x", mode=THEMES_MODE, person="Dana Lee", known_persons=[]),
        route("x", mode=GLOBAL_MODE, person=None, known_persons=[]),
        route("acme pricing", mode=AUTO_MODE, person=None, known_persons=[_DANA]),
        route("themes with Dana Lee", mode=AUTO_MODE, person=None, known_persons=[_DANA]),
        route("recurring themes lately", mode=AUTO_MODE, person=None, known_persons=[]),
    ]
    for decision in cases:
        assert decision.degraded_from is None
        assert decision.degradation_reason is None
