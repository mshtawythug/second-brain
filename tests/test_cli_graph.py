"""CLI integration tests for ``brain graph``."""
import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)


def _make_doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    kind: str = "vault",
    vault_path: str | None = None,
) -> str:
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, vault_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (doc_id, title, f"body of {title}", f"hash-{doc_id}", "note", kind, vault_path),
    )
    return doc_id


def _link(
    conn: psycopg.Connection[Any],
    *,
    src: str,
    dst: str,
    text: str = "[[X]]",
) -> None:
    conn.execute(
        """
        INSERT INTO links
          (src_document_id, dst_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, 'wiki', NULL)
        """,
        (src, dst, text),
    )


def _derived(
    conn: psycopg.Connection[Any],
    *,
    a: str,
    b: str,
    rule: str = "shared_thread",
    weight: float = 1.0,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Insert a ``derived_links`` row in canonical (LEAST, GREATEST) order."""
    src, dst = (a, b) if a < b else (b, a)
    payload = {} if evidence is None else evidence
    conn.execute(
        """
        INSERT INTO derived_links
          (src_document_id, dst_document_id, rule, evidence, weight)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        """,
        (src, dst, rule, json.dumps(payload), weight),
    )


def _seed_chain(conn: psycopg.Connection[Any]) -> dict[str, str]:
    a = _make_doc(conn, doc_id="11111111-1111-1111-1111-111111111111",
                  title="A", vault_path="a.md")
    b = _make_doc(conn, doc_id="22222222-2222-2222-2222-222222222222",
                  title="B", vault_path="b.md")
    c = _make_doc(conn, doc_id="33333333-3333-3333-3333-333333333333",
                  title="C", vault_path="c.md")
    _link(conn, src=a, dst=b, text="[[B]]")
    _link(conn, src=b, dst=c, text="[[C]]")
    return {"a": a, "b": b, "c": c}


def test_graph_default_format_is_json(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    _seed_chain(test_db)
    result = CliRunner().invoke(app, ["graph"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "nodes" in payload
    assert "edges" in payload
    titles = {n["title"] for n in payload["nodes"]}
    assert titles == {"A", "B", "C"}


def test_graph_dot_format(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    _seed_chain(test_db)
    result = CliRunner().invoke(app, ["graph", "--format", "dot"])
    assert result.exit_code == 0
    assert "digraph G {" in result.output
    assert "n_11111111" in result.output


def test_graph_mermaid_format(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    _seed_chain(test_db)
    result = CliRunner().invoke(app, ["graph", "--format", "mermaid"])
    assert result.exit_code == 0
    assert "graph TD" in result.output
    assert "n_11111111" in result.output


def test_graph_unknown_format_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["graph", "--format", "yaml"])
    assert result.exit_code != 0
    assert "format" in result.output.lower()


def test_graph_root_focuses_subgraph(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    ids = _seed_chain(test_db)
    result = CliRunner().invoke(
        app, ["graph", "--root", ids["a"][:8], "--depth", "1"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    titles = {n["title"] for n in payload["nodes"]}
    # Depth-1 from A reaches B but not C.
    assert "A" in titles
    assert "B" in titles
    assert "C" not in titles


def test_graph_depth_without_root_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["graph", "--depth", "1"])
    assert result.exit_code != 0
    # Typer's BadParameter error.
    assert "depth" in result.output.lower()


def test_graph_negative_depth_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    ids = _seed_chain(test_db)
    result = CliRunner().invoke(
        app, ["graph", "--root", ids["a"][:8], "--depth", "-1"]
    )
    assert result.exit_code != 0


def test_graph_unknown_root_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    result = CliRunner().invoke(app, ["graph", "--root", "00000000"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_graph_include_ingested_pulls_in_orphans(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    _seed_chain(test_db)
    _make_doc(
        test_db,
        doc_id="44444444-4444-4444-4444-444444444444",
        title="lonely ingested",
        kind="ingested",
        vault_path="_ingested/krisp/lonely.md",
    )
    # Default — ingested orphan dropped.
    default = CliRunner().invoke(app, ["graph"])
    payload_default = json.loads(default.stdout)
    titles_default = {n["title"] for n in payload_default["nodes"]}
    assert "lonely ingested" not in titles_default
    # With --include-ingested.
    result = CliRunner().invoke(app, ["graph", "--include-ingested"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    titles = {n["title"] for n in payload["nodes"]}
    assert "lonely ingested" in titles


def test_graph_out_writes_to_path(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
) -> None:
    _set_env(monkeypatch)
    _seed_chain(test_db)
    out_path = tmp_path / "g.json"
    result = CliRunner().invoke(
        app, ["graph", "--format", "json", "--out", str(out_path)]
    )
    assert result.exit_code == 0, result.output
    assert out_path.is_file()
    payload = json.loads(out_path.read_text())
    titles = {n["title"] for n in payload["nodes"]}
    assert titles == {"A", "B", "C"}
    # Stdout shouldn't contain the JSON itself when --out is given.
    assert "wrote" in result.output


def test_graph_default_includes_derived_edges(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    """Without ``--no-derived``, derived edges show up in the JSON edge list.

    Per spec §10 Q4: derived edges are on by default in every read path.
    """
    _set_env(monkeypatch)
    ids = _seed_chain(test_db)
    _derived(
        test_db,
        a=ids["a"],
        b=ids["c"],
        rule="same_day_participant",
        weight=0.7,
    )
    result = CliRunner().invoke(app, ["graph"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    kinds = {e["kind"] for e in payload["edges"]}
    assert "derived" in kinds
    derived_edges = [e for e in payload["edges"] if e["kind"] == "derived"]
    assert len(derived_edges) == 1
    assert derived_edges[0]["rule"] == "same_day_participant"


def test_graph_no_derived_excludes_derived_edges(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    """``--no-derived`` drops every derived edge from the output."""
    _set_env(monkeypatch)
    ids = _seed_chain(test_db)
    _derived(
        test_db,
        a=ids["a"],
        b=ids["c"],
        rule="same_day_participant",
        weight=0.7,
    )
    result = CliRunner().invoke(app, ["graph", "--no-derived"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    kinds = {e["kind"] for e in payload["edges"]}
    assert "derived" not in kinds
    # Wiki edges from the chain still present (sanity).
    assert "wiki" in kinds


def test_graph_no_derived_dot_excludes_derived_edges(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    """The opt-out also propagates to the DOT formatter (no derived attrs)."""
    _set_env(monkeypatch)
    ids = _seed_chain(test_db)
    _derived(
        test_db,
        a=ids["a"],
        b=ids["c"],
        rule="shared_thread",
        weight=1.0,
    )
    # Default — derived attrs (style=bold, etc.) should appear.
    default = CliRunner().invoke(app, ["graph", "--format", "dot"])
    assert default.exit_code == 0
    assert "bold" in default.output  # _DOT_DERIVED_ATTRS for shared_thread
    # With --no-derived, the derived edge is gone, so no bold either.
    result = CliRunner().invoke(
        app, ["graph", "--format", "dot", "--no-derived"]
    )
    assert result.exit_code == 0, result.output
    assert "bold" not in result.output


def test_graph_empty_db_produces_valid_output(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
) -> None:
    _set_env(monkeypatch)
    # JSON.
    json_result = CliRunner().invoke(app, ["graph"])
    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload == {"nodes": [], "edges": []}
    # DOT.
    dot_result = CliRunner().invoke(app, ["graph", "--format", "dot"])
    assert dot_result.exit_code == 0
    assert "digraph G {" in dot_result.output
    # Mermaid.
    mermaid_result = CliRunner().invoke(app, ["graph", "--format", "mermaid"])
    assert mermaid_result.exit_code == 0
    assert "graph TD" in mermaid_result.output


# ===========================================================================
# Wave C3 — `brain graphrag aliases apply` (nested Typer group)
# Synthetic alias rules only (PII rule 15) — never real entity names.
# ===========================================================================


def test_graphrag_aliases_apply_empty_rules_is_noop_text(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — fixture readies the DB
) -> None:
    """No rules file → human output prints the 'no rules configured' hint."""
    _set_env(monkeypatch)
    monkeypatch.delenv("BRAIN_GRAPH_ALIASES_PATH", raising=False)
    res = CliRunner().invoke(app, ["graphrag", "aliases", "apply"])
    assert res.exit_code == 0, res.output
    assert "no rules configured" in res.output
    # Bare apply with no rules MUST NOT print the apply counters footer (no
    # rules ran). Match on the counter substring rather than the verb prefix —
    # the "no rules configured" hint itself leads with the same prefix.
    assert "rule(s) applied" not in res.output
    assert "source(s) orphaned" not in res.output


def test_graphrag_aliases_apply_empty_rules_is_noop_json(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001
) -> None:
    """No rules file + ``--json`` → emits the zero-valued AliasResult payload.

    C4 parity (review fix #2): the empty-rules CLI JSON shape MUST match the
    MCP empty-rules wire shape — 7 ``AliasResult`` fields + the
    ``communities_refresh_recommended`` staleness hint = 8 keys. Empty rules
    can't dirty the community partition, so the hint is always ``False`` here.
    """
    _set_env(monkeypatch)
    monkeypatch.delenv("BRAIN_GRAPH_ALIASES_PATH", raising=False)
    res = CliRunner().invoke(app, ["graphrag", "aliases", "apply", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["rules_total"] == 0
    assert payload["rules_applied"] == 0
    assert payload["dry_run"] is False
    # Empty rules → never dirty → staleness hint is always False.
    assert payload["communities_refresh_recommended"] is False
    # The empty-rules wire shape is locked at 8 keys (alias_result_json's 7 +
    # communities_refresh_recommended) — matches the MCP empty-rules payload.
    assert set(payload.keys()) == {
        "tenant_id",
        "rules_total",
        "rules_applied",
        "mentions_repointed",
        "contributions_repointed",
        "sources_orphaned",
        "dry_run",
        "communities_refresh_recommended",
    }


def test_graphrag_aliases_apply_dry_run_json(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
) -> None:
    """``aliases apply --dry-run --json`` reports the would-be merge + writes nothing.

    Seeds a synthetic source + target entity, points
    ``BRAIN_GRAPH_ALIASES_PATH`` at a one-rule YAML, then runs the dry-run.
    Asserts: the JSON tally shows the rule was applied (counters) but the DB
    state is unchanged (source mention count preserved).
    """
    _set_env(monkeypatch)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    # Seed: synthetic person + topic entities with one source mention.
    test_db.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES ('default', 'person', 'Sam Rivera', 'sam rivera'), "
        "       ('default', 'topic', 'Sam', 'sam')"
    )
    doc_row = test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES ('alias-dry', 'body', 'hash-alias-dry', 'note') RETURNING id"
    ).fetchone()
    assert doc_row is not None
    src_eid = test_db.execute(
        "SELECT id FROM graph_entities WHERE tenant_id='default' AND "
        "entity_type='topic' AND canonical_key='sam'"
    ).fetchone()
    assert src_eid is not None
    test_db.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, mention_count, source) "
        "VALUES ('default', %s, %s, 1, 'concepts')",
        (src_eid[0], doc_row[0]),
    )

    rules_yml = tmp_path / "aliases.yml"
    rules_yml.write_text(
        "rules:\n"
        "  - from: {type: topic, key: sam}\n"
        "    to:   {type: person, key: sam rivera}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BRAIN_GRAPH_ALIASES_PATH", str(rules_yml))

    res = CliRunner().invoke(
        app, ["graphrag", "aliases", "apply", "--dry-run", "--json"]
    )

    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["dry_run"] is True
    assert payload["rules_total"] == 1
    assert payload["rules_applied"] == 1
    assert payload["mentions_repointed"] == 1
    assert payload["sources_orphaned"] == 1
    assert payload["tenant_id"] == "default"

    # Dry-run wrote NOTHING: the source mention row is still there.
    moved_check = test_db.execute(
        "SELECT count(*) FROM graph_entity_mentions "
        "WHERE tenant_id = 'default' AND entity_id = %s",
        (src_eid[0],),
    ).fetchone()
    assert moved_check is not None and int(moved_check[0]) == 1


def test_graphrag_aliases_apply_writes_and_hints_communities_refresh(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
) -> None:
    """A real apply re-points mentions AND prints the communities-refresh hint."""
    _set_env(monkeypatch)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    # Seed: source (topic:sam) + target (person:sam rivera) with one source mention.
    test_db.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES ('default', 'person', 'Sam Rivera', 'sam rivera'), "
        "       ('default', 'topic', 'Sam', 'sam')"
    )
    doc_row = test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES ('alias-apply', 'body', 'hash-alias-apply', 'note') RETURNING id"
    ).fetchone()
    assert doc_row is not None
    src_eid = test_db.execute(
        "SELECT id FROM graph_entities WHERE tenant_id='default' AND "
        "entity_type='topic' AND canonical_key='sam'"
    ).fetchone()
    assert src_eid is not None
    test_db.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, mention_count, source) "
        "VALUES ('default', %s, %s, 1, 'concepts')",
        (src_eid[0], doc_row[0]),
    )

    rules_yml = tmp_path / "aliases.yml"
    rules_yml.write_text(
        "rules:\n"
        "  - from: {type: topic, key: sam}\n"
        "    to:   {type: person, key: sam rivera}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BRAIN_GRAPH_ALIASES_PATH", str(rules_yml))

    res = CliRunner().invoke(app, ["graphrag", "aliases", "apply"])

    assert res.exit_code == 0, res.output
    assert "graphrag aliases apply: 1/1 rule(s) applied" in res.output
    # Staleness hint MUST fire on a non-dry, non-empty apply.
    assert "communities may be stale" in res.output

    # Source row was GC'd by the embedded refresh_aggregates; target absorbed
    # the mention.
    src_check = test_db.execute(
        "SELECT id FROM graph_entities WHERE tenant_id='default' AND "
        "entity_type='topic' AND canonical_key='sam'"
    ).fetchone()
    assert src_check is None
    dst_check = test_db.execute(
        "SELECT count(*) FROM graph_entity_mentions m "
        "JOIN graph_entities e ON m.entity_id = e.id "
        "WHERE e.tenant_id = 'default' AND e.entity_type='person' "
        "AND e.canonical_key = 'sam rivera'"
    ).fetchone()
    assert dst_check is not None and int(dst_check[0]) == 1


def test_graphrag_aliases_apply_exits_when_age_absent(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """AGE absent → red error + exit 1 (mirrors build/refresh AGE guard)."""
    _set_env(monkeypatch)
    rules_yml = tmp_path / "aliases.yml"
    rules_yml.write_text(
        "rules:\n"
        "  - from: {type: org, key: acme}\n"
        "    to:   {type: org, key: acme corp}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BRAIN_GRAPH_ALIASES_PATH", str(rules_yml))
    monkeypatch.setattr("brain.cli.age_extension_available", lambda conn: False)
    res = CliRunner().invoke(app, ["graphrag", "aliases", "apply"])
    assert res.exit_code == 1
    assert "Apache AGE is not available" in res.output
