"""`GET /api/notes/{id}/graph` — the one-ring neighbourhood graph of one note.

Documents and edges are seeded with direct SQL, the pattern
``tests/test_ui_routes_links.py`` established: the route is a projection of
:func:`brain.vault.graph.graph_data` through :func:`brain.ui.graph_layout.layout`,
and a hand-seeded row states its expected position unambiguously.

All ids and titles are synthetic.
"""
from __future__ import annotations

import ast
import contextlib
import json
import math
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.config import Config
from brain.sensitivity import CONFIDENTIAL, DEFAULT_SENSITIVITY
from brain.ui import graph_layout, queries, routes_graph
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.ui.graph_layout import RING_RADIUS

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"

DOC_A = "aaaaaaaa-0000-4000-8000-00000000000a"
DOC_B = "bbbbbbbb-0000-4000-8000-00000000000b"
DOC_C = "cccccccc-0000-4000-8000-00000000000c"
DOC_D = "dddddddd-0000-4000-8000-00000000000d"
DOC_S = "5ea1ed00-0000-4000-8000-00000000000e"

SEALED_TITLE = "Severance Terms"


def _neighbour_id(n: int) -> str:
    return f"eeeeeeee-0000-4000-8000-{n:012d}"


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
            f"notes/{doc_id}.md",
        ),
    )
    return doc_id


def _link(conn: psycopg.Connection[Any], *, src: str, dst: str, text: str = "[[x]]") -> None:
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


def _seal(conn: psycopg.Connection[Any], doc_id: str) -> str:
    conn.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s", (CONFIDENTIAL, doc_id)
    )
    return doc_id


def _context(
    conn_factory: Any,
    tmp_path: Path,
    fake_embedder: Any,
    *,
    serve_confidential_titles: bool = False,
) -> UiContext:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return UiContext(
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
        serve_confidential_titles=serve_confidential_titles,
    )


def _client(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: Any,
    *,
    serve_confidential_titles: bool = False,
    raise_server_exceptions: bool = True,
) -> TestClient:
    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    context = _context(
        conn_factory,
        tmp_path,
        fake_embedder,
        serve_confidential_titles=serve_confidential_titles,
    )
    return TestClient(
        create_app(context),
        base_url=ORIGIN,
        raise_server_exceptions=raise_server_exceptions,
    )


@pytest.fixture
def client(test_db: psycopg.Connection, tmp_path: Path, fake_embedder: Any) -> TestClient:
    return _client(test_db, tmp_path, fake_embedder)


def _graph_of(client: TestClient, doc_id: str) -> dict[str, Any]:
    response = client.get(f"/api/notes/{doc_id}/graph")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


# ------------------------------------------------------------------ shape --


