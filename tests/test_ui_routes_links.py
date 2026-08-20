"""`GET /api/notes/{id}/links` — backlinks, outgoing links, derived edges.

Documents are seeded with direct SQL (the pattern
``tests/test_graph_queries.py`` established) rather than through the sync
engine: this route is a projection of :mod:`brain.vault.graph`, and the
assertions here are about *direction* — which side of an edge a document sits
on — which a hand-seeded row states unambiguously.

The route table (``brain.ui.app``) is owned by the phase-2 integrator, so this
module mounts the routes onto the real app itself rather than editing
``create_app``. Everything else — middleware, error envelope, ``no-store`` — is
the production stack.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from brain.config import Config
from brain.ui import routes_links
from brain.ui.app import create_app
from brain.ui.context import UiContext

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"

#: Synthetic ids. No PII in this corpus, and the fixed UUIDs make a failure
#: message readable ("aaaaaaaa…" is A).
DOC_A = "aaaaaaaa-0000-4000-8000-00000000000a"
DOC_B = "bbbbbbbb-0000-4000-8000-00000000000b"
DOC_C = "cccccccc-0000-4000-8000-00000000000c"


def _make_doc(
    conn: psycopg.Connection[Any], *, doc_id: str, title: str, kind: str = "vault"
) -> str:
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, vault_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            doc_id,
            title,
            f"body of {title}",
            f"hash-{doc_id}",
            "note",
            kind,
            f"notes/{title.lower().replace(' ', '-')}.md",
        ),
    )
    return doc_id


def _link(conn: psycopg.Connection[Any], *, src: str, dst: str, text: str) -> None:
    conn.execute(
        """
        INSERT INTO links
          (src_document_id, dst_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (src, dst, text, "wiki", None),
    )


def _derived(conn: psycopg.Connection[Any], *, a: str, b: str) -> None:
    """One ``derived_links`` row in canonical ``(LEAST, GREATEST)`` order."""
    src, dst = (a, b) if a < b else (b, a)
    conn.execute(
        """
        INSERT INTO derived_links
          (src_document_id, dst_document_id, rule, evidence, weight)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        """,
        (src, dst, "shared_thread", json.dumps({"thread": "t-1"}), 0.5),
    )


@pytest.fixture
def linked_corpus(test_db: psycopg.Connection) -> dict[str, str]:
    """A → B by wiki link; A ~ C by a derived edge. C links to nothing."""
    a = _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    b = _make_doc(test_db, doc_id=DOC_B, title="Vendor Evaluation")
    c = _make_doc(test_db, doc_id=DOC_C, title="Budget Review")
    _link(test_db, src=a, dst=b, text="[[Vendor Evaluation]]")
    _derived(test_db, a=a, b=c)
    return {"A": a, "B": b, "C": c}


@pytest.fixture
def client(test_db: psycopg.Connection, tmp_path: Path, fake_embedder: Any) -> TestClient:
    vault = tmp_path / "vault"
    vault.mkdir()

    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    context = UiContext(
        cfg=Config(
            database_url="postgresql://unused/in/these/tests",
            vault_path=vault,
            embedder="none",
        ),
        conn_factory=conn_factory,
        embedder=fake_embedder,
        search_fn=lambda *a, **k: [],
        allowed_origin=ORIGIN,
        logging_enabled=False,
    )
    app = create_app(context)
    # app.py is the integrator's file for the whole phase; the registration diff
    # lands there once. Appending here exercises the same router the integrator
    # will write into.
    app.routes.append(
        Route("/api/notes/{id_prefix}/links", routes_links.note_links, methods=["GET"])
    )
    return TestClient(app, base_url=ORIGIN)


def _links_of(client: TestClient, doc_id: str) -> dict[str, Any]:
    response = client.get(f"/api/notes/{doc_id}/links")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def test_backlinks_name_the_document_that_links_in(
    client: TestClient, linked_corpus: dict[str, str]
) -> None:
    payload = _links_of(client, linked_corpus["B"])

    wiki = [row for row in payload["backlinks"] if row["link_kind"] == "wiki"]
    assert [(row["id"], row["title"]) for row in wiki] == [
        (linked_corpus["A"], "Planning Sync")
    ]
    assert wiki[0]["link_text"] == "[[Vendor Evaluation]]"
    assert wiki[0]["kind"] == "vault"


