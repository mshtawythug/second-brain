"""The tsquery re-parse bug: lexemes must never be stemmed twice (task #14).

``build_tsquery`` returns LEXEMES — text that has already been through
``plainto_tsquery``. Binding those as ``to_tsquery('english', %s)`` stems them a
SECOND time, and the result no longer matches the stored ``tsv``.

**Why this survived so long, and why this module is written the way it is.**
The bug is invisible to any test whose vocabulary happens to be stem-stable —
``workflow``, ``roadmap``, ``payroll`` all survive a second pass unchanged, so a
suite built from them is green while the feature is broken. My own first
workaround while building F6 was to switch a test's vocabulary to stable words,
which is the same blind spot that let this persist. So every term below is
deliberately drawn from the UNSTABLE set, and
:data:`_UNSTABLE_TERMS` is asserted to actually BE unstable before it is used to
test anything — otherwise a future Postgres stemmer change could quietly turn
this whole module into a tautology.

Three distinct failure modes were measured on the live 1,376-doc corpus:

* **re-stemming** — ``provisioning`` -> ``provis`` -> ``provi``. 2.5% of the
  top-2000 single-token corpus lexemes are affected. The query ``provisioning``
  matched 94 documents through the ranked leg and 1 through the facet leg.
* **re-parsing of hyphenated compounds** — a stored single lexeme comes back as
  a phrase (``'a-b' <-> 'a' <-> 'b'``) that cannot match it.
* **stop-word annihilation** — a term re-parses to the EMPTY tsquery, silently
  deleting it from the query.

Because ``plainto_tsquery`` AND-s terms, ONE affected word zeroes the whole FTS
leg.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.facets import compute_facets, count_matching_documents
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import build_tsquery, hybrid_search
from brain.search_predicate import build_predicate
from tests.conftest import FakeEmbedder

#: Terms whose stem is NOT stem-stable. Drawn from the live-corpus measurement.
#: ``provisioning`` is the canonical case: ``provis`` -> ``provi``.
_UNSTABLE_TERMS = (
    "provisioning",
    "responsive",
    "decision",
    "conversation",
    # Merged from test_search_double_stemming.py so no vocabulary coverage was
    # lost in the consolidation: databas -> databa, enterpris -> enterpri.
    "database",
    "enterprise",
)

_BODY = (
    "Platform notes. The provisioning process was reviewed alongside the "
    "responsive rollout plan. A decision was recorded after the conversation "
    "with the operations group. The database and enterprise rollout notes "
    "were attached.\n"
)


def _seed(conn: psycopg.Connection[Any], *, title: str) -> str:
    result = ingest_document(
        conn,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title=title,
            content=f"{title}\n\n{_BODY}",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=title,
    )
    assert result.document_id is not None
    return result.document_id


def test_the_chosen_terms_really_are_unstable(
    test_db: psycopg.Connection[Any],
) -> None:
    """GUARD ON THE GUARD: prove the fixtures exercise the bug at all.

    Every other test here is meaningful only if these terms genuinely lose
    information under a second parse. If a future Postgres/Snowball change made
    them stable, the rest of this module would still pass while testing
    nothing — the precise failure mode that let the original bug survive.
    """
    unstable = []
    for term in _UNSTABLE_TERMS:
        row = test_db.execute(
            "SELECT plainto_tsquery('english', %s)::text", (term,)
        ).fetchone()
        assert row is not None
        once = str(row[0])
        row2 = test_db.execute(
            "SELECT to_tsquery('english', %s)::text", (once,)
        ).fetchone()
        assert row2 is not None
        if str(row2[0]) != once:
            unstable.append(term)

    assert unstable, (
        f"none of {_UNSTABLE_TERMS} is stem-unstable on this Postgres, so this "
        f"module no longer tests the double-stemming bug. Replace them with "
        f"terms that are (check ts_stat on a real corpus)."
    )


@pytest.mark.parametrize("term", _UNSTABLE_TERMS)
def test_reparsing_a_lexeme_loses_the_match(
    test_db: psycopg.Connection[Any], term: str
) -> None:
    """Demonstrates the defect directly, at the SQL layer.

    Kept as an explicit demonstration rather than folded into the behavioural
    tests below, so the *mechanism* is documented in an executable form: a
    reader who wonders "why can't we just call to_tsquery on it?" gets a
    runnable answer.
    """
    _seed(test_db, title="Synthetic stem probe")
    lexemes = build_tsquery(test_db, term)

    correct = test_db.execute(
        "SELECT count(*) FROM chunks WHERE tsv @@ %s::tsquery", (lexemes,)
    ).fetchone()
    reparsed = test_db.execute(
        "SELECT count(*) FROM chunks WHERE tsv @@ to_tsquery('english', %s)",
        (lexemes,),
    ).fetchone()

    assert correct is not None and reparsed is not None
    assert correct[0] > 0, f"{term!r} must match the seeded body when bound correctly"
    assert reparsed[0] < correct[0], (
        f"{term!r} was expected to lose matches under a second parse; if this "
        f"fails the term is stem-stable and is the wrong fixture"
    )


@pytest.mark.parametrize("term", _UNSTABLE_TERMS)
def test_ranked_leg_finds_unstable_terms(
    test_db: psycopg.Connection[Any], term: str
) -> None:
    """``hybrid_search`` matches an unstable term (the ranked leg binds correctly)."""
    _seed(test_db, title="Synthetic ranked probe")

    results = hybrid_search(
        test_db, embedder=FakeEmbedder(), query=term, limit=5, fts_only=True
    )

    assert results, (
        f"the FTS leg returned nothing for {term!r} — the lexeme is being "
        f"re-parsed somewhere in search.py"
    )


@pytest.mark.parametrize("term", _UNSTABLE_TERMS)
def test_total_count_agrees_with_the_ranked_leg(
    test_db: psycopg.Connection[Any], term: str
) -> None:
    """REGRESSION: the ``N matched`` total must not undercount an unstable term.

    This is the half of the bug that survived the first fix. ``search.py``'s
    ranked legs were switched to ``%s::tsquery`` while ``facets.py`` kept
    ``to_tsquery('english', %s)``, so the two disagreed: on the live corpus
    ``provisioning`` ranked 94 documents and this counted 1.

    A footer that says ``1 matched`` above a screen of results is worse than no
    footer — it reads as authoritative and is wrong by two orders of magnitude.
    """
    _seed(test_db, title="Synthetic total probe one")
    _seed(test_db, title="Synthetic total probe two")

    ranked = hybrid_search(
        test_db, embedder=FakeEmbedder(), query=term, limit=50, fts_only=True
    )
    total = count_matching_documents(
        test_db, predicate=build_predicate(), tsquery=build_tsquery(test_db, term)
    )

    assert ranked, f"precondition: the ranked leg must match {term!r}"
    assert total >= len(ranked), (
        f"the total ({total}) undercounts the ranked results ({len(ranked)}) "
        f"for {term!r} — facets.py is re-parsing the lexeme"
    )
    assert total == 2, f"both seeded documents contain {term!r}"


@pytest.mark.parametrize("term", _UNSTABLE_TERMS)
def test_facets_agree_with_the_ranked_leg(
    test_db: psycopg.Connection[Any], term: str
) -> None:
    """The facet panel describes the same match set the results came from.

    ``facets.py``'s whole reason for co-locating the count and the rollup is
    that they cannot be allowed to drift from each other or from the ranked
    legs. A silently-empty facet panel would read as "this corpus has no
    structure" rather than "the query was mis-bound".
    """
    _seed(test_db, title="Synthetic facet probe one")
    _seed(test_db, title="Synthetic facet probe two")

    facets = compute_facets(
        test_db, predicate=build_predicate(), tsquery=build_tsquery(test_db, term)
    )

    assert facets.total_documents == 2, (
        f"facet rollup lost documents for {term!r}: {facets.total_documents}"
    )
    assert facets.source, "the source facet must be populated for a real match set"


# --- task #20: the wiki Related-Docs leg --------------------------------------
#
# `related.py` (extracted from `wiki/build_related.py`) was the last site. It
# differs from the search legs in
# two ways that matter:
#
# 1. Its lexemes come from `ts_stat` over `chunks.tsv` — they are already
#    stemmed by construction, not merely "usually stemmed".
# 2. They are `" | ".join`-ed (OR, not AND), so an affected term contributes
#    NOTHING rather than zeroing the whole query. Severity is therefore
#    *degraded relevance*, not empty results — which is why this was correctly
#    triaged below the search legs.
#
# The trap: `_to_tsquery_text` has TWO callers with opposite needs. The title
# path feeds it RAW tokens, where stemming is required and correct; the body
# path feeds it `ts_stat` lexemes, where stemming is the bug. A blind cast swap
# would have silently broken the title path, so the fix SPLIT the helper rather
# than changing it.


def test_lexeme_helper_preserves_an_already_stemmed_lexeme(
    test_db: psycopg.Connection[Any],
) -> None:
    """``_lexeme_to_tsquery_text`` must not stem its input a second time."""
    from brain.related import _lexeme_to_tsquery_text

    for term in _UNSTABLE_TERMS:
        row = test_db.execute(
            "SELECT plainto_tsquery('english', %s)::text", (term,)
        ).fetchone()
        assert row is not None
        lexeme = str(row[0]).strip("'")

        out = _lexeme_to_tsquery_text(test_db, lexeme)
        assert out.strip("'") == lexeme, (
            f"{lexeme!r} (already a lexeme, from ts_stat) came back as {out!r} — "
            "it was stemmed a second time and will no longer match the tsv "
            "column it was derived from."
        )


def test_raw_token_helper_still_stems(test_db: psycopg.Connection[Any]) -> None:
    """``_to_tsquery_text`` must KEEP stemming — the title path depends on it.

    Guards the other half of the split. If someone "fixes" this one the same way
    as the lexeme helper, raw title tokens stop being normalised and silently
    stop matching the stored ``tsv`` — the mirror image of the original bug, and
    invisible without this assertion.
    """
    from brain.related import _to_tsquery_text

    stemmed = _to_tsquery_text(test_db, "provisioning")
    assert stemmed.strip("'") == "provis", (
        f"raw-token stemming regressed: 'provisioning' -> {stemmed!r}. The title "
        "path needs raw text stemmed into lexemes."
    )


def test_lexeme_helper_still_rejects_unsafe_input(
    test_db: psycopg.Connection[Any],
) -> None:
    """Swapping re-parse for a cast must not lose the validation it provided.

    ``_to_tsquery_text`` rejected anything that was not a safe
    ``[a-z][a-z0-9]*`` lexeme so callers could ``" | ".join`` the parts without
    building a malformed tsquery. The cast-based replacement must reject the
    same inputs, or a stray token produces a query that fails at execution time.
    """
    from brain.related import _lexeme_to_tsquery_text

    for bad in ("9leading", "has-hyphen", "two words", "", "UPPER"):
        assert _lexeme_to_tsquery_text(test_db, bad) == "", (
            f"{bad!r} must be rejected — the join would otherwise emit a "
            "malformed tsquery"
        )


def test_stop_word_lexeme_is_not_annihilated(
    test_db: psycopg.Connection[Any],
) -> None:
    """A lexeme that re-parses to the EMPTY tsquery must survive the cast.

    Third failure class, and the one that is invisible in isolation: ``own``
    appears in 400 live documents, and ``to_tsquery('english', 'own')`` returns
    an EMPTY tsquery, so the term silently vanished from the OR chain rather
    than failing loudly.
    """
    from brain.related import _lexeme_to_tsquery_text, _to_tsquery_text

    assert _to_tsquery_text(test_db, "own") == "", (
        "premise check: the re-parsing helper is expected to annihilate 'own'"
    )
    assert _lexeme_to_tsquery_text(test_db, "own").strip("'") == "own", (
        "the cast-based helper must preserve a stop-word lexeme that ts_stat "
        "legitimately produced"
    )


# --- merged from test_search_double_stemming.py (task #14, wgraph) ------------
#
# Two modules covered this one defect and would have drifted apart silently.
# Consolidated here; nothing was dropped. What follows is the coverage that did
# not already exist above: the AND-zeroing multiplier, the hyphenated-compound
# class end-to-end, tsquery-literal validity, and a source-level structural
# guard.


def test_one_unstable_term_does_not_zero_a_multi_word_query(
    test_db: psycopg.Connection[Any],
) -> None:
    """A stable + unstable word pair still matches.

    This is the blast-radius multiplier: ``plainto_tsquery`` AND-s its terms, so
    before the fix ONE affected word was enough to zero an otherwise-fine query.
    It is why a defect touching a few percent of vocabulary was user-visible.
    """
    _seed(test_db, title="Platform provisioning notes")

    results = hybrid_search(
        test_db,
        query="platform provisioning",
        embedder=FakeEmbedder(),
        limit=5,
        fts_only=True,
    )

    assert results, (
        "a two-word query with one stem-unstable term returned nothing — one "
        "double-stemmed word zeroes the whole FTS leg"
    )


def test_hyphenated_compound_is_not_reparsed_into_a_phrase(
    test_db: psycopg.Connection[Any],
) -> None:
    """A hyphenated token matches itself rather than becoming a phrase query.

    Second failure class, end-to-end. The stored single lexeme
    ``interview-prep`` was re-parsed into
    ``'interview-prep' <-> 'interview' <-> 'prep'``, a phrase that cannot match
    it. Measured on the live corpus this class reached ``competitive-strate``
    at 3414 documents, so it was not a corner case.
    """
    _seed(test_db, title="Interview-prep index")

    results = hybrid_search(
        test_db,
        query="interview-prep",
        embedder=FakeEmbedder(),
        limit=5,
        fts_only=True,
    )

    assert results, (
        "a hyphenated compound returned nothing — it was re-parsed into a "
        "phrase query that cannot match the stored single lexeme"
    )


def test_build_tsquery_output_is_always_a_valid_tsquery_literal(
    test_db: psycopg.Connection[Any],
) -> None:
    """Whatever ``build_tsquery`` emits must cast cleanly to ``tsquery``.

    The fix moved every consumer to ``%s::tsquery``; that cast must never raise
    on real output, including the compact-form OR branch and the empty case. An
    uncastable literal would be a hard search failure rather than a silent one.
    """
    for raw in ["provisioning", "platform provisioning", "Example Group", "", "!!!"]:
        tsq = build_tsquery(test_db, raw)
        row = test_db.execute("SELECT (%s::tsquery)::text", (tsq,)).fetchone()
        assert row is not None, f"{raw!r} produced an uncastable tsquery {tsq!r}"


# --- structural: no module may re-parse an already-built tsquery -------------


#: Modules that legitimately call ``to_tsquery`` on RAW TEXT.
#:
#: ``related.py`` is here permanently, not pending a fix.
#: ``_to_tsquery_text`` stems raw *title tokens*, which is required — a raw word
#: must become a lexeme to match the stored ``tsv``. Its sibling
#: ``_lexeme_to_tsquery_text`` handles the already-stemmed ``ts_stat`` lexemes
#: by casting instead (task #20). The rule this guard enforces is "never
#: re-parse an already-stemmed LEXEME", which is narrower than "never call
#: ``to_tsquery``", and this exemption is where the two differ.
INTENTIONAL_RAW_TEXT_STEMMERS = {"related.py"}


def _modules_calling_to_tsquery() -> set[str]:
    """Modules whose SQL applies ``to_tsquery`` to something.

    Scans real string literals via AST, so prose in docstrings and comments
    describing this rule cannot trip it. ``plainto_tsquery`` — the correct entry
    point for raw user text — is never matched.
    """
    import ast
    import re
    from pathlib import Path

    # NOT `(?<!plainto_)`: "plainto_tsquery" literally CONTAINS "to_tsquery" at
    # offset 5, preceded by "n", so that lookbehind never fires. Require instead
    # that the call is not preceded by an identifier character.
    pattern = re.compile(r"(?<![A-Za-z0-9_])to_tsquery\s*\(")
    offenders: set[str] = set()
    for path in sorted(Path("src/brain").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        prose = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in prose:
                continue
            if pattern.search(node.value):
                offenders.add(path.name)
    return offenders


def test_no_new_module_reparses_an_already_built_tsquery() -> None:
    """Only the documented raw-text stemmer may call ``to_tsquery``.

    A behavioural test only covers the paths it exercises, and this defect was
    ONE wrong binding repeated across four call sites in three modules. So the
    contract is pinned at the source level too. A new module calling
    ``to_tsquery`` turns this red and has to justify itself as raw-text
    stemming or switch to ``%s::tsquery``.
    """
    unexpected = _modules_calling_to_tsquery() - INTENTIONAL_RAW_TEXT_STEMMERS

    assert not unexpected, (
        f"{sorted(unexpected)} apply to_tsquery() to a value. If the input is "
        "an already-built tsquery (build_tsquery / ts_stat output), it is "
        "LEXEMES and re-parsing stems it twice — bind as %s::tsquery instead. "
        "If it really is raw text, add the module to "
        "INTENTIONAL_RAW_TEXT_STEMMERS with the reason."
    )


def test_the_fixed_modules_stay_fixed() -> None:
    """The four modules repaired in #14/#20 must not regress.

    Without this, the guard above would still pass if someone reverted one of
    them — the exemption set would simply grow back, which is how carve-outs
    quietly become permanent.
    """
    regressed = _modules_calling_to_tsquery() & {
        "search.py",
        "facets.py",
        "global_.py",
        "_retrieval_common.py",
    }
    assert not regressed, (
        f"{sorted(regressed)} re-introduced to_tsquery() on built lexemes — "
        "the exact defect this module regression-tests."
    )
