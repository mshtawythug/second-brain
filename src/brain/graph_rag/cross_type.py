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
    captures each winning target's current ``name`` BEFORE the merge and restores
    it — relationally AND on the AGE vertex — AFTER, so the merged concept keeps
    its real display name.
    """
    rules = generate_cross_type_collapse_rules(conn, tenant_id)
    if not rules:
        return AliasResult(tenant_id=tenant_id, rules_total=0, dry_run=dry_run)
    _validate_alias_graph(rules)  # chain/cycle/dup-source (defensive; F7)
    # Snapshot the winning targets' real surface names before merge_aliases
    # overwrites them with the humanized canonical key (see docstring).
    winner_names = _winner_display_names(conn, tenant_id, rules)
    result = merge_aliases(
        conn, tenant_id, rules, backend, dry_run=dry_run, config=config
    )
    if not dry_run and result.rules_applied > 0:
        _restore_winner_display_names(conn, tenant_id, backend, winner_names)
    return result


def _winner_display_names(
    conn: psycopg.Connection[Any], tenant_id: str, rules: list[AliasRule]
) -> dict[tuple[str, str], str]:
    """Capture each rule TARGET's current ``name`` (the merge winner's surface form).

    Returns ``{(to_type, to_key): name}`` for every distinct target — the concept
    row the lower-precedence fragments collapse into. Read BEFORE ``merge_aliases``
    so the extractor's real display name (``"AcmePlatform"``) is preserved across
    the merge's humanized-key overwrite. Parameterized SQL.
    """
    names: dict[tuple[str, str], str] = {}
    for to_type, to_key in sorted({(r.to_type, r.to_key) for r in rules}):
        row = conn.execute(
            "SELECT name FROM graph_entities "
            "WHERE tenant_id = %s AND entity_type = %s AND canonical_key = %s",
            (tenant_id, to_type, to_key),
        ).fetchone()
        if row is not None:
            names[(to_type, to_key)] = str(row[0])
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