def test_a_document_is_never_its_own_backlink(
    client: TestClient, linked_corpus: dict[str, str]
) -> None:
    """The direction assertion. Swapping ``src``/``dst`` fails exactly here.

    A links to B, so A has no *wiki* backlink at all — and above all not
    itself. Asserting "200 with a backlinks key" would survive the swap; this
    names the ids.
    """
    payload = _links_of(client, linked_corpus["A"])

    wiki_ids = [row["id"] for row in payload["backlinks"] if row["link_kind"] == "wiki"]
    assert linked_corpus["A"] not in wiki_ids
    assert wiki_ids == []


def test_outgoing_names_the_target_and_not_the_source(
    client: TestClient, linked_corpus: dict[str, str]
) -> None:
    outgoing = [
        row
        for row in _links_of(client, linked_corpus["A"])["outgoing"]
        if row["link_kind"] == "wiki"
    ]
    assert [(row["id"], row["title"]) for row in outgoing] == [
        (linked_corpus["B"], "Vendor Evaluation")
    ]
    assert outgoing[0]["resolved"] is True

    # ...and B, the target, has no outgoing wiki link of its own.
    b_outgoing = [
        row
        for row in _links_of(client, linked_corpus["B"])["outgoing"]
        if row["link_kind"] == "wiki"
    ]
    assert b_outgoing == []


def test_derived_edges_appear_under_link_kind_derived(
    client: TestClient, linked_corpus: dict[str, str]
) -> None:
    payload = _links_of(client, linked_corpus["A"])

    derived = [row for row in payload["backlinks"] if row["link_kind"] == "derived"]
    assert [row["id"] for row in derived] == [linked_corpus["C"]]
    assert derived[0]["rule"] == "shared_thread"
    assert derived[0]["weight"] == pytest.approx(0.5)

    # Derived storage is undirected, so C sees A from the other side.
    partner = [
        row
        for row in _links_of(client, linked_corpus["C"])["backlinks"]
        if row["link_kind"] == "derived"
    ]
    assert [row["id"] for row in partner] == [linked_corpus["A"]]


def test_counts_match_the_rows(
    client: TestClient, linked_corpus: dict[str, str]
) -> None:
    payload = _links_of(client, linked_corpus["A"])
    assert payload["counts"]["backlinks"] == len(payload["backlinks"])
    assert payload["counts"]["outgoing"] == len(payload["outgoing"])
    assert payload["id"] == linked_corpus["A"]


def test_the_payload_carries_no_document_bodies(
    client: TestClient, linked_corpus: dict[str, str]
) -> None:
    """Lazy means lazy: this route must never grow into a second note fetch."""
    raw = client.get(f"/api/notes/{linked_corpus['A']}/links").text
    assert "body of" not in raw
    for row in _links_of(client, linked_corpus["A"])["backlinks"]:
        assert set(row) == {"id", "title", "kind", "link_text", "link_kind", "rule", "weight"}


# ------------------------------------------------------------ fails closed --


def test_unknown_id_is_a_typed_404(client: TestClient, linked_corpus: dict[str, str]) -> None:
    response = client.get("/api/notes/deadbeefdead/links")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "note_not_found"


def test_short_prefix_is_a_typed_400(client: TestClient, linked_corpus: dict[str, str]) -> None:
    response = client.get("/api/notes/abc/links")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "id_prefix_too_short"


def test_a_database_failure_is_a_leak_free_503(
    tmp_path: Path, fake_embedder: Any
) -> None:
    """No connection is available at all — the route must 503, not 500."""

    @contextlib.contextmanager
    def broken_factory() -> Any:
        raise psycopg.OperationalError("connection refused to 10.0.0.1:5432 as brain")
        yield  # pragma: no cover — unreachable, keeps this a generator

    vault = tmp_path / "vault"
    vault.mkdir()
    context = UiContext(
        cfg=Config(
            database_url="postgresql://unused/in/these/tests",
            vault_path=vault,
            embedder="none",
        ),
        conn_factory=broken_factory,
        embedder=fake_embedder,
        search_fn=lambda *a, **k: [],
        allowed_origin=ORIGIN,
        logging_enabled=False,
    )
    app = create_app(context)
    app.routes.append(
        Route("/api/notes/{id_prefix}/links", routes_links.note_links, methods=["GET"])
    )
    response = TestClient(app, base_url=ORIGIN).get(f"/api/notes/{DOC_A}/links")

    assert response.status_code == 503
    body = response.text
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "10.0.0.1" not in body and "brain" not in body
