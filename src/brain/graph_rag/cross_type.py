"""Automatic cross-document concept type-collapse (Bug A — no schema migration).

The catalog uniqueness key is type-scoped — ``UNIQUE (tenant_id, entity_type,
canonical_key)`` (migration 012) — so one ``canonical_key`` may exist once *per
type*. The LLM concept extractor assigns the same real concept a different
``entity_type`` across documents (``acmeplatform`` as ``org`` in one doc,
``project`` in another), and each satisfies the type-scoped key, landing as a
SEPARATE row. :func:`brain.graph_rag.extract._dedupe_cross_type` already collapses
this **within a single document** using :data:`~brain.graph_rag.extract._TYPE_PRECEDENCE`;
this module is its **cross-document** counterpart.

**Path 1 (no migration).** Rather than widening the schema, this reuses the
shipped, tested repoint + AGE-upsert + ``refresh_aggregates`` + orphan-GC wrapper
:func:`brain.graph_rag.aliases.merge_aliases`. The collapse:

1. scans the tenant's concept catalog for every ``canonical_key`` carried by more
   than one CONCEPT ``entity_type`` (:func:`generate_cross_type_collapse_rules`),
2. generates :class:`~brain.graph_rag.aliases.AliasRule` rows merging each
   lower-precedence type into the highest-precedence type present (same precedence
   as the per-document dedupe), then
3. validates them (chain/cycle/dup-source — :func:`brain.graph_rag.aliases
   ._validate_alias_graph`) and applies them via ``merge_aliases``.

**Scope — concept types ONLY.** The scan is restricted to the four concept types
(``org``/``project``/``tool``/``topic``); ``person`` rows are never selected, so a
person can never be a merge source, and the precedence winner is always a concept
type, so a person can never be a merge target. A person and a concept that happen
to share a ``canonical_key`` are left untouched (D1b — persons are a separate
aspect, derived from the participants pipeline, not from this catalog).

**Hook placement (correctness).** This MUST run as a corpus-level pass AFTER a
document's mentions/contributions are written + committed — never mid-reconcile
(a mid-flow GC of a zero-mention source would orphan an id the pending
mention-insert then references). It is invoked from
:meth:`brain.graph_rag.sync.GraphSyncer.reconcile` (the incremental ingest path,
after ``reconcile_document`` commits) and from the ``brain graphrag build`` /
``refresh`` CLI (after the per-document loop / the curated alias merge).

**Idempotent.** A second run finds no ``canonical_key`` under more than one type
(the first run collapsed them) → empty rules → ``merge_aliases`` short-circuits to
a zero-valued no-op. Communities go stale after any merge (entity GC cascades to
``graph_community_members``); the rollout runs ``brain graphrag communities
refresh`` after the collapse.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg

from .aliases import AliasResult, AliasRule, _validate_alias_graph, merge_aliases
from .schema import GraphEntity

if TYPE_CHECKING:
    from .backends.base import GraphBackend
    from .reconcile import ReconcileConfig

__all__ = [
    "collapse_cross_type_concepts",
    "generate_cross_type_collapse_rules",
]


def _concept_precedence() -> tuple[tuple[str, ...], list[str]]:
    """Return ``(ordered_concept_types, concept_types)`` for the collapse.

    ``ordered_concept_types`` is :data:`brain.graph_rag.extract._TYPE_PRECEDENCE`
    filtered to the concept types (``person`` removed), highest-precedence first
    — ``("org", "project", "tool", "topic")``. ``concept_types`` is the same set
    as a list for parameterized ``= ANY(%s)`` scoping. Late import keeps this
    module import-cheap (``extract`` pulls in the enrichment transport) and
    mirrors the late-import discipline used across :mod:`brain.graph_rag`.
    """
    from .extract import _TYPE_PRECEDENCE, CONCEPT_ENTITY_TYPES

    ordered = tuple(t for t in _TYPE_PRECEDENCE if t in CONCEPT_ENTITY_TYPES)
    return ordered, list(CONCEPT_ENTITY_TYPES)


def generate_cross_type_collapse_rules(
    conn: psycopg.Connection[Any], tenant_id: str
) -> list[AliasRule]:
    """Build merge rules for every cross-type concept fragment in ``tenant_id``.

    Scans ``graph_entities`` for each ``canonical_key`` carried by more than one
    CONCEPT ``entity_type`` (``person`` rows are excluded by the scan), and emits
    one :class:`~brain.graph_rag.aliases.AliasRule` per lower-precedence type
    collapsing it into the highest-precedence type present for that key (per
    :data:`brain.graph_rag.extract._TYPE_PRECEDENCE`). Both endpoints carry the
    SAME ``canonical_key`` — only the type changes — so the rule re-points the
    fragment onto the winning-type row.

    Returns a deterministically ordered list (sorted by ``(from_type, from_key)``)
    so repeated runs and tests see a stable rule order. A key under a single type
    yields no rule; distinct keys are independent (no rule ever spans two keys, so
    the generated graph is always chain/cycle/dup-source free by construction —
    :func:`collapse_cross_type_concepts` still validates it defensively). SQL is
    parameterized throughout.
    """
    ordered, concept_types = _concept_precedence()
    rank = {entity_type: i for i, entity_type in enumerate(ordered)}
    rows = conn.execute(
        """
        SELECT canonical_key, array_agg(DISTINCT entity_type) AS types
        FROM graph_entities
        WHERE tenant_id = %s AND entity_type = ANY(%s)
        GROUP BY canonical_key
        HAVING COUNT(DISTINCT entity_type) > 1
        """,
        (tenant_id, concept_types),
    ).fetchall()

    rules: list[AliasRule] = []
    for row in rows:
        canonical_key = str(row[0])
        present = [t for t in (str(x) for x in row[1]) if t in rank]
        if len(present) < 2:
            # Defensive: every selected type should be a known concept type, but
            # if an unranked type slipped in there is nothing to collapse.
            continue
        winner = min(present, key=lambda entity_type: rank[entity_type])
        for entity_type in present:
            if entity_type == winner:
                continue
            rules.append(
                AliasRule(
                    from_type=entity_type,
                    from_key=canonical_key,
                    to_type=winner,
                    to_key=canonical_key,
                )
            )
    rules.sort(key=lambda rule: (rule.from_type, rule.from_key))
    return rules


def collapse_cross_type_concepts(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    backend: GraphBackend,
    *,
    config: ReconcileConfig | None = None,
    dry_run: bool = False,
) -> AliasResult:
    """Collapse cross-document concept type-fragmentation for one tenant (Bug A).

    Generates the auto-merge rules (:func:`generate_cross_type_collapse_rules`),
    validates them (:func:`brain.graph_rag.aliases._validate_alias_graph` —
    ``merge_aliases`` does not re-validate a directly-passed rules list), and
    applies them atomically via :func:`brain.graph_rag.aliases.merge_aliases`
    (re-point mentions + contributions, provision any new target AGE vertices,
    ``refresh_aggregates`` → GC the now-zero-mention source rows + DETACH DELETE
    their AGE vertices + rebuild ``CO_OCCURS``). Concept types only — ``person``
    is never a source or target.

    Returns the :class:`~brain.graph_rag.aliases.AliasResult`; an empty rule set
    (nothing to collapse) returns a zero-valued result without opening a
    transaction, so callers can invoke this unconditionally. ``dry_run=True``
    reports the would-be merges without persisting. ``config`` is forwarded to
    ``merge_aliases`` so the post-merge edge recompute uses the caller's
    ``generic_df_ratio`` (its ``tenant_id`` MUST match ``tenant_id``).

    **Display-name preservation.** ``merge_aliases``'s find-or-create
    (:func:`brain.graph_rag.aliases._upsert_entity`) rewrites the target row's
    ``name`` to ``humanize_person_name(canonical_key)`` — fine for a curated alias
    target the operator named by key, but for an auto-collapse the target is an
    EXISTING concept row carrying the extractor's proper surface form (e.g.
    ``"AcmePlatform"``), which the humanized strip-all key would clobber to
    ``"Acmeplatform"`` (camelCase lost). To avoid changing shared
    ``merge_aliases`` behavior (which curated aliases + their tests rely on), this
    captures the BEST surface form across ALL merged variants BEFORE the merge
    (:func:`_best_display_names` / :func:`_best_surface_form` — F1: a branded
    mixed-case form like ``"AcmePlatform"`` beats an all-lowercase ``"acmeplatform"``
    even when the lowercase variant wins TYPE precedence) and restores it —
    relationally AND on the AGE vertex — AFTER, so the merged concept keeps the
    best real display name rather than the winning type's possibly-lowercase one.
    """
    rules = generate_cross_type_collapse_rules(conn, tenant_id)
    if not rules:
        return AliasResult(tenant_id=tenant_id, rules_total=0, dry_run=dry_run)
    _validate_alias_graph(rules)  # chain/cycle/dup-source (defensive; F7)
    # Snapshot the BEST surface form across all merged variants before
    # merge_aliases overwrites the winner's name with the humanized key (F1).
    best_names = _best_display_names(conn, tenant_id, rules)
    result = merge_aliases(
        conn, tenant_id, rules, backend, dry_run=dry_run, config=config
    )
    if not dry_run and result.rules_applied > 0:
        _restore_winner_display_names(conn, tenant_id, backend, best_names)
    return result


def _uppercase_count(name: str) -> int:
    """Count uppercase letters in ``name`` — a proxy for proper branding/casing."""
    return sum(1 for char in name if char.isupper())


def _is_all_caps(name: str) -> bool:
    """True when ``name`` has letters but NO lowercase — an all-caps "shout".

    Distinguishes ``"NEON"`` / ``"DACS"`` (shouting) from properly-cased
    ``"Neon"`` / ``"DACs"``. A genuine all-caps acronym (``"NFPA"``) is still
    kept when it is the only / best variant — the guard only DEMOTES an all-caps
    form when a mixed-case sibling exists.
    """
    return any(char.isalpha() for char in name) and not any(
        char.islower() for char in name
    )


def _best_surface_form(variants: list[tuple[str, int]]) -> str:
    """Pick the best display name among merged variants (F1, deterministic).

    ``variants`` is ``[(name, doc_count), …]`` — the surface forms of every
    concept row sharing one ``canonical_key`` that the collapse merges. The
    heuristic is a total order (never a tie), in priority:

    1. prefer a mixed/cased form over an ALL-CAPS "shout" (``"Neon"`` over
       ``"NEON"``, ``"DACs"`` over ``"DACS"``) — a lone all-caps acronym is still
       kept when no mixed-case sibling exists;
    2. more uppercase letters (proper branding) — a mixed/branded form beats an
       all-lowercase one (``AcmePlatform`` over ``acmeplatform``) and a
       better-cased branded form beats a worse one (``AI::Client`` over
       ``Ai::Client``);
    3. higher ``doc_count`` (the most-attested form);
    4. longer name;
    5. lexicographically smallest (final deterministic tiebreak).

    Known trade-off: a one-off variant with spurious extra capitals (a typo like
    ``"ALBa"``) can outrank a more-attested correctly-cased form, because casing
    signal is ranked above ``doc_count``. Acceptable for a personal corpus, and
    curated aliases can override any specific case. Returns ``""`` only for empty
    input (caller guards).
    """
    if not variants:
        return ""
    return min(
        variants,
        key=lambda v: (_is_all_caps(v[0]), -_uppercase_count(v[0]), -v[1], -len(v[0]), v[0]),
    )[0]


def _best_display_names(
    conn: psycopg.Connection[Any], tenant_id: str, rules: list[AliasRule]
) -> dict[tuple[str, str], str]:
    """Capture the BEST surface form to assign each merge winner (F1).

    For every distinct rule TARGET ``(to_type, to_key)`` — the concept row the
    lower-precedence fragments collapse into — collect the ``(name, doc_count)``
    of EVERY concept row sharing that ``canonical_key`` (the winner plus all the
    soon-to-be-GC'd source variants) and pick the best via
    :func:`_best_surface_form`. Returns ``{(to_type, to_key): best_name}``. Read
    BEFORE ``merge_aliases`` because the source variants are deleted by the
    merge's orphan GC, so their surface forms must be captured first. Restricted
    to concept types (``person`` is never part of a cross-type collapse).
    Parameterized SQL.
    """
    names: dict[tuple[str, str], str] = {}
    _, concept_types = _concept_precedence()
    for to_type, to_key in sorted({(r.to_type, r.to_key) for r in rules}):
        rows = conn.execute(
            "SELECT name, doc_count FROM graph_entities "
            "WHERE tenant_id = %s AND canonical_key = %s AND entity_type = ANY(%s)",
            (tenant_id, to_key, concept_types),
        ).fetchall()
        variants = [(str(r[0]), int(r[1])) for r in rows]
        best = _best_surface_form(variants)
        if best:
            names[(to_type, to_key)] = best
    return names


def _restore_winner_display_names(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    backend: GraphBackend,
    winner_names: dict[tuple[str, str], str],
) -> None:
    """Restore the captured target ``name``s relationally + on the AGE vertex.

    Undoes ``merge_aliases``'s humanized-key overwrite of each winning concept
    row's ``name`` (see :func:`collapse_cross_type_concepts`). Runs in one
    transaction: UPDATE the relational ``name`` back to its captured surface form,
    then re-MERGE the corrected vertices via
    :meth:`~brain.graph_rag.backends.base.GraphBackend.upsert_entities` so the AGE
    label matches too. A winner that no longer exists is skipped (it cannot be
    GC'd — it carries the merged mentions). Parameterized SQL.
    """
    if not winner_names:
        return
    with conn.transaction():
        corrected: list[GraphEntity] = []
        for (entity_type, canonical_key), name in winner_names.items():
            row = conn.execute(
                "UPDATE graph_entities SET name = %s "
                "WHERE tenant_id = %s AND entity_type = %s AND canonical_key = %s "
                "RETURNING id::text",
                (name, tenant_id, entity_type, canonical_key),
            ).fetchone()
            if row is None:
                continue
            corrected.append(
                GraphEntity(
                    id=str(row[0]),
                    entity_type=entity_type,
                    name=name,
                    canonical_key=canonical_key,
                    tenant_id=tenant_id,
                )
            )
        if corrected:
            backend.upsert_entities(conn, tenant_id, corrected)
