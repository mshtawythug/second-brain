"""`GET /api/tree` — confidential titles on the one surface nobody asked for.

The vault tree is fetched by ``boot()`` in ``static/js/main.js`` with no user
action, and ``index.html`` places it in the same viewport, on the same paint, as
the recent rail and the search box. Both of those already filtered by
sensitivity; the tree filtered nothing. So the first frame of the page listed
every confidential title in the vault, next to two surfaces that had decided not
to — three adjacent behaviours, and the difference had never been chosen.

The gate is ``UiContext.serve_confidential_titles``, deliberately **not**
``serve_confidential_bodies``: see that attribute's docstring. This module
proves the gate is consulted in BOTH directions, and that the counts move with
the rows.

MUTATION, MEASURED 2026-08-20 — BOTH DIRECTIONS, because one direction proves
nothing here. A route that ignored its context entirely would still satisfy
whichever branch matched its hard-coded behaviour, so the claim under test is
that the flag is *read*, and only a pair of mutations reddening DISJOINT tests
can show that. Against ``routes_tree.py``'s ``strict = not
ctx.serve_confidential_titles``, run on this file alone (7 tests):

- ``strict = False`` (stuck OPEN) -> **4 failed, 3 passed**:
  ``…_is_not_in_the_tree``, ``…the_id_is_withheld…``,
  ``…counts_do_not_include…``, ``…context_default_hides…``.
- ``strict = True`` (stuck CLOSED) -> **2 failed, 5 passed**:
  ``…opted_in_session_gets…``, ``…counts_include_the_rows…``.

The two sets do not intersect, which is the result that matters: neither branch
is riding on the other's assertions. Every failure was the test's own
sensitivity assertion, not ``_tree``'s status-code check or ``_folder``'s
lookup — checked in the failure output, because a helper failing first would
have made the mutation look caught when it was not.

``test_iter_tree_rows_defaults_to_excluding_confidential`` stayed GREEN under
both, correctly: it bypasses the route. Its own guard was measured separately —
flipping ``iter_tree_rows``' ``exclude_confidential`` default to ``False`` ->
**1 failed, 6 passed**, that test alone.

All fixture data is synthetic.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.config import Config
from brain.sensitivity import CONFIDENTIAL
from brain.ui.app import create_app
from brain.ui.context import UiContext

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"

OPEN_TITLE = "Vendor Shortlist"
SEALED_TITLE = "Compensation Bands"


def _make_doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    vault_path: str,
    sensitivity: str = "normal",
) -> str:
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, kind, vault_path,
           tags, draft, sent_at, sensitivity)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            doc_id,
            title,
            f"body of {title}",
            f"hash-{doc_id}",
            "note",
            "vault",
            vault_path,
            [],
            False,
            datetime(2026, 3, 4, 12, 0, tzinfo=UTC),
            sensitivity,
        ),
    )
    return doc_id


@pytest.fixture
def corpus(test_db: psycopg.Connection[Any]) -> dict[str, str]:
    """Two exported notes in one folder: one ordinary, one confidential.

    Same folder on purpose — it makes the folder's ``note_count`` a number that
    has to change when a row is withheld, which is the count-consistency claim.
    """
    return {
        "open": _make_doc(
            test_db,
            doc_id="a1111111-0000-4000-8000-000000000001",
            title=OPEN_TITLE,
            vault_path="projects/vendor-shortlist.md",
        ),
        "sealed": _make_doc(
            test_db,
            doc_id="a1111111-0000-4000-8000-000000000002",
            title=SEALED_TITLE,
            vault_path="projects/compensation-bands.md",
            sensitivity=CONFIDENTIAL,
        ),
    }


def _app(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    *,
    serve_confidential_titles: bool,
) -> TestClient:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)

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
        serve_confidential_titles=serve_confidential_titles,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


@pytest.fixture
def hiding(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> TestClient:
    """The default shape: confidential titles withheld from the tree."""
    return _app(test_db, tmp_path, fake_embedder, serve_confidential_titles=False)


@pytest.fixture
def naming(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> TestClient:
    """The opted-in shape: ``BRAIN_UI_SERVE_CONFIDENTIAL_TITLES`` is on."""
    return _app(test_db, tmp_path, fake_embedder, serve_confidential_titles=True)


def _tree(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/tree")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _titles(node: dict[str, Any]) -> set[str]:
    """Every note title anywhere in the tree."""
    found = {note["title"] for note in node["notes"]}
    for child in node["children"]:
        found |= _titles(child)
    return found


def _folder(node: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [c for c in node["children"] if c["name"] == name]
    assert matches, f"no {name!r} folder in {[c['name'] for c in node['children']]}"
    return matches[0]


# ------------------------------------------------------------------ hiding --


def test_a_confidential_title_is_not_in_the_tree(
    hiding: TestClient, corpus: dict[str, str]
) -> None:
    """The ruling, stated as an assertion.

    The ordinary title is asserted PRESENT in the same breath. Without it this
    would pass just as well against an empty tree, a 500 swallowed into an empty
    payload, or a fixture that never inserted anything — and an absence-only
    assertion is exactly the shape that passes for the wrong reason.
    """
    titles = _titles(_tree(hiding))

    assert OPEN_TITLE in titles, (
        "the ordinary note is missing too, so the absence below would hold even "
        "if sensitivity were ignored entirely"
    )
    assert SEALED_TITLE not in titles, (
        f"/api/tree names a confidential document on an unprompted surface: "
        f"{sorted(titles)}"
    )


def test_the_id_is_withheld_with_the_title(
    hiding: TestClient, corpus: dict[str, str]
) -> None:
    """A withheld ROW, not a blanked title.

    Shipping the node with the title stripped would still hand a client the
    document id — and ``GET /api/notes/{id}`` is a different surface with a
    different gate, so the tree would have become a directory of things to try.
    """
    payload = _tree(hiding)
    assert corpus["sealed"] not in str(payload)


def test_the_counts_do_not_include_the_rows_the_tree_hides(
    hiding: TestClient, corpus: dict[str, str]
) -> None:
    """A surface that hides rows must not display a count that includes them.

    Both numbers are checked because they are produced by different code:
    ``payload["count"]`` is ``len(rows)`` in ``routes_tree``, while a folder's
    ``note_count`` is folded in ``tree.to_payload``. Filtering in the SQL is what
    makes both correct at once; a post-fold filter would have had to fix every
    node by hand, and the one it missed would be the bug.
    """
    payload = _tree(hiding)

    assert payload["count"] == 1
    projects = _folder(payload, "projects")
    assert projects["note_count"] == 1
    assert projects["vault_count"] + projects["ingested_count"] == 1


# ------------------------------------------------------------------ naming --


def test_an_opted_in_session_gets_the_confidential_title(
    naming: TestClient, corpus: dict[str, str]
) -> None:
    """The permissive branch, and its own test rather than a parametrised case.

    A route that ignored its context entirely would satisfy whichever branch
    matched its hard-coded behaviour, and the parameter would look tested while
    proving nothing. Two fixtures fail separately; that separation is the proof
    the flag is READ.
    """
    titles = _titles(_tree(naming))

    assert SEALED_TITLE in titles, (
        "an opted-in session did not get the title, so the gate is stuck closed"
    )
    assert OPEN_TITLE in titles


def test_the_counts_include_the_rows_an_opted_in_session_sees(
    naming: TestClient, corpus: dict[str, str]
) -> None:
    """The count moves with the rows in BOTH directions, not just downward."""
    payload = _tree(naming)

    assert payload["count"] == 2
    assert _folder(payload, "projects")["note_count"] == 2


# ------------------------------------------------------------------ default --


def test_the_context_default_hides_rather_than_names(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    corpus: dict[str, str],
) -> None:
    """Fail closed: a ``UiContext`` built without the flag must hide.

    Every other fixture in this module passes the flag explicitly, so none of
    them would notice the dataclass default flipping. This one constructs the
    context the way a forgetful caller would.
    """
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)

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
    client = TestClient(create_app(context), base_url=ORIGIN)

    assert SEALED_TITLE not in _titles(_tree(client))


# ------------------------------------------------------------------- query --


def test_iter_tree_rows_defaults_to_excluding_confidential(
    test_db: psycopg.Connection[Any], corpus: dict[str, str]
) -> None:
    """The QUERY fails closed, independently of any route.

    Every route test above passes the flag explicitly, so none of them would
    notice ``iter_tree_rows``' own default flipping — and that default is the
    thing protecting the next caller, who will not be ``routes_tree``. Asserted
    against the permissive call in the same test so "defaults to exclude" is
    distinguished from "cannot return the row at all".
    """
    from brain.ui import queries as ui_queries

    titles_by_default = {row[1] for row in ui_treerows(ui_queries, test_db)}
    titles_permissive = {
        row[1]
        for row in ui_queries.iter_tree_rows(test_db, exclude_confidential=False)
    }

    assert SEALED_TITLE not in titles_by_default
    assert OPEN_TITLE in titles_by_default
    assert SEALED_TITLE in titles_permissive


def ui_treerows(ui_queries: Any, conn: psycopg.Connection[Any]) -> list[Any]:
    """Call ``iter_tree_rows`` with NO sensitivity argument — the default path."""
    return ui_queries.iter_tree_rows(conn)
