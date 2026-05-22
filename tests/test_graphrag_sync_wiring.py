"""Tests for the wave-G1-c graph-sync wiring (``brain.graph_rag.sync``).

Three layers:

* **Config parsing** — the ``BRAIN_GRAPH_*`` env surface resolves into
  :class:`brain.config.Config` with eager :class:`ConfigError` validation.
* **Unit (no live AGE)** — :func:`build_reconcile_config` shares ONE
  :class:`ReconcileConfig`; :class:`GraphSyncer` gates on the enabled flag +
  AGE availability and NEVER raises (a sync failure is logged and swallowed).
  Wiring into ``sync_vault`` / ``sync_one_file`` / ``_handle_delete`` is proven
  with a recording fake syncer (those paths are person-aspect-inert, so a
  recording double is the right granularity).
* **Live-AGE integration** (``test_db``, port 5434) — ``ingest_document`` /
  ``update_document`` reconcile the people graph, and ``GraphSyncer.remove`` /
  the ``brain rm`` CLI drop a doc from it, when graph sync is enabled.

All people are synthetic (alice / bob / carol); no PII. The schema + AGE graph
reset per test via the ``test_db`` fixture.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from typer.testing import CliRunner

from brain import mcp_server
from brain.cli import app
from brain.config import Config, ConfigError
from brain.db import DEFAULT_GRAPH_NAME
from brain.errors import GraphBackendError
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.reconcile import ReconcileConfig
from brain.graph_rag.sync import (
    GraphSyncer,
    build_reconcile_config,
    make_graph_syncer,
)
from brain.ingest import ExtractedDoc, ingest_document, update_document
from brain.vault.derived_links.directory import DirectoryStore
from brain.vault.frontmatter import dump_frontmatter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# Suppression-disabled ratio so the tiny test corpora still materialize edges
# (round(0.3 * 1) == 0 would suppress every edge at N == 1). Mirrors the
# ``_NO_SUPPRESS`` constant in ``test_graphrag_reconcile``.
_NO_SUPPRESS = 1.0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _enabled_syncer(*, generic_df_ratio: float = _NO_SUPPRESS) -> GraphSyncer:
    """An enabled syncer over a real :class:`AgeBackend` (live-AGE tests)."""
    return GraphSyncer(
        ReconcileConfig(generic_df_ratio=generic_df_ratio),
        enabled=True,
        backend=AgeBackend(),
    )


def _seed_directory(
    conn: psycopg.Connection[Any], pairs: Sequence[tuple[str, str]]
) -> None:
    """Insert ``(display_name, email)`` directory rows (source='gmail')."""
    store = DirectoryStore(conn)
    for name, email in pairs:
        store.upsert_pair(display_name=name, email=email, source="gmail")


def _gmail_doc(external_id: str, participants: Sequence[tuple[str, str]]) -> ExtractedDoc:
    """An :class:`ExtractedDoc` whose from/to headers carry ``participants``."""
    from_hdr = f"{participants[0][0]} <{participants[0][1]}>"
    to_hdr = ", ".join(f"{n} <{e}>" for n, e in participants[1:])
    return ExtractedDoc(
        title=f"thread {external_id}",
        content=f"Body of message {external_id} with some words.",
        content_type="email",
        source_path=None,
        metadata={"from": from_hdr, "to": to_hdr},
    )


def _person_keys(conn: psycopg.Connection[Any], tenant: str = "default") -> set[str]:
    rows = conn.execute(
        "SELECT canonical_key FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = 'person'",
        (tenant,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _cypher_scalar(
    conn: psycopg.Connection[Any], query: str, params: Mapping[str, Any]
) -> list[tuple[Any, ...]]:
    conn.execute('SET search_path = ag_catalog, "$user", public')
    try:
        rows = conn.execute(
            f"SELECT * FROM ag_catalog.cypher('{DEFAULT_GRAPH_NAME}', "
            f"$$ {query} $$, %s::ag_catalog.agtype) AS (v ag_catalog.agtype)",
            (json.dumps(params),),
        ).fetchall()
    finally:
        conn.execute("RESET search_path")
    return rows


def _age_entity_count(conn: psycopg.Connection[Any], tenant: str = "default") -> int:
    rows = _cypher_scalar(
        conn, "MATCH (e:Entity {tenant_id: $t}) RETURN count(e)", {"t": tenant}
    )
    return int(str(rows[0][0]))


def _age_document_count(conn: psycopg.Connection[Any], tenant: str = "default") -> int:
    rows = _cypher_scalar(
        conn, "MATCH (d:Document {tenant_id: $t}) RETURN count(d)", {"t": tenant}
    )
    return int(str(rows[0][0]))


def _age_cooccur_count(conn: psycopg.Connection[Any], tenant: str = "default") -> int:
    rows = _cypher_scalar(
        conn,
        "MATCH ()-[r:CO_OCCURS {tenant_id: $t}]->() RETURN count(r)",
        {"t": tenant},
    )
    return int(str(rows[0][0]))


class _RecordingSyncer:
    """Duck-typed :class:`GraphSyncer` double — records reconcile/remove calls.

    Used to prove the ``sync_vault`` / ``sync_one_file`` / ``_handle_delete``
    wiring invokes the hook for the right doc ids without standing up a live
    graph (those vault paths are person-aspect-inert by design).
    """

    def __init__(self) -> None:
        self.reconciled: list[str] = []
        self.removed: list[str] = []

    def reconcile(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        self.reconciled.append(document_id)

    def remove(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        self.removed.append(document_id)


# --------------------------------------------------------------------------- #
# 1. Config parsing + validation
# --------------------------------------------------------------------------- #
def test_graph_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # BRAIN_GRAPH_ENABLED is isolated from the local .env by the session-autouse
    # _force_graph_flags_default fixture (empty -> code default = disabled);
    # delenv'ing it here would instead let the .env file re-inject the flag.
    for key in (
        "BRAIN_GRAPH_TENANT",
        "BRAIN_GRAPH_COOCCUR_WINDOW",
        "BRAIN_GRAPH_MAX_ENTITIES_PER_DOC",
        "BRAIN_GRAPH_GENERIC_DF",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = Config.load()
    assert cfg.graph_enabled is False
    assert cfg.graph_tenant_id == "default"
    assert cfg.graph_cooccur_window == 3
    assert cfg.graph_max_entities == 40
    assert cfg.graph_generic_df_ratio == 0.30


@pytest.mark.parametrize("token", ["1", "true", "TRUE", "yes", "on"])
def test_graph_enabled_truthy(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", token)
    assert Config.load().graph_enabled is True


@pytest.mark.parametrize("token", ["0", "false", "no", "off", ""])
def test_graph_enabled_falsy(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", token)
    assert Config.load().graph_enabled is False


def test_graph_enabled_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "maybe")
    with pytest.raises(ConfigError, match="BRAIN_GRAPH_ENABLED"):
        Config.load()


def test_graph_max_entities_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_MAX_ENTITIES_PER_DOC", "none")
    assert Config.load().graph_max_entities is None


@pytest.mark.parametrize(
    ("var", "value", "needle"),
    [
        ("BRAIN_GRAPH_COOCCUR_WINDOW", "0", "BRAIN_GRAPH_COOCCUR_WINDOW"),
        ("BRAIN_GRAPH_COOCCUR_WINDOW", "abc", "BRAIN_GRAPH_COOCCUR_WINDOW"),
        ("BRAIN_GRAPH_MAX_ENTITIES_PER_DOC", "0", "BRAIN_GRAPH_MAX_ENTITIES_PER_DOC"),
        ("BRAIN_GRAPH_MAX_ENTITIES_PER_DOC", "-3", "BRAIN_GRAPH_MAX_ENTITIES_PER_DOC"),
        ("BRAIN_GRAPH_GENERIC_DF", "0", "BRAIN_GRAPH_GENERIC_DF"),
        ("BRAIN_GRAPH_GENERIC_DF", "1.5", "BRAIN_GRAPH_GENERIC_DF"),
        ("BRAIN_GRAPH_GENERIC_DF", "xyz", "BRAIN_GRAPH_GENERIC_DF"),
    ],
)
def test_graph_config_invalid_raises(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str, needle: str
) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError, match=needle):
        Config.load()


# --------------------------------------------------------------------------- #
# 1b. Wave-G2 config knobs (concept extraction + bounded retrieval; spec §10)
# --------------------------------------------------------------------------- #
_G2_GRAPH_ENV_VARS = (
    "BRAIN_GRAPH_CONCEPTS",
    "BRAIN_GRAPH_EXTRACT_MODEL",
    "BRAIN_GRAPH_DEPTH",
    "BRAIN_GRAPH_FRONTIER_CAP",
    "BRAIN_GRAPH_MAX_DEGREE",
    "BRAIN_GRAPH_MIN_EDGE_WEIGHT",
    "BRAIN_GRAPH_THEME_LIMIT",
)


def test_graph_g2_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _G2_GRAPH_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    cfg = Config.load()
    assert cfg.graph_concepts is False
    assert cfg.graph_extract_model == "llama3.1:8b"
    assert cfg.graph_depth == 2
    assert cfg.graph_frontier_cap == 200
    assert cfg.graph_max_degree == 50
    assert cfg.graph_min_edge_weight == 0.20
    assert cfg.graph_theme_limit == 5


@pytest.mark.parametrize("token", ["1", "true", "TRUE", "yes", "on"])
def test_graph_concepts_truthy(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_CONCEPTS", token)
    assert Config.load().graph_concepts is True


@pytest.mark.parametrize("token", ["0", "false", "no", "off", ""])
def test_graph_concepts_falsy(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_CONCEPTS", token)
    assert Config.load().graph_concepts is False


def test_graph_concepts_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_CONCEPTS", "maybe")
    with pytest.raises(ConfigError, match="BRAIN_GRAPH_CONCEPTS"):
        Config.load()


def test_graph_extract_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_EXTRACT_MODEL", "  qwen2.5:7b  ")
    # Trimmed, honored verbatim (no whitelist — any pullable Ollama model).
    assert Config.load().graph_extract_model == "qwen2.5:7b"


def test_graph_extract_model_blank_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_EXTRACT_MODEL", "   ")
    assert Config.load().graph_extract_model == "llama3.1:8b"


@pytest.mark.parametrize(
    ("var", "value", "attr", "expected"),
    [
        ("BRAIN_GRAPH_DEPTH", "3", "graph_depth", 3),
        ("BRAIN_GRAPH_FRONTIER_CAP", "500", "graph_frontier_cap", 500),
        ("BRAIN_GRAPH_MAX_DEGREE", "25", "graph_max_degree", 25),
        ("BRAIN_GRAPH_THEME_LIMIT", "8", "graph_theme_limit", 8),
    ],
)
def test_graph_g2_int_overrides(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str, attr: str, expected: int
) -> None:
    monkeypatch.setenv(var, value)
    assert getattr(Config.load(), attr) == expected


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("BRAIN_GRAPH_DEPTH", "0"),
        ("BRAIN_GRAPH_DEPTH", "-1"),
        ("BRAIN_GRAPH_DEPTH", "abc"),
        ("BRAIN_GRAPH_DEPTH", "2.5"),
        ("BRAIN_GRAPH_FRONTIER_CAP", "0"),
        ("BRAIN_GRAPH_FRONTIER_CAP", "-5"),
        ("BRAIN_GRAPH_FRONTIER_CAP", "lots"),
        ("BRAIN_GRAPH_MAX_DEGREE", "0"),
        ("BRAIN_GRAPH_MAX_DEGREE", "-2"),
        ("BRAIN_GRAPH_MAX_DEGREE", "huge"),
        ("BRAIN_GRAPH_THEME_LIMIT", "0"),
        ("BRAIN_GRAPH_THEME_LIMIT", "-1"),
        ("BRAIN_GRAPH_THEME_LIMIT", "five"),
    ],
)
def test_graph_g2_positive_int_invalid_raises(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError, match=f"{var} must be a positive integer"):
        Config.load()


@pytest.mark.parametrize("value", ["0", "0.0", "0.5", "1", "1.0"])
def test_graph_min_edge_weight_valid(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_MIN_EDGE_WEIGHT", value)
    assert Config.load().graph_min_edge_weight == float(value)


@pytest.mark.parametrize("value", ["-0.1", "1.5", "abc"])
def test_graph_min_edge_weight_invalid_raises(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_MIN_EDGE_WEIGHT", value)
    with pytest.raises(
        ConfigError, match=r"BRAIN_GRAPH_MIN_EDGE_WEIGHT must be a float in \[0.0, 1.0\]"
    ):
        Config.load()


def test_graph_g2_blank_int_float_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Whitespace-only values are treated as "unset" -> default (mirrors the
    # vector-floor / recency idiom), not "empty integer".
    monkeypatch.setenv("BRAIN_GRAPH_DEPTH", "   ")
    monkeypatch.setenv("BRAIN_GRAPH_FRONTIER_CAP", "   ")
    monkeypatch.setenv("BRAIN_GRAPH_MAX_DEGREE", "   ")
    monkeypatch.setenv("BRAIN_GRAPH_MIN_EDGE_WEIGHT", "   ")
    monkeypatch.setenv("BRAIN_GRAPH_THEME_LIMIT", "   ")
    cfg = Config.load()
    assert cfg.graph_depth == 2
    assert cfg.graph_frontier_cap == 200
    assert cfg.graph_max_degree == 50
    assert cfg.graph_min_edge_weight == 0.20
    assert cfg.graph_theme_limit == 5


# --------------------------------------------------------------------------- #
# 1c. Wave-G3 community config knobs (global community detection; spec §17c)
# --------------------------------------------------------------------------- #
_G3_GRAPH_ENV_VARS = (
    "BRAIN_GRAPH_COMMUNITY_RESOLUTION",
    "BRAIN_GRAPH_COMMUNITY_SEED",
    "BRAIN_GRAPH_COMMUNITY_MIN_SIZE",
    "BRAIN_GRAPH_COMMUNITY_JACCARD",
    "BRAIN_GRAPH_COMMUNITY_LIMIT",
    "BRAIN_GRAPH_COMMUNITY_MAX",
)


def test_graph_g3_community_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _G3_GRAPH_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    cfg = Config.load()
    assert cfg.graph_community_resolution == 1.0
    assert cfg.graph_community_seed == 1234
    assert cfg.graph_community_min_size == 3
    assert cfg.graph_community_jaccard == 0.5
    assert cfg.graph_community_limit == 5
    assert cfg.graph_community_max is None


@pytest.mark.parametrize(
    ("var", "value", "attr", "expected"),
    [
        ("BRAIN_GRAPH_COMMUNITY_RESOLUTION", "1.5", "graph_community_resolution", 1.5),
        ("BRAIN_GRAPH_COMMUNITY_SEED", "99", "graph_community_seed", 99),
        ("BRAIN_GRAPH_COMMUNITY_SEED", "0", "graph_community_seed", 0),
        ("BRAIN_GRAPH_COMMUNITY_MIN_SIZE", "2", "graph_community_min_size", 2),
        ("BRAIN_GRAPH_COMMUNITY_JACCARD", "0.75", "graph_community_jaccard", 0.75),
        ("BRAIN_GRAPH_COMMUNITY_JACCARD", "0", "graph_community_jaccard", 0.0),
        ("BRAIN_GRAPH_COMMUNITY_JACCARD", "1", "graph_community_jaccard", 1.0),
        ("BRAIN_GRAPH_COMMUNITY_LIMIT", "10", "graph_community_limit", 10),
        ("BRAIN_GRAPH_COMMUNITY_MAX", "500", "graph_community_max", 500),
    ],
)
def test_graph_g3_valid_overrides(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str, attr: str, expected: object
) -> None:
    monkeypatch.setenv(var, value)
    assert getattr(Config.load(), attr) == expected


@pytest.mark.parametrize("token", ["none", "unlimited", "NONE", "Unlimited"])
def test_graph_community_max_none(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_COMMUNITY_MAX", token)
    assert Config.load().graph_community_max is None


@pytest.mark.parametrize(
    ("var", "value", "needle"),
    [
        (
            "BRAIN_GRAPH_COMMUNITY_RESOLUTION",
            "0",
            "BRAIN_GRAPH_COMMUNITY_RESOLUTION must be a positive float",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_RESOLUTION",
            "-1",
            "BRAIN_GRAPH_COMMUNITY_RESOLUTION must be a positive float",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_RESOLUTION",
            "abc",
            "BRAIN_GRAPH_COMMUNITY_RESOLUTION must be a positive float",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_SEED",
            "-1",
            "BRAIN_GRAPH_COMMUNITY_SEED must be a non-negative integer",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_SEED",
            "1.5",
            "BRAIN_GRAPH_COMMUNITY_SEED must be a non-negative integer",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_SEED",
            "abc",
            "BRAIN_GRAPH_COMMUNITY_SEED must be a non-negative integer",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_MIN_SIZE",
            "0",
            "BRAIN_GRAPH_COMMUNITY_MIN_SIZE must be a positive integer",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_MIN_SIZE",
            "-2",
            "BRAIN_GRAPH_COMMUNITY_MIN_SIZE must be a positive integer",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_MIN_SIZE",
            "two",
            "BRAIN_GRAPH_COMMUNITY_MIN_SIZE must be a positive integer",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_JACCARD",
            "-0.1",
            r"BRAIN_GRAPH_COMMUNITY_JACCARD must be a float in \[0.0, 1.0\]",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_JACCARD",
            "1.5",
            r"BRAIN_GRAPH_COMMUNITY_JACCARD must be a float in \[0.0, 1.0\]",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_JACCARD",
            "xyz",
            r"BRAIN_GRAPH_COMMUNITY_JACCARD must be a float in \[0.0, 1.0\]",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_LIMIT",
            "0",
            "BRAIN_GRAPH_COMMUNITY_LIMIT must be a positive integer",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_LIMIT",
            "-1",
            "BRAIN_GRAPH_COMMUNITY_LIMIT must be a positive integer",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_LIMIT",
            "lots",
            "BRAIN_GRAPH_COMMUNITY_LIMIT must be a positive integer",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_MAX",
            "0",
            "BRAIN_GRAPH_COMMUNITY_MAX must be a positive integer or 'none'",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_MAX",
            "-3",
            "BRAIN_GRAPH_COMMUNITY_MAX must be a positive integer or 'none'",
        ),
        (
            "BRAIN_GRAPH_COMMUNITY_MAX",
            "huge",
            "BRAIN_GRAPH_COMMUNITY_MAX must be a positive integer or 'none'",
        ),
    ],
)
def test_graph_g3_invalid_raises(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str, needle: str
) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError, match=needle):
        Config.load()


def test_graph_g3_blank_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Whitespace-only values are treated as "unset" -> default (mirrors the G2
    # idiom), not "empty value".
    for key in _G3_GRAPH_ENV_VARS:
        monkeypatch.setenv(key, "   ")
    cfg = Config.load()
    assert cfg.graph_community_resolution == 1.0
    assert cfg.graph_community_seed == 1234
    assert cfg.graph_community_min_size == 3
    assert cfg.graph_community_jaccard == 0.5
    assert cfg.graph_community_limit == 5
    assert cfg.graph_community_max is None


def test_networkx_importable() -> None:
    """G3 detection (G3-b) depends on networkx Louvain — confirm it imports."""
    import networkx
    from networkx.algorithms.community import louvain_communities

    assert callable(louvain_communities)
    assert networkx.__version__  # non-empty version string


# --------------------------------------------------------------------------- #
# 2. build_reconcile_config — single shared object (no divergence)
# --------------------------------------------------------------------------- #
def test_build_reconcile_config_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_TENANT", "tenant-x")
    monkeypatch.setenv("BRAIN_GRAPH_COOCCUR_WINDOW", "5")
    monkeypatch.setenv("BRAIN_GRAPH_MAX_ENTITIES_PER_DOC", "12")
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "0.5")
    monkeypatch.setenv("BRAIN_OWNER_PARTICIPANTS", "owner@x.com, Owner Name")
    cfg = Config.load()
    rc = build_reconcile_config(cfg)
    assert rc.tenant_id == "tenant-x"
    assert rc.cooccur_window == 5
    assert rc.max_entities_per_doc == 12
    assert rc.generic_df_ratio == 0.5
    # Owner keys reuse cfg.owner_participants (the People-Hub owner filter).
    assert rc.owner_keys == frozenset({"owner@x.com", "owner name"})


def test_build_reconcile_config_is_shared_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRAIN_GRAPH_TENANT", raising=False)
    cfg_a = Config.load()
    cfg_b = Config.load()
    assert cfg_a == cfg_b
    # Equal configs -> the cache returns the SAME ReconcileConfig instance, so a
    # build and a later delete cannot diverge on generic_df_ratio / tenant.
    assert build_reconcile_config(cfg_a) is build_reconcile_config(cfg_b)
    # And the syncers a CLI command would build share that one config object.
    assert make_graph_syncer(cfg_a).config is make_graph_syncer(cfg_b).config


def test_make_graph_syncer_reflects_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    assert make_graph_syncer(Config.load()).enabled is True
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")
    assert make_graph_syncer(Config.load()).enabled is False


# --------------------------------------------------------------------------- #
# 3. GraphSyncer gating + never-raise discipline (unit)
# --------------------------------------------------------------------------- #
def test_disabled_syncer_is_noop(test_db: psycopg.Connection[Any]) -> None:
    syncer = GraphSyncer(ReconcileConfig(), enabled=False, backend=AgeBackend())
    with patch("brain.graph_rag.sync.reconcile_document") as recon, patch(
        "brain.graph_rag.sync.remove_document"
    ) as rem:
        syncer.reconcile(test_db, "00000000-0000-0000-0000-000000000000")
        syncer.remove(test_db, "00000000-0000-0000-0000-000000000000")
    recon.assert_not_called()
    rem.assert_not_called()


def test_age_absent_is_noop(test_db: psycopg.Connection[Any]) -> None:
    syncer = _enabled_syncer()
    # Simulate a stock pgvector DB: AGE not installable -> graceful skip.
    with patch(
        "brain.graph_rag.sync.age_extension_available", return_value=False
    ), patch("brain.graph_rag.sync.reconcile_document") as recon, patch(
        "brain.graph_rag.sync.remove_document"
    ) as rem:
        syncer.reconcile(test_db, "00000000-0000-0000-0000-000000000000")
        syncer.remove(test_db, "00000000-0000-0000-0000-000000000000")
    recon.assert_not_called()
    rem.assert_not_called()


def test_reconcile_failure_is_caught_and_logged(
    test_db: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    syncer = _enabled_syncer()
    with patch(
        "brain.graph_rag.sync.reconcile_document",
        side_effect=GraphBackendError("boom"),
    ), caplog.at_level("WARNING", logger="brain.graph_rag.sync"):
        # Must NOT raise.
        syncer.reconcile(test_db, "11111111-1111-1111-1111-111111111111")
    assert any("graph sync (reconcile) skipped" in r.message for r in caplog.records)


def test_remove_failure_is_caught_and_logged(
    test_db: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    syncer = _enabled_syncer()
    with patch(
        "brain.graph_rag.sync.remove_document",
        side_effect=GraphBackendError("boom"),
    ), caplog.at_level("WARNING", logger="brain.graph_rag.sync"):
        syncer.remove(test_db, "11111111-1111-1111-1111-111111111111")
    assert any("graph sync (remove) skipped" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# 4. ingest_document / update_document live-AGE reconcile
# --------------------------------------------------------------------------- #
def test_ingest_document_reconciles_graph(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    syncer = _enabled_syncer()
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc("m1", [("alice", "alice@x.com"), ("bob", "bob@x.com")]),
        source_kind="gmail",
        source_external_id="m1",
        graph_syncer=syncer,
    )
    assert result.created is True
    assert _person_keys(test_db) == {"alice", "bob"}
    assert _age_entity_count(test_db) == 2
    assert _age_document_count(test_db) == 1
    assert _age_cooccur_count(test_db) == 1


def test_ingest_without_syncer_writes_no_graph(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    # Bootstrap so the MATCH below has labels to scan even on an empty graph.
    AgeBackend().bootstrap(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc("m1", [("alice", "alice@x.com"), ("bob", "bob@x.com")]),
        source_kind="gmail",
        source_external_id="m1",
        graph_syncer=None,
    )
    assert result.created is True
    assert _person_keys(test_db) == set()
    assert _age_entity_count(test_db) == 0


def test_ingest_failure_does_not_block_ingest(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    syncer = _enabled_syncer()
    with patch(
        "brain.graph_rag.sync.reconcile_document",
        side_effect=GraphBackendError("boom"),
    ), caplog.at_level("WARNING", logger="brain.graph_rag.sync"):
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=_gmail_doc("m1", [("alice", "alice@x.com"), ("bob", "bob@x.com")]),
            source_kind="gmail",
            source_external_id="m1",
            graph_syncer=syncer,
        )
    # Ingest committed despite the graph-sync failure.
    assert result.created is True
    row = test_db.execute(
        "SELECT 1 FROM documents WHERE id = %s", (result.document_id,)
    ).fetchone()
    assert row is not None
    assert any("graph sync (reconcile) skipped" in r.message for r in caplog.records)


def test_update_document_reindexes_participants(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    _seed_directory(
        test_db,
        [("alice", "alice@x.com"), ("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )
    syncer = _enabled_syncer()
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc("m1", [("alice", "alice@x.com"), ("bob", "bob@x.com")]),
        source_kind="gmail",
        source_external_id="m1",
        graph_syncer=syncer,
    )
    assert _person_keys(test_db) == {"alice", "bob"}

    # Swap bob -> carol via a metadata edit; reconcile must re-index.
    update_document(
        test_db,
        document_id=result.document_id,
        metadata_patch={
            "from": "alice <alice@x.com>",
            "to": "carol <carol@x.com>",
        },
        replace_metadata=True,
        graph_syncer=syncer,
    )
    assert _person_keys(test_db) == {"alice", "carol"}
    assert _age_entity_count(test_db) == 2


def test_syncer_remove_drops_doc_from_graph(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    syncer = _enabled_syncer()
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc("m1", [("alice", "alice@x.com"), ("bob", "bob@x.com")]),
        source_kind="gmail",
        source_external_id="m1",
        graph_syncer=syncer,
    )
    assert _age_entity_count(test_db) == 2

    test_db.execute("DELETE FROM documents WHERE id = %s", (result.document_id,))
    syncer.remove(test_db, result.document_id)
    assert _age_document_count(test_db) == 0
    assert _age_entity_count(test_db) == 0
    assert _person_keys(test_db) == set()


# --------------------------------------------------------------------------- #
# 5. vault sync / watch wiring (recording double — person-aspect-inert paths)
# --------------------------------------------------------------------------- #
def test_sync_vault_reconciles_and_prunes(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    from brain.vault.sync import sync_vault

    note = tmp_path / "note.md"
    note.write_text(
        dump_frontmatter({"title": "Hello"}, "body text here\n"), encoding="utf-8"
    )
    recording = _RecordingSyncer()
    report = sync_vault(
        test_db,
        embedder=fake_embedder,
        vault_path=tmp_path,
        graph_syncer=recording,  # type: ignore[arg-type]
    )
    assert report.created == 1
    # The created vault doc id was reconciled.
    doc_row = test_db.execute(
        "SELECT id::text FROM documents WHERE kind = 'vault'"
    ).fetchone()
    assert doc_row is not None
    doc_id = str(doc_row[0])
    assert recording.reconciled == [doc_id]

    # Delete the file + prune -> the doc id is removed from the graph.
    note.unlink()
    recording2 = _RecordingSyncer()
    report2 = sync_vault(
        test_db,
        embedder=fake_embedder,
        vault_path=tmp_path,
        prune=True,
        graph_syncer=recording2,  # type: ignore[arg-type]
    )
    assert report2.deleted == 1
    assert recording2.removed == [doc_id]


def test_sync_one_file_reconciles(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    from brain.vault.sync import sync_one_file

    note = tmp_path / "solo.md"
    note.write_text(
        dump_frontmatter({"title": "Solo"}, "body\n"), encoding="utf-8"
    )
    recording = _RecordingSyncer()
    sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=tmp_path,
        file_path=note,
        graph_syncer=recording,  # type: ignore[arg-type]
    )
    doc_row = test_db.execute(
        "SELECT id::text FROM documents WHERE kind = 'vault'"
    ).fetchone()
    assert doc_row is not None
    assert recording.reconciled == [str(doc_row[0])]


def test_handle_delete_removes_from_graph(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    from brain.vault.watch import _handle_delete

    # Seed a vault-tier row whose vault_path matches the file we "delete".
    doc_row = test_db.execute(
        """
        INSERT INTO documents (title, content, content_hash, content_type, kind,
                               vault_path)
        VALUES ('n', 'b', 'h-watch-1', 'note', 'vault', 'note.md')
        RETURNING id::text
        """,
    ).fetchone()
    assert doc_row is not None
    doc_id = str(doc_row[0])
    recording = _RecordingSyncer()
    _handle_delete(
        test_db,
        tmp_path / "note.md",
        tmp_path,
        graph_syncer=recording,  # type: ignore[arg-type]
    )
    assert recording.removed == [doc_id]
    # Row is gone (the DELETE the watcher issues).
    assert (
        test_db.execute("SELECT 1 FROM documents WHERE id = %s", (doc_id,)).fetchone()
        is None
    )


# --------------------------------------------------------------------------- #
# 6. CLI end-to-end wiring (brain rm -> remove)
# --------------------------------------------------------------------------- #
def test_cli_rm_removes_doc_from_graph(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    syncer = _enabled_syncer()
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc("m1", [("alice", "alice@x.com"), ("bob", "bob@x.com")]),
        source_kind="gmail",
        source_external_id="m1",
        graph_syncer=syncer,
    )
    assert _age_entity_count(test_db) == 2
    doc_id = result.document_id
    assert doc_id is not None

    # Drive `brain rm` with graph sync enabled + a real AGE backend.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    runner = CliRunner()
    res = runner.invoke(app, ["rm", doc_id[:8], "--yes"])
    assert res.exit_code == 0, res.output

    assert _age_document_count(test_db) == 0
    assert _age_entity_count(test_db) == 0


# --------------------------------------------------------------------------- #
# 7. Force content-hash-fallback removes the OLD doc from the graph (#2)
# --------------------------------------------------------------------------- #
def test_force_fallback_removes_old_doc_from_graph(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    syncer = _enabled_syncer()
    # source_external_id=None forces the content-hash-fallback path (not the
    # sourced upsert); a non-empty source_metadata still creates the gmail
    # ``sources`` row the person resolver JOINs on.
    doc = _gmail_doc("m1", [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    first = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id=None,
        source_metadata={"thread_id": "m1"},
        graph_syncer=syncer,
    )
    assert first.created is True
    assert first.replaced_document_id is None
    assert _age_document_count(test_db) == 1
    assert _age_entity_count(test_db) == 2

    # Force re-ingest of identical content -> DELETE old row + INSERT new uuid.
    second = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="gmail",
        source_external_id=None,
        source_metadata={"thread_id": "m1"},
        force=True,
        graph_syncer=syncer,
    )
    assert second.created is True
    assert second.document_id != first.document_id
    assert second.replaced_document_id == first.document_id
    # Exactly ONE Document vertex survives (the new uuid) — the old uuid's AGE
    # vertex was DETACH DELETEd by the post-commit remove(replaced_document_id).
    assert _age_document_count(test_db) == 1
    assert _age_entity_count(test_db) == 2


# --------------------------------------------------------------------------- #
# 8. MCP wiring (brain_ingest_stdin triggers reconcile)
# --------------------------------------------------------------------------- #
def test_mcp_ingest_stdin_reconciles_graph(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,
        graph_syncer=_enabled_syncer(),
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    result = mcp_server.brain_ingest_stdin(
        content="Body of the MCP-ingested thread with words.",
        source="gmail",
        external_id="m-mcp-1",
        title="mcp thread",
        content_type="email",
        metadata={"from": "alice <alice@x.com>", "to": "bob <bob@x.com>"},
    )
    assert result["document_id"] is not None
    assert _person_keys(test_db) == {"alice", "bob"}
    assert _age_entity_count(test_db) == 2
    assert _age_document_count(test_db) == 1


# --------------------------------------------------------------------------- #
# 9. CLI ingest-family via the real factory (not the fake double)
# --------------------------------------------------------------------------- #
def test_cli_ingest_stdin_reconciles_via_factory(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: None)
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "ingest-stdin",
            "--source",
            "gmail",
            "--external-id",
            "m-cli-1",
            "--title",
            "cli thread",
            "--content-type",
            "email",
            "--metadata",
            json.dumps({"from": "alice <alice@x.com>", "to": "bob <bob@x.com>"}),
            "--no-enrich",
        ],
        input="Body of the CLI-ingested thread with words.\n",
    )
    assert res.exit_code == 0, res.output
    # The CLI built the syncer via the real _build_graph_syncer factory.
    assert _person_keys(test_db) == {"alice", "bob"}
    assert _age_entity_count(test_db) == 2
    assert _age_document_count(test_db) == 1
    assert _age_cooccur_count(test_db) == 1


# --------------------------------------------------------------------------- #
# 10. relink-derived reconciles the linkable corpus (#1)
# --------------------------------------------------------------------------- #
def test_relink_derived_reconciles_corpus(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    # Ingest a gmail doc WITHOUT graph sync, so the graph starts empty even
    # though the doc has resolvable participants.
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc("m1", [("alice", "alice@x.com"), ("bob", "bob@x.com")]),
        source_kind="gmail",
        source_external_id="m1",
        graph_syncer=None,
    )
    AgeBackend().bootstrap(test_db)
    assert _age_entity_count(test_db) == 0

    # relink-derived must reconcile the linkable corpus -> graph reflects it.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    runner = CliRunner()
    res = runner.invoke(app, ["vault", "relink-derived"])
    assert res.exit_code == 0, res.output

    assert _person_keys(test_db) == {"alice", "bob"}
    assert _age_entity_count(test_db) == 2
    assert _age_document_count(test_db) == 1