def test_a_lone_note_in_a_linked_corpus_is_one_node(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    _make_doc(test_db, doc_id=DOC_B, title="Vendor Evaluation")
    _make_doc(test_db, doc_id=DOC_C, title="Budget Review")
    _link(test_db, src=DOC_B, dst=DOC_C)

    payload = _graph_of(client, DOC_A)

    assert payload == {
        "id": DOC_A,
        "width": 320,
        "height": 320,
        "nodes": [
            {
                "id": DOC_A,
                "title": "Planning Sync",
                "kind": "vault",
                "x": 160.0,
                "y": 160.0,
                "r": 9,
                "root": True,
            }
        ],
        "edges": [],
        "truncated": 0,
        "corpus_linked": True,
    }


def test_an_isolated_note_in_an_unlinked_corpus_says_so(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    _make_doc(test_db, doc_id=DOC_B, title="Vendor Evaluation")

    payload = _graph_of(client, DOC_A)

    assert [n["id"] for n in payload["nodes"]] == [DOC_A]
    assert payload["corpus_linked"] is False


def test_a_derived_edge_alone_makes_the_corpus_linked(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    _make_doc(test_db, doc_id=DOC_B, title="Vendor Evaluation")
    _make_doc(test_db, doc_id=DOC_C, title="Budget Review")
    _derived(test_db, a=DOC_B, b=DOC_C)

    assert _graph_of(client, DOC_A)["corpus_linked"] is True


def test_three_neighbours_sit_on_the_ring_in_title_order_clockwise_from_twelve(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    # Inserted, and id-ordered, deliberately NOT in title order.
    _make_doc(test_db, doc_id=DOC_B, title="charlie")
    _make_doc(test_db, doc_id=DOC_C, title="Alpha")
    _make_doc(test_db, doc_id=DOC_D, title="bravo")
    for dst in (DOC_B, DOC_C, DOC_D):
        _link(test_db, src=DOC_A, dst=dst)

    payload = _graph_of(client, DOC_A)
    ring = payload["nodes"][1:]

    assert [n["title"] for n in ring] == ["Alpha", "bravo", "charlie"]
    for i, node in enumerate(ring):
        theta = 2 * math.pi * i / 3
        assert node["x"] == pytest.approx(160 + RING_RADIUS * math.sin(theta), abs=1e-6)
        assert node["y"] == pytest.approx(160 - RING_RADIUS * math.cos(theta), abs=1e-6)
        assert node["r"] == 6 and node["root"] is False


def test_twenty_five_neighbours_draw_twenty_four_and_drop_the_last(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    for i in range(25):
        _make_doc(test_db, doc_id=_neighbour_id(i), title=f"Topic {i:02d}")
        _link(test_db, src=DOC_A, dst=_neighbour_id(i))

    payload = _graph_of(client, DOC_A)
    drawn = [n["title"] for n in payload["nodes"][1:]]

    assert payload["truncated"] == 1
    assert drawn == [f"Topic {i:02d}" for i in range(24)]
    last = _neighbour_id(24)
    assert last not in {n["id"] for n in payload["nodes"]}
    assert all(last not in (e["src"], e["dst"]) for e in payload["edges"]), (
        "an edge to the truncated neighbour shipped without its node"
    )
    assert len(payload["edges"]) == 24


def test_a_derived_link_is_an_edge_of_kind_derived(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    _make_doc(test_db, doc_id=DOC_B, title="Vendor Evaluation")
    _derived(test_db, a=DOC_A, b=DOC_B)

    payload = _graph_of(client, DOC_A)

    assert payload["edges"] == [{"src": DOC_A, "dst": DOC_B, "kind": "derived"}]


def test_repeated_refs_to_one_target_are_one_edge(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    _make_doc(test_db, doc_id=DOC_B, title="Vendor Evaluation")
    _link(test_db, src=DOC_A, dst=DOC_B, text="[[Vendor Evaluation]]")
    _link(test_db, src=DOC_A, dst=DOC_B, text="[[Vendor Evaluation|vendors]]")

    assert _graph_of(client, DOC_A)["edges"] == [
        {"src": DOC_A, "dst": DOC_B, "kind": "wiki"}
    ]


def test_the_payload_carries_no_bodies(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    _make_doc(test_db, doc_id=DOC_B, title="Vendor Evaluation")
    _link(test_db, src=DOC_A, dst=DOC_B)

    assert "body of" not in client.get(f"/api/notes/{DOC_A}/graph").text


# ------------------------------------------------ the confidentiality gate --


@pytest.fixture
def sealed_neighbourhood(test_db: psycopg.Connection) -> None:
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    _make_doc(test_db, doc_id=DOC_B, title="Vendor Evaluation")
    _seal(test_db, _make_doc(test_db, doc_id=DOC_S, title=SEALED_TITLE))
    _link(test_db, src=DOC_A, dst=DOC_B)
    _link(test_db, src=DOC_A, dst=DOC_S)


def test_a_confidential_neighbour_and_its_edge_are_withheld(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: Any,
    sealed_neighbourhood: None,
) -> None:
    payload = _graph_of(_client(test_db, tmp_path, fake_embedder), DOC_A)

    assert DOC_S not in {n["id"] for n in payload["nodes"]}
    assert SEALED_TITLE not in {n["title"] for n in payload["nodes"]}
    assert all(DOC_S not in (e["src"], e["dst"]) for e in payload["edges"])
    assert payload["truncated"] == 0, "the gate must not surface as a count"
    assert {n["id"] for n in payload["nodes"]} == {DOC_A, DOC_B}, (
        "anti-vacuity: the ordinary neighbour must still be drawn"
    )


def test_an_opted_in_session_draws_the_confidential_neighbour(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: Any,
    sealed_neighbourhood: None,
) -> None:
    payload = _graph_of(
        _client(test_db, tmp_path, fake_embedder, serve_confidential_titles=True),
        DOC_A,
    )

    assert DOC_S in {n["id"] for n in payload["nodes"]}
    assert {"src": DOC_A, "dst": DOC_S, "kind": "wiki"} in payload["edges"]


def test_a_confidential_root_in_a_strict_session_is_a_typed_403(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder: Any
) -> None:
    """graph_data withholds the root itself; the route must not 500 or leak."""
    _seal(test_db, _make_doc(test_db, doc_id=DOC_S, title=SEALED_TITLE))
    _make_doc(test_db, doc_id=DOC_B, title="Vendor Evaluation")
    _link(test_db, src=DOC_S, dst=DOC_B)

    # Server exceptions are rendered, not raised: without the check the route
    # 500s (layout's ValueError), and that must fail HERE, on the status code.
    client = _client(test_db, tmp_path, fake_embedder, raise_server_exceptions=False)
    response = client.get(f"/api/notes/{DOC_S}/graph")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "graph_withheld"
    body = response.text
    for leaked in (SEALED_TITLE, "Vendor Evaluation", DOC_B):
        assert leaked not in body, f"the 403 envelope named {leaked!r}"


# ------------------------------------------------------- the query helpers --


def test_corpus_has_links_reads_both_tables(test_db: psycopg.Connection) -> None:
    assert queries.corpus_has_links(test_db) is False
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    _make_doc(test_db, doc_id=DOC_B, title="Vendor Evaluation")
    _link(test_db, src=DOC_A, dst=DOC_B)
    assert queries.corpus_has_links(test_db) is True


def test_document_sensitivity_reads_the_tier_and_none_for_no_match(
    test_db: psycopg.Connection,
) -> None:
    _seal(test_db, _make_doc(test_db, doc_id=DOC_S, title=SEALED_TITLE))
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")

    assert queries.document_sensitivity(test_db, DOC_S) == CONFIDENTIAL
    assert queries.document_sensitivity(test_db, DOC_A) == DEFAULT_SENSITIVITY
    assert queries.document_sensitivity(test_db, DOC_D) is None


# ------------------------------------------------------------ fails closed --


def test_unknown_id_is_a_typed_404(client: TestClient, test_db: psycopg.Connection) -> None:
    _make_doc(test_db, doc_id=DOC_A, title="Planning Sync")
    response = client.get("/api/notes/deadbeefdead/graph")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "note_not_found"


def test_ambiguous_prefix_is_a_typed_400(
    client: TestClient, test_db: psycopg.Connection
) -> None:
    _make_doc(test_db, doc_id="abcdef00-0000-4000-8000-000000000001", title="One")
    _make_doc(test_db, doc_id="abcdef00-0000-4000-8000-000000000002", title="Two")
    response = client.get("/api/notes/abcdef00/graph")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "id_prefix_ambiguous"


def test_a_database_failure_is_a_leak_free_503(tmp_path: Path, fake_embedder: Any) -> None:
    @contextlib.contextmanager
    def broken_factory() -> Any:
        raise psycopg.OperationalError(
            "connection refused to 10.0.0.1:5432 as brain: SELECT 1 FROM links"
        )
        yield  # pragma: no cover — unreachable, keeps this a generator

    app = create_app(_context(broken_factory, tmp_path, fake_embedder))
    response = TestClient(app, base_url=ORIGIN).get(f"/api/notes/{DOC_A}/graph")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    body = response.text
    assert "10.0.0.1" not in body and "brain" not in body
    assert "SELECT" not in body and "links" not in body


# ------------------------------------------- spec §6.5: no graph-build path --

_FORBIDDEN_PACKAGES = ("brain.maintenance", "brain.graph_rag", "brain.wiki")


def _absolute_imports(module: Any) -> set[str]:
    """Every module name ``module`` imports, relative imports resolved."""
    path = Path(module.__file__)
    package_parts = module.__name__.split(".")[:-1]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - node.level + 1]
                prefix = ".".join(base)
                mod = f"{prefix}.{node.module}" if node.module else prefix
            else:
                mod = node.module or ""
            names.add(mod)
            names.update(f"{mod}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("module", [routes_graph, graph_layout])
def test_graph_surfaces_cannot_reach_a_build_path(module: Any) -> None:
    imported = _absolute_imports(module)
    assert imported, "anti-vacuity: the parser found no imports at all"
    bad = {
        name
        for name in imported
        for pkg in _FORBIDDEN_PACKAGES
        if name == pkg or name.startswith(f"{pkg}.")
    }
    assert not bad, f"{module.__name__} imports a build path: {sorted(bad)}"


def test_the_import_resolver_sees_relative_imports() -> None:
    """Guard the guard: ``from ..vault.graph import graph_data`` must resolve."""
    assert "brain.vault.graph" in _absolute_imports(routes_graph)
