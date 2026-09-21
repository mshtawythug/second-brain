"""Curated entity alias/merge rules for the GraphRAG entity catalog."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import yaml

from brain.errors import GraphReconcileError
from brain.person_name import humanize_person_name

if TYPE_CHECKING:
    from ..backends.base import GraphBackend
    from ..reconcile import ReconcileConfig
    from ..schema import GraphEntity

_VALID_TYPES = frozenset({"person", "org", "project", "topic", "tool"})

# ``graph_entity_mentions.source`` value for person mentions — a mirror of
# :data:`brain.graph_rag.reconcile.PEOPLE_MENTION_SOURCE`. Duplicated as a plain
# literal (not imported) so this module stays import-cheap and free of the
# ``reconcile`` → ``aggregates`` → ``concepts`` chain (the same late-import
# discipline the rest of the module follows). Person mentions are *presence
# flags* (always ``mention_count = 1``); concept mentions carry real counts under
# an ``"extractor:<model>@<ver>"`` source — the two aspects merge differently
# (see :func:`_repoint_mentions`).
_PEOPLE_MENTION_SOURCE = "people"


@dataclass(frozen=True)
class AliasRule:
    """One directed merge rule: source entity (from_type, from_key) → target (to_type, to_key)."""

    from_type: str
    from_key: str
    to_type: str
    to_key: str


@dataclass(frozen=True)
class AliasResult:
    """Summary of a single ``apply_aliases`` (or ``merge_aliases``) execution."""

    tenant_id: str
    rules_total: int = 0
    rules_applied: int = 0  # rules whose source entity existed
    mentions_repointed: int = 0
    contributions_repointed: int = 0
    sources_orphaned: int = 0  # sources left zero-mention (deleted by refresh GC, F2)
    dry_run: bool = False


def _norm(value: str) -> str:
    """Normalise a key to lowercase, collapsed whitespace."""
    return " ".join(str(value).lower().split())


def load_alias_rules(path: Path | None = None) -> list[AliasRule]:
    """Load curated rules from *path*.

    A missing file returns ``[]`` — the feature is opt-in; real rules live in a
    gitignored local file (``BRAIN_GRAPH_ALIASES_PATH``).

    Raises :class:`brain.errors.GraphReconcileError` on semantic violations:
    invalid entity type, self-merge, duplicate source, or alias chain/cycle.
    Error messages include the rule *type* and a redacted key only (F5 — never
    the full real value).
    """
    if path is None or not path.exists():
        return []
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules: list[AliasRule] = []
    for entry in raw.get("rules", []):
        frm, to = entry["from"], entry["to"]
        rule = AliasRule(
            from_type=str(frm["type"]).lower(),
            from_key=_norm(frm["key"]),
            to_type=str(to["type"]).lower(),
            to_key=_norm(to["key"]),
        )
        if rule.from_type not in _VALID_TYPES or rule.to_type not in _VALID_TYPES:
            raise GraphReconcileError(
                f"alias rule has invalid type: "
                f"{rule.from_type}:{_redact(rule.from_key)} → "
                f"{rule.to_type}:{_redact(rule.to_key)}"
            )
        if (rule.from_type, rule.from_key) == (rule.to_type, rule.to_key):
            raise GraphReconcileError(
                f"alias rule is a self-merge: {rule.from_type}:{_redact(rule.from_key)}"
            )
        rules.append(rule)
    _validate_alias_graph(rules)  # F7: reject duplicate sources / chains / cycles
    return rules


def _validate_alias_graph(rules: list[AliasRule]) -> None:
    """Reject an ill-formed alias graph (F7).

    A source ``(type, key)`` may appear at most once (no ambiguous duplicate
    targets), and a source may not also be some other rule's target (no
    transitive chains/cycles like A→B→C or A→B→A), because the merge is
    single-pass and order-independent.

    Error messages name type + a redacted key only (F5), never the full mapping.
    """
    targets = {(r.to_type, r.to_key) for r in rules}
    seen: set[tuple[str, str]] = set()
    for r in rules:
        src = (r.from_type, r.from_key)
        if src in seen:
            raise GraphReconcileError(
                f"duplicate alias source: {r.from_type}:{_redact(r.from_key)}"
            )
        seen.add(src)
        if src in targets:
            raise GraphReconcileError(
                f"alias chain/cycle: {r.from_type}:{_redact(r.from_key)} "
                "is both a source and a target"
            )


def _redact(key: str) -> str:
    """Return a safe, non-identifying prefix of *key* for error/log messages (F5)."""
    return key[:2] + "…" if len(key) > 2 else "…"


# ---------------------------------------------------------------------------
# C2 — apply_aliases: FK re-point only (F2: never DELETE graph_entities rows)
# ---------------------------------------------------------------------------


class _Rollback(Exception):  # noqa: N818 — internal control-flow marker, not user-facing
    """Internal control-flow exception used to unwind a dry-run savepoint.

    Raised at the end of a successful per-rule transaction when ``dry_run`` is
    true so that ``psycopg``'s context-managed ``SAVEPOINT`` rolls the row
    changes back without surfacing as a real error to the caller.
    """


def apply_aliases(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    rules: list[AliasRule],
    *,
    dry_run: bool = False,
) -> AliasResult:
    """Re-point each rule's source mentions+contributions onto its target.

    Leaves the source ``graph_entities`` row in place (zero-mention). The caller
    is then responsible for (1) upserting target AGE vertices and
    (2) running ``refresh_aggregates`` — whose existing orphan-GC step deletes
    the zero-mention source relationally **and** detach-deletes its AGE vertex
    (F2). This function intentionally never touches ``graph_entities`` rows,
    ``graph_relationships``, ``doc_count``, or the AGE graph.

    Per rule the work runs inside a ``with conn.transaction()`` SAVEPOINT so
    failure rolls back only that rule. ``dry_run=True`` rolls every savepoint
    back after counting; the returned summary still reports what *would* move.

    Idempotent: an absent source ``(from_type, from_key)`` is a silent no-op
    (does not increment ``rules_applied``). ``rules_total`` always reflects the
    input count.

    SQL is parameterized throughout — entity ids never concatenate into a
    string. Catches only the internal :class:`_Rollback` sentinel; real
    ``psycopg`` errors propagate so callers see actionable failures.
    """
    mentions_repointed = 0
    contributions_repointed = 0
    applied = 0
    orphaned = 0

    for rule in rules:
        src_id = _entity_id_by_key(conn, tenant_id, rule.from_type, rule.from_key)
        if src_id is None:
            # F2 idempotency: source absent (already merged / never existed) — skip.
            continue
        try:
            with conn.transaction():  # SAVEPOINT
                dst_id = _upsert_entity(conn, tenant_id, rule.to_type, rule.to_key)
                mentions_repointed += _repoint_mentions(
                    conn, tenant_id, src_id, dst_id
                )
                contributions_repointed += _repoint_contributions(
                    conn, tenant_id, src_id, dst_id
                )
                applied += 1
                orphaned += 1  # source is now zero-mention (GC handled by caller)
                if dry_run:
                    raise _Rollback
        except _Rollback:
            # Counters above were tallied before the rollback unwound the row
            # changes, so the dry-run summary still reports the would-be moves.
            pass

    return AliasResult(
        tenant_id=tenant_id,
        rules_total=len(rules),
        rules_applied=applied,
        mentions_repointed=mentions_repointed,
        contributions_repointed=contributions_repointed,
        sources_orphaned=orphaned,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# SQL helpers — all parameterized (NEVER concat ids/keys into SQL strings).
# ---------------------------------------------------------------------------


def _entity_id_by_key(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    entity_type: str,
    canonical_key: str,
) -> str | None:
    """Return the durable entity id for ``(tenant, type, key)`` or ``None``."""
    row = conn.execute(
        "SELECT id::text FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = %s AND canonical_key = %s",
        (tenant_id, entity_type, canonical_key),
    ).fetchone()
    return None if row is None else str(row[0])


def _upsert_entity(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    entity_type: str,
    canonical_key: str,
) -> str:
    """Find-or-create the target entity and return its id::text.

    Uses the migration-012 UNIQUE ``(tenant_id, entity_type, canonical_key)``
    constraint so a re-merge re-uses the existing row. ``name`` is set to the
    humanized canonical key on every call — reuses
    :func:`brain.person_name.humanize_person_name` so the alias surface
    matches the people-resolver display-name shape (DRY).
    """
    row = conn.execute(
        """
        INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (tenant_id, entity_type, canonical_key) DO UPDATE SET
            name = EXCLUDED.name,
            updated_at = NOW()
        RETURNING id::text
        """,
        (tenant_id, entity_type, humanize_person_name(canonical_key), canonical_key),
    ).fetchone()
    # RETURNING on INSERT ... ON CONFLICT DO UPDATE always yields one row.
    assert row is not None
    return str(row[0])


def _repoint_mentions(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    src_id: str,
    dst_id: str,
) -> int:
    """Move every source-entity mention onto *dst_id*; return rows moved.

    Two-step: INSERT the source rows under ``dst_id`` (collapsing the PK
    ``(tenant_id, entity_id, document_id)`` on conflict), then DELETE the old
    source-keyed rows so the source ends up zero-mention. Returns the number of
    source rows that existed pre-move (= the number routed to the target).

    The on-conflict collapse is **aspect-aware** because ``mention_count`` means
    different things per aspect. Person mentions (``source = 'people'``) are
    *presence flags* — always ``1`` — so summing two ``1``s into ``2`` when both
    the source and target already mention the same document would fabricate a
    count the reconcile pipeline never writes. For those we keep presence via
    ``GREATEST`` (``1``). Concept mentions carry real per-document counts, so
    they still *sum*. The determinant is the row's ``source`` (not entity type):
    if either the existing target row or the incoming source row is a
    people-presence mention, clamp; otherwise sum.
    """
    moved_row = conn.execute(
        """
        WITH moved AS (
            INSERT INTO graph_entity_mentions
                (tenant_id, entity_id, document_id, mention_count, source)
            SELECT tenant_id, %(dst)s, document_id, mention_count, source
            FROM graph_entity_mentions
            WHERE tenant_id = %(tenant)s AND entity_id = %(src)s
            ON CONFLICT (tenant_id, entity_id, document_id)
            DO UPDATE SET
                mention_count = CASE
                    WHEN graph_entity_mentions.source = %(people)s
                         OR EXCLUDED.source = %(people)s
                    THEN GREATEST(
                        graph_entity_mentions.mention_count,
                        EXCLUDED.mention_count
                    )
                    ELSE graph_entity_mentions.mention_count
                         + EXCLUDED.mention_count
                END
            RETURNING 1
        )
        SELECT count(*) FROM moved
        """,
        {
            "dst": dst_id,
            "tenant": tenant_id,
            "src": src_id,
            "people": _PEOPLE_MENTION_SOURCE,
        },
    ).fetchone()
    assert moved_row is not None
    moved = int(moved_row[0])
    conn.execute(
        "DELETE FROM graph_entity_mentions WHERE tenant_id = %s AND entity_id = %s",
        (tenant_id, src_id),
    )
    return moved


def _repoint_contributions(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    src_id: str,
    dst_id: str,
) -> int:
    """Move every source-touching contribution edge onto *dst_id*.

    The ``graph_edge_contributions`` PK is
    ``(tenant_id, document_id, src_id, dst_id)`` with CHECK ``src_id < dst_id``,
    so naively swapping the endpoint id breaks the canonical ordering. We
    therefore re-canonicalize via ``LEAST/GREATEST`` after substituting, then:

    * **drop self-edges** (``LEAST == GREATEST``) — re-pointing produces a
      self-edge when the variant already co-occurred with the target in the
      same document.
    * collapse PK conflicts **aspect-aware** — like :func:`_repoint_mentions`,
      person-person co-occurrence edges are *presence flags* (the people
      pipeline emits ``cooccur_count = 1`` for every same-doc pair), so summing
      two ``1``s when the source and target both co-occurred with a third person
      in the same document would fabricate a weight. ``graph_edge_contributions``
      carries no ``source`` column, so the aspect is derived from the endpoints'
      types: a conflict whose two endpoints are BOTH ``person`` keeps presence
      via ``GREATEST``; every other edge (concept-concept, or a cross-type merge)
      still *sums*.
    * delete the old source-touching rows so the source is fully detached at
      the contributions table too.

    Returns the number of source-touching rows that existed pre-move (= the
    number considered for the target).
    """
    moved_row = conn.execute(
        """
        WITH source_rows AS (
            SELECT
                tenant_id,
                document_id,
                LEAST(
                    CASE WHEN src_id = %(src)s THEN %(dst)s::uuid ELSE src_id END,
                    CASE WHEN dst_id = %(src)s THEN %(dst)s::uuid ELSE dst_id END
                ) AS new_src,
                GREATEST(
                    CASE WHEN src_id = %(src)s THEN %(dst)s::uuid ELSE src_id END,
                    CASE WHEN dst_id = %(src)s THEN %(dst)s::uuid ELSE dst_id END
                ) AS new_dst,
                cooccur_count
            FROM graph_edge_contributions
            WHERE tenant_id = %(tenant)s
              AND (src_id = %(src)s OR dst_id = %(src)s)
        ),
        rewritten AS (
            SELECT tenant_id, document_id, new_src, new_dst, cooccur_count
            FROM source_rows
            WHERE new_src <> new_dst  -- drop self-edges
        ),
        upserted AS (
            INSERT INTO graph_edge_contributions
                (tenant_id, document_id, src_id, dst_id, cooccur_count)
            SELECT tenant_id, document_id, new_src, new_dst, cooccur_count
            FROM rewritten
            ON CONFLICT (tenant_id, document_id, src_id, dst_id)
            DO UPDATE SET
                cooccur_count = CASE
                    WHEN (
                        SELECT bool_and(ge.entity_type = 'person')
                        FROM graph_entities ge
                        WHERE ge.tenant_id = graph_edge_contributions.tenant_id
                          AND ge.id IN (
                              graph_edge_contributions.src_id,
                              graph_edge_contributions.dst_id
                          )
                    )
                    THEN GREATEST(
                        graph_edge_contributions.cooccur_count,
                        EXCLUDED.cooccur_count
                    )
                    ELSE graph_edge_contributions.cooccur_count
                         + EXCLUDED.cooccur_count
                END
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM source_rows)
        """,
        {"tenant": tenant_id, "src": src_id, "dst": dst_id},
    ).fetchone()
    assert moved_row is not None
    moved = int(moved_row[0])
    conn.execute(
        "DELETE FROM graph_edge_contributions "
        "WHERE tenant_id = %s AND (src_id = %s OR dst_id = %s)",
        (tenant_id, src_id, src_id),
    )
    return moved


# ---------------------------------------------------------------------------
# C3 — merge_aliases: atomic orchestrator (F2)
#
# Wraps the full apply → upsert AGE targets → refresh aggregates flow in ONE
# transaction so the relational re-point, the AGE vertex provisioning, and the
# GC + AGE-detach + edge rebuild commit or roll back together. ``dry_run``
# rolls everything back via the internal :class:`_Rollback` sentinel while
# still returning the counters that ``apply_aliases`` tallied.
# ---------------------------------------------------------------------------


def merge_aliases(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    rules: list[AliasRule],
    backend: GraphBackend,
    *,
    dry_run: bool = False,
    config: ReconcileConfig | None = None,
) -> AliasResult:
    """Apply curated alias rules atomically, then refresh tenant aggregates (F2).

    The single corpus-level entry point used by ``brain graphrag aliases apply``,
    the ``build`` / ``refresh`` CLI wiring, and the MCP twin
    ``brain_graphrag_aliases_apply``. Orchestrates the three steps that together
    leave the relational source-of-truth and the AGE mirror consistent:

    1. :func:`apply_aliases` re-points every source entity's mentions +
       contributions onto its rule target, leaving the source ``graph_entities``
       row in place (zero-mention; F2 — never DELETEd here).
    2. ``backend.upsert_entities`` MERGEs the rule targets' AGE vertices so
       newly-created targets exist before
       :meth:`~brain.graph_rag.aggregates.refresh_aggregates`'s
       ``refresh_cooccur_edges`` looks them up (which raises on a missing
       vertex; F2).
    3. :func:`~brain.graph_rag.aggregates.refresh_aggregates` rebuilds the
       tenant's derived ``graph_relationships`` from the contributions,
       GCs the now-zero-mention sources relationally **and** ``detach delete``
       their AGE vertices, and rematerializes the AGE ``CO_OCCURS`` edges.

    Steps 1-3 run inside ONE ``with conn.transaction()`` so a failure at step 2
    or 3 rolls back the re-point at step 1 — the graph never lands in a
    half-merged state. ``dry_run=True`` runs step 1 only and raises
    :class:`_Rollback` to unwind the transaction without persisting; the
    returned :class:`AliasResult` still reports the would-be moves and has
    ``dry_run=True``.

    Empty ``rules`` short-circuits before opening a transaction and returns a
    zero-valued result (the wiring sites can call this unconditionally).

    ``config`` is an optional :class:`ReconcileConfig` forwarded to
    ``refresh_aggregates`` so the caller's ``generic_df_ratio`` /
    ``suppress_ver`` etc. apply to the post-merge edge recompute. When ``None``
    the default :class:`ReconcileConfig` is used (its ``tenant_id`` is replaced
    with the caller's). When supplied, its ``tenant_id`` MUST match ``tenant_id``
    so the apply step (using ``tenant_id``) and the refresh step (using
    ``config.tenant_id``) can never diverge.
    """
    if not rules:
        return AliasResult(tenant_id=tenant_id, rules_total=0, dry_run=dry_run)

    # Late imports keep this module import-cheap (avoid pulling reconcile +
    # aggregates + backends at module-load time) and break the import cycle
    # that would otherwise form between aliases.py and aggregates.py (which is
    # allowed to depend on aliases at a later G-wave but not at module-load).
    from ..aggregates import refresh_aggregates  # noqa: PLC0415 — break cycle
    from ..reconcile import ReconcileConfig  # noqa: PLC0415 — break cycle

    refresh_config: ReconcileConfig
    if config is None:
        refresh_config = ReconcileConfig(tenant_id=tenant_id)
    elif config.tenant_id != tenant_id:
        raise GraphReconcileError(
            "merge_aliases: config.tenant_id "
            f"({config.tenant_id!r}) does not match tenant_id ({tenant_id!r})"
        )
    else:
        refresh_config = config

    captured: dict[str, AliasResult] = {}
    try:
        with conn.transaction():
            res = apply_aliases(conn, tenant_id, rules, dry_run=False)
            if dry_run:
                # Record the counters BEFORE unwinding so the dry-run summary
                # still reports what WOULD have moved. The _Rollback raise
                # unwinds the savepoint without surfacing as a real error.
                captured["res"] = AliasResult(
                    tenant_id=res.tenant_id,
                    rules_total=res.rules_total,
                    rules_applied=res.rules_applied,
                    mentions_repointed=res.mentions_repointed,
                    contributions_repointed=res.contributions_repointed,
                    sources_orphaned=res.sources_orphaned,
                    dry_run=True,
                )
                raise _Rollback
            # Gate the AGE upsert + corpus refresh on `rules_applied > 0`:
            # when no rule's source entity existed in this corpus (a config
            # carried over from another brain, or an already-merged corpus),
            # `apply_aliases` re-pointed NOTHING — the derived layers are
            # already consistent, so the post-apply target upsert + the
            # whole-tenant `refresh_aggregates` (with its O(R) CO_OCCURS
            # rematerialization) are pure waste. They also create the only
            # branch where the find-or-create `_upsert_entity` from a no-op
            # rule could materialize an unused target vertex in AGE.
            if res.rules_applied > 0:
                # F2: ensure every rule TARGET has an AGE vertex before
                # refresh_cooccur_edges looks it up. A target that was created
                # by apply_aliases (find-or-create via _upsert_entity) has a
                # relational row but no AGE vertex yet; without this upsert
                # the post-merge CO_OCCURS rebuild would raise
                # GraphBackendError on the first contribution touching the
                # new target.
                target_pairs = list({(r.to_type, r.to_key) for r in rules})
                targets = _fetch_entities_by_keys(conn, tenant_id, target_pairs)
                if targets:
                    backend.upsert_entities(conn, tenant_id, targets)
                # Refresh: recompute graph_relationships from contributions,
                # GC the now-zero-mention source entities + DETACH DELETE
                # their AGE vertices, rematerialize CO_OCCURS. Runs inside
                # this outer transaction (nested as a SAVEPOINT) so the
                # whole flow commits or rolls back together.
                refresh_aggregates(conn, backend=backend, config=refresh_config)
            captured["res"] = res
    except _Rollback:
        # Counters were tallied before the rollback unwound the row changes,
        # so the dry-run summary still reports the would-be moves.
        pass
    return captured["res"]


def _fetch_entities_by_keys(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    type_key_pairs: list[tuple[str, str]],
) -> list[GraphEntity]:
    """Resolve ``(entity_type, canonical_key)`` pairs to :class:`GraphEntity` rows.

    De-duplicates the input pairs (multiple alias rules can share a target),
    then runs ONE tenant-scoped ``SELECT`` over ``graph_entities`` and maps each
    row via :func:`brain.graph_rag._retrieval_common._row_to_entity` so the
    :class:`GraphEntity` shape stays in lockstep with the rest of the read path
    (DRY — G2 split). Pairs that resolve to no row are silently dropped
    (nothing to AGE-upsert when nothing was created). SQL is parameterized.
    """
    if not type_key_pairs:
        return []
    # Late import keeps this module import-cheap and breaks the cycle with
    # retrieve.py / _retrieval_common (which imports schema, which we already
    # have — but keeping it lazy mirrors the other late imports in this file).
    from .._retrieval_common import _row_to_entity  # noqa: PLC0415

    deduped = sorted(set(type_key_pairs))
    types = [t for t, _ in deduped]
    keys = [k for _, k in deduped]
    rows = conn.execute(
        "SELECT id::text, entity_type, name, canonical_key, description, doc_count "
        "FROM graph_entities "
        "WHERE tenant_id = %s "
        "AND (entity_type, canonical_key) IN ("
        "  SELECT unnest(%s::text[]), unnest(%s::text[])"
        ")",
        (tenant_id, types, keys),
    ).fetchall()
    return [_row_to_entity(row, tenant_id) for row in rows]
