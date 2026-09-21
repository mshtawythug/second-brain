"""REACHABILITY: the phase-2 routes answer from an app built the normal way.

This file exists because of one sentence in the T10 handover: *"the tests
exercise them by appending to a real ``create_app()`` router, which is why they
pass without the wiring."* ``tests/test_ui_routes_links.py`` and
``tests/test_ui_routes_discovery.py`` are correct and thorough about what each
route *returns* — they were written before ``brain.ui.app`` had an owner, and
each mounts its routes itself with ``app.routes.append(...)``. Twenty-one tests
were green while a user could not reach a single one of the four endpoints.

So the split of labour is deliberate and the duplication is not accidental:

* those two modules own BEHAVIOUR — ranking, projection, normalization, the
  typed 400/404/503 envelope. They keep their appends; once the registration
  landed those lines became harmless no-ops (Starlette matches the FIRST route
  that accepts the path, which is now the one in ``create_app``).
* this module owns REACHABILITY, and asserts exactly one thing per route: that
  ``create_app(context)`` — with NOTHING appended, which is the whole point —
  answers it. Deleting a ``Route(...)`` entry from ``ui/app.py`` must turn the
  matching test below 404, and each of the four is an independent claim, so
  there are four tests and four mutations, not one.

The assertions are deliberately thin. A richer payload assertion here would
duplicate the behaviour suites and, worse, would make THIS file fail for
reasons that have nothing to do with wiring — at which point the one question
it exists to answer gets lost in the noise.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.config import Config
from brain.ui.app import create_app
from brain.ui.context import UiContext

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"

#: Synthetic ids and titles — no PII anywhere in this corpus.
DOC_SRC = "eeeeeeee-0000-4000-8000-00000000000e"
DOC_DST = "ffffffff-0000-4000-8000-00000000000f"
TAG = "planning"


def _make_doc(
    conn: psycopg.Connection[Any], *, doc_id: str, title: str, slug: str
) -> str:
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, vault_path, tags)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            doc_id,
            title,
            f"body of {title}",
            f"hash-{doc_id}",
            "note",
            "vault",
            f"notes/{slug}.md",
            [TAG],
        ),
    )
    return doc_id


@pytest.fixture
def wired_corpus(test_db: psycopg.Connection) -> dict[str, str]:
    """Two tagged notes, one wiki link between them.

    Every route below needs a non-empty answer to be worth asserting: a 200 over
    an empty corpus would also be returned by a route that silently found
    nothing, and this file's whole subject is the difference between "answered"
    and "present".
    """
    src = _make_doc(test_db, doc_id=DOC_SRC, title="Planning Sync", slug="planning-sync")
    dst = _make_doc(
        test_db, doc_id=DOC_DST, title="Vendor Evaluation", slug="vendor-evaluation"
    )
    test_db.execute(
        """
        INSERT INTO links
          (src_document_id, dst_document_id, link_text, link_kind, display_text)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (src, dst, "[[Vendor Evaluation]]", "wiki", None),
    )
    return {"src": src, "dst": dst}


@pytest.fixture
def client(
    test_db: psycopg.Connection, tmp_path: Path, fake_embedder: Any
) -> TestClient:
    """The production app, UNMODIFIED.

    There is no ``app.routes.append`` in this module and there must never be
    one. It would restore exactly the blind spot this file was written to close.
    """
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
    return TestClient(create_app(context), base_url=ORIGIN)


def test_api_recent_is_reachable_from_the_shipped_route_table(
    client: TestClient, wired_corpus: dict[str, str]
) -> None:
    response = client.get("/api/recent")

    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()["documents"]} == {
        wired_corpus["src"],
        wired_corpus["dst"],
    }


def test_api_tags_is_reachable_from_the_shipped_route_table(
    client: TestClient, wired_corpus: dict[str, str]
) -> None:
    response = client.get("/api/tags")

    assert response.status_code == 200, response.text
    assert TAG in {row["value"] for row in response.json()["tags"]}


def test_api_tag_page_is_reachable_from_the_shipped_route_table(
    client: TestClient, wired_corpus: dict[str, str]
) -> None:
    """A separate claim from ``/api/tags``: separate path, separate ``Route``.

    ``/api/tags/{tag}`` can be deleted while ``/api/tags`` still answers, so one
    test covering both would report a wiring gap it cannot see.
    """
    response = client.get(f"/api/tags/{TAG}")

    assert response.status_code == 200, response.text
    assert response.json()["tag"] == TAG
    assert {row["id"] for row in response.json()["documents"]} == {
        wired_corpus["src"],
        wired_corpus["dst"],
    }


def test_api_note_links_is_reachable_from_the_shipped_route_table(
    client: TestClient, wired_corpus: dict[str, str]
) -> None:
    """And it is NOT swallowed by ``/api/notes/{id_prefix}``.

    That was the registration's one real ordering question, and this answers it
    by experiment rather than by argument: Starlette's default ``str`` convertor
    compiles to ``[^/]+``, so a path parameter cannot match a ``/`` and the two
    patterns are disjoint at any declaration order. If they were not, this
    request would be served by ``get_note`` and the body would be a note
    payload, not a link payload — so the assertion names a key only the links
    route produces.
    """
    response = client.get(f"/api/notes/{wired_corpus['src']}/links")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["id"] == wired_corpus["src"]
    assert [row["id"] for row in payload["outgoing"]] == [wired_corpus["dst"]]
    # `counts` exists on the links payload and on no other note route, so a
    # response served by `get_note` cannot satisfy this.
    assert set(payload["counts"]) == {"backlinks", "outgoing"}


def test_the_note_route_still_answers_beside_its_links_sibling(
    client: TestClient, wired_corpus: dict[str, str]
) -> None:
    """The other half of the disjointness claim, which the four above cannot make.

    They prove the links route is reachable. They do not prove the new entry did
    not shadow ``GET /api/notes/{id_prefix}`` — a route table where the links
    pattern swallowed the plain note fetch would leave every assertion above
    green and break the app's primary read.
    """
    response = client.get(f"/api/notes/{wired_corpus['src']}")

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Planning Sync"
