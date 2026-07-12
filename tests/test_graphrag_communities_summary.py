"""Tests for the GraphRAG eager community summary + embedding pass (wave G3-c).

Covers :func:`brain.graph_rag.communities_summary.summarize_communities` and
migration 014's ``graph_communities.summary_members_hash`` staleness column:

* migration 014 applies fresh + idempotent; the column exists.
* a FAKE enricher + FAKE embedder write summaries + embeddings, set
  ``summary_members_hash``, and re-run as a no-op (idempotency).
* a membership change (members_hash moves via the G3-b delta-gate) marks a
  community stale → re-summarized on the next run.
* never-raise: enricher returning ``None`` / raising leaves the summary NULL and
  ``summary_members_hash`` unset (retried next run); a raising embedder leaves
  ``summary_embedding`` NULL while the summary text is still written.
* enricher ``None`` → the whole pass is a logged no-op (``skipped=True``);
  embedder ``None`` with the enricher present → summaries ARE written and only the
  embedding phase is skipped (``summary_embedding`` NULL, ``skipped=False``).
* ``limit`` cap + tenant isolation.

All entities/docs are synthetic (P-/Q-/T- keys); no PII; no live Ollama.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.errors import GraphTenantError
from brain.graph_rag.communities import build_communities
from brain.graph_rag.communities_summary import (
    CommunitySummaryResult,
    summarize_communities,
)
from tests.conftest import FakeEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# ``summarize_communities`` reconciles ``graph_communities.summary_embedding`` to
# the (4096-dim FakeEmbedder) dim — a schema mutation. Route the whole module to
# the full drop+migrate reset.
pytestmark = pytest.mark.fresh_schema


# --------------------------------------------------------------------------- #
# Test doubles (DI seam — no monkeypatching, no live Ollama)
# --------------------------------------------------------------------------- #
class RecordingSummarizer:
    """Fake enricher returning a canned summary + recording every call."""

    def __init__(
        self, summary: str = "Synthetic community summary.", model: str = "fake-model:1b"
    ) -> None:
        self._summary = summary
        self._model = model
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return self._model

    def summarize_group(
        self,
        *,
        person: str | None,
        entity_names: list[str],
        doc_titles: list[str],
    ) -> str | None:
        self.calls.append(
            {"person": person, "entity_names": entity_names, "doc_titles": doc_titles}
        )
        return self._summary


class NoneSummarizer:
    """Fake enricher that always returns ``None`` (Ollama-down simulation)."""

    model = "fake-model:1b"

    def summarize_group(
        self, *, person: str | None, entity_names: list[str], doc_titles: list[str]
    ) -> str | None:
        return None


class RaisingSummarizer:
    """Fake enricher whose ``summarize_group`` raises (defence-in-depth case)."""

    model = "fake-model:1b"

    def summarize_group(
        self, *, person: str | None, entity_names: list[str], doc_titles: list[str]
    ) -> str | None:
        raise RuntimeError("synthetic enricher failure")


class RaisingEmbedder:
    """Fake embedder whose ``embed`` raises; ``dim`` matches the fake corpus."""

    dim = 1024

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        raise RuntimeError("synthetic embedder failure")

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# Seeding helpers (mirror tests/test_graphrag_communities.py)
# --------------------------------------------------------------------------- #
def _cfg(**overrides: Any) -> Config:
    params: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "graph_tenant_id": "default",
    }
    params.update(overrides)
    return Config(**params)


def _insert_entity(
    conn: psycopg.Connection[Any], tenant: str, name: str, canonical_key: str
) -> str:
    row = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, 'person', %s, %s) RETURNING id::text",
        (tenant, name, canonical_key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_rel(
    conn: psycopg.Connection[Any], tenant: str, a: str, b: str, weight: float
) -> None:
    src, dst = sorted((a, b))
    conn.execute(
        "INSERT INTO graph_relationships "
        "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
        "VALUES (%s, %s, %s, 'co_occurs', %s, 1, 1)",
        (tenant, src, dst, weight),
    )


def _seed_two_clusters(
    conn: psycopg.Connection[Any], tenant: str = "default"
) -> tuple[list[str], list[str]]:
    """Two dense triangles + a weak bridge, scoped to ``tenant``."""
    c1 = [
        _insert_entity(conn, tenant, f"P-{tenant}-{i}", f"p-{tenant}-{i}")
        for i in range(3)
    ]
    c2 = [
        _insert_entity(conn, tenant, f"Q-{tenant}-{i}", f"q-{tenant}-{i}")
        for i in range(3)
    ]
    for a, b in [(0, 1), (0, 2), (1, 2)]:
        _insert_rel(conn, tenant, c1[a], c1[b], 0.8)
        _insert_rel(conn, tenant, c2[a], c2[b], 0.8)
    _insert_rel(conn, tenant, c1[2], c2[0], 0.05)  # weak bridge
    return c1, c2


def _seed_disjoint_triangles(
    conn: psycopg.Connection[Any], tenant: str, n: int
) -> list[list[str]]:
    """``n`` fully-disconnected triangles → ``n`` communities of size 3."""
    triangles: list[list[str]] = []
    for t in range(n):
        members = [
            _insert_entity(conn, tenant, f"T{t}-{i}", f"t{t}-{tenant}-{i}")
            for i in range(3)
        ]
        for a, b in [(0, 1), (0, 2), (1, 2)]:
            _insert_rel(conn, tenant, members[a], members[b], 0.8)
        triangles.append(members)
    return triangles


def _insert_document(conn: psycopg.Connection[Any], title: str) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, 'note') RETURNING id::text",
        (title, "synthetic body", uuid.uuid4().hex),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_mention(
    conn: psycopg.Connection[Any], tenant: str, entity_id: str, document_id: str
) -> None:
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, source) VALUES (%s, %s, %s, 'people')",
        (tenant, entity_id, document_id),
    )


def _community_keys(conn: psycopg.Connection[Any], tenant: str) -> list[str]:
    rows = conn.execute(
        "SELECT community_key::text FROM graph_communities WHERE tenant_id = %s "
        "ORDER BY community_key",
        (tenant,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _community_for_entity(
    conn: psycopg.Connection[Any], tenant: str, entity_id: str
) -> str:
    row = conn.execute(
        "SELECT community_key::text FROM graph_community_members "
        "WHERE tenant_id = %s AND entity_id = %s",
        (tenant, entity_id),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _read_summary(
    conn: psycopg.Connection[Any], tenant: str, key: str
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT summary, summary_model, summary_members_hash, members_hash, "
        "summary_at, summary_embedding IS NOT NULL, vector_dims(summary_embedding) "
        "FROM graph_communities WHERE tenant_id = %s AND community_key = %s",
        (tenant, key),
    ).fetchone()
    assert row is not None
    return {
        "summary": row[0],
        "summary_model": row[1],
        "summary_members_hash": row[2],
        "members_hash": row[3],
        "summary_at": row[4],
        "embedding_present": bool(row[5]),
        "embedding_dims": row[6],
    }


# --------------------------------------------------------------------------- #
# Migration 014
# --------------------------------------------------------------------------- #
def test_migration_014_adds_summary_members_hash_column(
    test_db: psycopg.Connection[Any],
) -> None:
    row = test_db.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'graph_communities' "
        "AND column_name = 'summary_members_hash'"
    ).fetchone()
    assert row is not None  # migration 014 applied by the fresh-schema fixture


def test_migration_014_is_idempotent(test_db: psycopg.Connection[Any]) -> None:
    # Re-running the additive ALTER (the migration body) must be a safe no-op.
    test_db.execute(
        "ALTER TABLE graph_communities "
        "ADD COLUMN IF NOT EXISTS summary_members_hash TEXT NULL"
    )
    count_row = test_db.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'graph_communities' "
        "AND column_name = 'summary_members_hash'"
    ).fetchone()
    assert count_row is not None and int(count_row[0]) == 1


# --------------------------------------------------------------------------- #
# Happy path — summaries + embeddings written
# --------------------------------------------------------------------------- #
def test_summarize_writes_summaries_and_embeddings(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")

    enricher = RecordingSummarizer(summary="Cluster synthesis.")
    embedder = FakeEmbedder()  # dim 4096 — exercises the dim-reconcile path
    result = summarize_communities(
        test_db, _cfg(), tenant="default", enricher=enricher, embedder=embedder
    )

    assert isinstance(result, CommunitySummaryResult)
    assert result.skipped is False
    assert result.candidates == 2
    assert result.summarized == 2
    assert result.summary_failures == 0
    assert result.embedded == 2
    assert result.embed_failures == 0

    for key in _community_keys(test_db, "default"):
        snap = _read_summary(test_db, "default", key)
        assert snap["summary"] == "Cluster synthesis."
        assert snap["summary_model"] == "fake-model:1b"
        # summary_members_hash is set to the row's members_hash (fresh summary).
        assert snap["summary_members_hash"] == snap["members_hash"]
        assert snap["summary_at"] is not None
        assert snap["embedding_present"] is True
        assert snap["embedding_dims"] == embedder.dim


def test_summarize_passes_person_none_entities_and_doc_titles(
    test_db: psycopg.Connection[Any],
) -> None:
    c1, _c2 = _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")

    # Seed a document mentioning cluster-one's entities so the doc-title SQL
    # has something to return for that community.
    doc_id = _insert_document(test_db, "Synthetic Cluster One Doc")
    for entity_id in c1:
        _insert_mention(test_db, "default", entity_id, doc_id)

    enricher = RecordingSummarizer()
    summarize_communities(
        test_db, _cfg(), tenant="default", enricher=enricher, embedder=FakeEmbedder()
    )

    assert len(enricher.calls) == 2
    assert all(call["person"] is None for call in enricher.calls)
    assert all(call["entity_names"] for call in enricher.calls)
    # The cluster-one call (its entity_names are the P-* display names) carries
    # the seeded doc title.
    c1_names = {f"P-default-{i}" for i in range(3)}
    c1_calls = [c for c in enricher.calls if set(c["entity_names"]) == c1_names]
    assert len(c1_calls) == 1
    assert c1_calls[0]["doc_titles"] == ["Synthetic Cluster One Doc"]


def test_summarize_is_idempotent_on_unchanged_communities(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")
    embedder = FakeEmbedder()

    first = summarize_communities(
        test_db, _cfg(), tenant="default", enricher=RecordingSummarizer(),
        embedder=embedder,
    )
    assert first.summarized == 2 and first.embedded == 2

    # Second run with no membership change: every community already has a
    # current summary + embedding → nothing to do.
    second = summarize_communities(
        test_db, _cfg(), tenant="default", enricher=RecordingSummarizer(),
        embedder=embedder,
    )
    assert second.skipped is False
    assert second.candidates == 0
    assert second.summarized == 0
    assert second.embedded == 0
    assert second.embed_failures == 0


# --------------------------------------------------------------------------- #
# Staleness — membership change re-summarizes
# --------------------------------------------------------------------------- #
def test_membership_change_marks_stale_and_resummarizes(
    test_db: psycopg.Connection[Any],
) -> None:
    c1, _c2 = _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")
    summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RecordingSummarizer(summary="Original summary."),
        embedder=FakeEmbedder(),
    )
    key = _community_for_entity(test_db, "default", c1[0])
    before = _read_summary(test_db, "default", key)
    assert before["summary"] == "Original summary."

    # Grow cluster one with a strongly-connected new member: members_hash moves
    # but Jaccard 3/4 = 0.75 >= 0.5 → the G3-b delta-gate REUSES the key and
    # preserves the old summary while leaving summary_members_hash stale.
    e_new = _insert_entity(test_db, "default", "P-default-new", "p-default-new")
    for existing in c1:
        _insert_rel(test_db, "default", existing, e_new, 0.8)
    rebuild = build_communities(test_db, _cfg(), tenant="default")
    assert rebuild.reused >= 1

    stale = _read_summary(test_db, "default", key)
    assert stale["summary"] == "Original summary."  # delta-gate preserved it
    assert stale["summary_members_hash"] != stale["members_hash"]  # now stale

    # The next summary pass detects the staleness and re-summarizes ONLY the
    # changed community.
    result = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RecordingSummarizer(summary="Refreshed summary."),
        embedder=FakeEmbedder(),
    )
    assert result.candidates == 1
    assert result.summarized == 1
    assert result.embedded == 1

    after = _read_summary(test_db, "default", key)
    assert after["summary"] == "Refreshed summary."
    assert after["summary_members_hash"] == after["members_hash"]  # back in sync
    assert after["embedding_present"] is True


# --------------------------------------------------------------------------- #
# Never-raise — enricher failures
# --------------------------------------------------------------------------- #
def test_enricher_returning_none_leaves_null_and_retries(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")

    result = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=NoneSummarizer(), embedder=FakeEmbedder(),
    )
    assert result.skipped is False
    assert result.candidates == 2
    assert result.summarized == 0
    assert result.summary_failures == 2
    assert result.embedded == 0

    for key in _community_keys(test_db, "default"):
        snap = _read_summary(test_db, "default", key)
        assert snap["summary"] is None
        assert snap["summary_members_hash"] is None  # NOT set → still a candidate
        assert snap["embedding_present"] is False

    # Retry with a working enricher → the communities are still candidates and
    # get summarized this time.
    retry = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RecordingSummarizer(summary="Now it works."),
        embedder=FakeEmbedder(),
    )
    assert retry.candidates == 2
    assert retry.summarized == 2
    assert retry.embedded == 2


def test_enricher_raising_is_caught_and_build_succeeds(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")

    # A raising enricher must NOT propagate — defence in depth over the
    # already-never-raising summarize_group.
    result = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RaisingSummarizer(), embedder=FakeEmbedder(),
    )
    assert result.summarized == 0
    assert result.summary_failures == 2
    for key in _community_keys(test_db, "default"):
        snap = _read_summary(test_db, "default", key)
        assert snap["summary"] is None
        assert snap["summary_members_hash"] is None


# --------------------------------------------------------------------------- #
# Never-raise — embedder failure
# --------------------------------------------------------------------------- #
def test_embedder_raising_leaves_embedding_null_summary_written(
    test_db: psycopg.Connection[Any],
) -> None:
    _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")

    result = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RecordingSummarizer(summary="Summary text."),
        embedder=RaisingEmbedder(),
    )
    # Summaries are written; the embedding pass fails best-effort.
    assert result.summarized == 2
    assert result.embedded == 0
    assert result.embed_failures == 2

    for key in _community_keys(test_db, "default"):
        snap = _read_summary(test_db, "default", key)
        assert snap["summary"] == "Summary text."  # summary survived
        assert snap["summary_members_hash"] == snap["members_hash"]
        assert snap["embedding_present"] is False  # embedding left NULL

    # A later pass with a working embedder backfills the embeddings without
    # rewriting the (still-current) summaries.
    retry = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RecordingSummarizer(summary="Summary text."),
        embedder=FakeEmbedder(),
    )
    assert retry.candidates == 0  # summaries already current
    assert retry.summarized == 0
    assert retry.embedded == 2
    for key in _community_keys(test_db, "default"):
        assert _read_summary(test_db, "default", key)["embedding_present"] is True


# --------------------------------------------------------------------------- #
# None enricher → skip whole pass (embedder-None is decoupled — see below)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("enricher", "embedder"),
    [
        (None, FakeEmbedder()),
        (None, None),
    ],
)
def test_skips_when_enricher_missing(
    test_db: psycopg.Connection[Any], enricher: Any, embedder: Any
) -> None:
    """A None enricher skips the whole pass — summaries can't be produced.

    Only the enricher gates the whole pass. A None embedder (with the enricher
    present) does NOT skip — see
    ``test_embedder_none_writes_summaries_without_embeddings``.
    """
    _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")

    result = summarize_communities(
        test_db, _cfg(), tenant="default", enricher=enricher, embedder=embedder
    )
    assert result.skipped is True
    assert result.summarized == 0
    assert result.embedded == 0
    for key in _community_keys(test_db, "default"):
        snap = _read_summary(test_db, "default", key)
        assert snap["summary"] is None
        assert snap["embedding_present"] is False


# --------------------------------------------------------------------------- #
# Decoupled phases (FIX 2) — embedder None still writes summaries
# --------------------------------------------------------------------------- #
def test_embedder_none_writes_summaries_without_embeddings(
    test_db: psycopg.Connection[Any],
) -> None:
    """FIX 2 (§17c Q10): a present enricher + None embedder still writes summaries.

    The summary and embedding phases are decoupled — a missing/unavailable
    embedder must NOT block summaries. Summaries (text/model/at +
    summary_members_hash) are written; ``summary_embedding`` stays NULL (the
    global path degrades that community to FTS-only); the pass is NOT marked
    skipped; nothing raises.
    """
    _seed_two_clusters(test_db)
    build_communities(test_db, _cfg(), tenant="default")

    result = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RecordingSummarizer(summary="Summary without embedding."),
        embedder=None,
    )
    assert result.skipped is False
    assert result.candidates == 2
    assert result.summarized == 2
    assert result.summary_failures == 0
    assert result.embedded == 0
    assert result.embed_failures == 0

    for key in _community_keys(test_db, "default"):
        snap = _read_summary(test_db, "default", key)
        assert snap["summary"] == "Summary without embedding."
        assert snap["summary_model"] == "fake-model:1b"
        assert snap["summary_members_hash"] == snap["members_hash"]
        assert snap["summary_at"] is not None
        assert snap["embedding_present"] is False  # embedding phase skipped

    # A later pass with a working embedder backfills the embeddings without
    # rewriting the (still-current) summaries.
    retry = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RecordingSummarizer(summary="Summary without embedding."),
        embedder=FakeEmbedder(),
    )
    assert retry.candidates == 0  # summaries already current
    assert retry.summarized == 0
    assert retry.embedded == 2
    for key in _community_keys(test_db, "default"):
        assert _read_summary(test_db, "default", key)["embedding_present"] is True


# --------------------------------------------------------------------------- #
# Limit cap
# --------------------------------------------------------------------------- #
def test_limit_caps_summaries_per_run(test_db: psycopg.Connection[Any]) -> None:
    _seed_disjoint_triangles(test_db, "default", 3)
    build = build_communities(test_db, _cfg(), tenant="default")
    assert build.communities_total == 3

    first = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RecordingSummarizer(), embedder=FakeEmbedder(), limit=2,
    )
    assert first.candidates == 2
    assert first.summarized == 2
    assert first.embedded == 2
    summarized_now = sum(
        1
        for key in _community_keys(test_db, "default")
        if _read_summary(test_db, "default", key)["summary"] is not None
    )
    assert summarized_now == 2  # one community still unsummarized

    # A second uncapped run picks up the remaining community.
    second = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RecordingSummarizer(), embedder=FakeEmbedder(),
    )
    assert second.candidates == 1
    assert second.summarized == 1
    assert second.embedded == 1


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #
def test_summarize_is_tenant_isolated(test_db: psycopg.Connection[Any]) -> None:
    _seed_two_clusters(test_db, tenant="default")
    _seed_two_clusters(test_db, tenant="other")
    build_communities(test_db, _cfg(graph_tenant_id="default"), tenant="default")
    build_communities(test_db, _cfg(graph_tenant_id="other"), tenant="other")

    result = summarize_communities(
        test_db, _cfg(), tenant="default",
        enricher=RecordingSummarizer(), embedder=FakeEmbedder(),
    )
    assert result.summarized == 2

    # The other tenant's communities are untouched (NULL summary + embedding).
    for key in _community_keys(test_db, "other"):
        snap = _read_summary(test_db, "other", key)
        assert snap["summary"] is None
        assert snap["summary_members_hash"] is None
        assert snap["embedding_present"] is False


# --------------------------------------------------------------------------- #
# Caller-bug guard
# --------------------------------------------------------------------------- #
def test_rejects_empty_tenant(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(GraphTenantError):
        summarize_communities(
            test_db, _cfg(), tenant="",
            enricher=RecordingSummarizer(), embedder=FakeEmbedder(),
        )
