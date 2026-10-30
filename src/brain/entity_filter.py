"""Resolve a ``--entity`` name to graph entity ids for the search filter (RM-24).

``brain search --entity`` and MCP ``brain_search(entity=...)`` keep only documents
that mention a graph entity. This module turns the name into entity ids and checks
that the graph can answer; :func:`brain.search_predicate.build_predicate` turns
the ids into SQL. It reads only the relational graph tables from migration 012
(``graph_entities``, ``graph_entity_mentions``), never the AGE mirror.

The ``brain.graph_rag`` imports are inside the functions: importing that package
pulls in the whole graph layer, and a search without ``--entity`` never needs it.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import yaml

from .errors import (
    EntityAmbiguous,
    EntityFilterError,
    EntityGraphUnavailable,
    EntityNotFound,
    GraphReconcileError,
    describe_failure,
)

if TYPE_CHECKING:
    from .config import Config
    from .graph_rag.aliases import AliasRule

#: The error for a graph that is off or has no tables (the RM-24 ticket's wording).
GRAPH_REQUIRED_MESSAGE = (
    "`--entity` needs the graph: set BRAIN_GRAPH_ENABLED=true and run `brain graphrag build`"
)

#: The fixed half of the error for an alias file that cannot be loaded.
ALIAS_FILE_MESSAGE = "`--entity` could not load the alias rules in BRAIN_GRAPH_ALIASES_PATH"

#: What ``load_alias_rules`` raises on a bad file: a rule it refuses, malformed YAML,
#: an unreadable or undecodable file, or an entry of the wrong shape.
_ALIAS_LOAD_FAILURES = (
    GraphReconcileError,
    yaml.YAMLError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)

#: The ``graph_entities.entity_type`` CHECK set (migration 012).
ENTITY_TYPES = ("org", "person", "project", "tool", "topic")

_TABLES_PRESENT = (
    "SELECT to_regclass('graph_entities') IS NOT NULL"
    " AND to_regclass('graph_entity_mentions') IS NOT NULL"
)
_BY_KEY = (
    "SELECT id::text, entity_type, canonical_key FROM graph_entities"
    " WHERE tenant_id = %s AND canonical_key = ANY(%s::text[])"
    " AND entity_type = ANY(%s::text[])"
)
_BY_NAME = (
    "SELECT id::text, entity_type, canonical_key FROM graph_entities"
    " WHERE tenant_id = %s AND lower(name) = lower(%s)"
    " AND entity_type = ANY(%s::text[])"
)
_BY_TYPE_AND_KEY = (
    "SELECT id::text, entity_type, canonical_key FROM graph_entities"
    " WHERE tenant_id = %s AND entity_type = %s AND canonical_key = %s"
)

_Row = tuple[str, str, str]  # (id, entity_type, canonical_key)


def graph_tables_present(conn: psycopg.Connection[Any]) -> bool:
    """True when both graph tables the filter reads exist."""
    row = conn.execute(_TABLES_PRESENT).fetchone()
    return bool(row and row[0])


def _keys(name: str) -> list[str]:
    """The keys a name can be stored under: its ``lower(name)`` form and the concept key.

    ``lower(name)`` with whitespace collapsed is the person and alias-rule key form.
    Concepts are keyed by the strip-all form (concepts-v6), so ``Acme Platform`` is
    stored as ``acmeplatform``.
    """
    from .graph_rag.aliases import _norm
    from .graph_rag.extract import _canonical_key

    return sorted({key for key in (_norm(name), _canonical_key(name)) if key})


def _types(entity_type: str | None) -> list[str]:
    if entity_type is None:
        return list(ENTITY_TYPES)
    if entity_type not in ENTITY_TYPES:
        raise EntityFilterError(
            f"--entity-type must be one of {'/'.join(ENTITY_TYPES)} (got {entity_type!r})"
        )
    return [entity_type]


def _alias_targets(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    sources: set[tuple[str, str]],
    rules: Sequence[AliasRule],
) -> list[_Row]:
    """The target entities of every rule whose ``(type, key)`` source is in ``sources``."""
    targets = {
        (rule.to_type, rule.to_key)
        for rule in rules
        if (rule.from_type, rule.from_key) in sources
    }
    rows: list[_Row] = []
    for to_type, to_key in sorted(targets):
        row = conn.execute(_BY_TYPE_AND_KEY, (tenant_id, to_type, to_key)).fetchone()
        if row is not None:
            rows.append((str(row[0]), str(row[1]), str(row[2])))
    return rows


def resolve_entity_ids(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    name: str,
    entity_type: str | None,
    rules: Sequence[AliasRule] = (),
) -> list[str]:
    """Return the ids of the graph entities ``name`` names, in ``tenant_id``.

    Order of matching: ``canonical_key`` (see :func:`_keys`), with any curated alias
    rule (``graph_rag.aliases``) applied, then, only when that found nothing, a
    case-insensitive ``name`` match. ``entity_type`` narrows the match.

    A rule ADDS its target to the result and keeps the source. Rules are merged
    into the graph only by ``brain graphrag build`` / ``refresh``; ingest and the
    watcher's sync apply none, so an unmerged source can still carry mentions of
    its own, and its target may not exist yet. A merged source has no mentions,
    so keeping it costs nothing, and a source already deleted by the orphan GC
    still reaches its target through the rule. Ambiguity is judged on each row's
    type AFTER the redirect: a source whose target was found counts as the
    target's type, so a cross-type rule (``topic`` → ``person``) is one entity.

    Raises:
        EntityNotFound: nothing matched.
        EntityAmbiguous: entities of several types matched and ``entity_type`` is None.
        EntityFilterError: ``entity_type`` is not a graph entity type.
    """
    types = _types(entity_type)
    keys = _keys(name)
    if not keys:
        raise EntityNotFound(name)
    target_of = {(r.from_type, r.from_key): (r.to_type, r.to_key) for r in rules}
    hits = [
        (str(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(_BY_KEY, (tenant_id, keys, types)).fetchall()
    ]
    named = {(t, k) for t in types for k in keys} | {(h[1], h[2]) for h in hits}
    rows = hits + _alias_targets(conn, tenant_id, named, rules)
    if not rows:
        found = [
            (str(r[0]), str(r[1]), str(r[2]))
            for r in conn.execute(_BY_NAME, (tenant_id, name.strip(), types)).fetchall()
        ]
        rows = found + _alias_targets(conn, tenant_id, {(h[1], h[2]) for h in found}, rules)
    if not rows:
        raise EntityNotFound(name)
    present = {(row[1], row[2]) for row in rows}
    found_types = sorted(
        {
            target[0] if (target := target_of.get((row[1], row[2]))) in present else row[1]
            for row in rows
        }
    )
    if entity_type is None and len(found_types) > 1:
        raise EntityAmbiguous(name, found_types)
    return sorted({row[0] for row in rows})


def _load_rules(path: Path | None) -> list[AliasRule]:
    """The curated alias rules, or :class:`EntityFilterError` naming the failure's class only.

    Search did not read this file before RM-24, so a bad file must not turn every
    ``--entity`` search into a traceback (CLI) or a generic error (MCP). The message
    is a fixed sentence plus :func:`describe_failure`, never ``str(exc)``: a YAML
    error quotes the offending line, and ``GraphReconcileError``'s invalid-type
    message prints the file's type field unredacted. Either can carry a real name.
    """
    from .graph_rag.aliases import load_alias_rules

    try:
        return load_alias_rules(path)
    except _ALIAS_LOAD_FAILURES as exc:
        raise EntityFilterError(f"{ALIAS_FILE_MESSAGE} ({describe_failure(exc)})") from exc


def search_kwargs(
    conn: psycopg.Connection[Any],
    cfg: Config,
    name: str | None,
    entity_type: str | None,
) -> dict[str, Any]:
    """The ``entity_ids`` / ``entity_tenant`` keyword arguments for one search, or ``{}``.

    Splatted into ``run_search`` (which forwards them to ``hybrid_search``) and into
    the facet predicate's ``build_predicate``, so the ranked rows, the total and the
    facets see one filter.

    Raises:
        EntityGraphUnavailable: the graph is disabled or its tables are absent.
        EntityFilterError: ``entity_type`` without ``name``, an unknown type, or an
            alias file that cannot be loaded.
        EntityNotFound / EntityAmbiguous: from :func:`resolve_entity_ids`.
    """
    if name is None:
        if entity_type is not None:
            raise EntityFilterError("--entity-type needs --entity")
        return {}
    if not cfg.graph_enabled or not graph_tables_present(conn):
        raise EntityGraphUnavailable(GRAPH_REQUIRED_MESSAGE)
    from .graph_rag.tenancy import resolve_tenant

    tenant_id = resolve_tenant(cfg)
    entity_ids = resolve_entity_ids(
        conn,
        tenant_id=tenant_id,
        name=name,
        entity_type=entity_type,
        rules=_load_rules(cfg.graph_aliases_path),
    )
    return {"entity_ids": entity_ids, "entity_tenant": tenant_id}
