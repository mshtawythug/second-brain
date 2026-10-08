"""`GET /api/notes/{id}/related` — the related-docs panel's server route.

The route is a thin projection of :func:`brain.related.compute_related`, so the
ranking itself is pinned by :mod:`tests.test_related_compute`; what is pinned
here is what the ROUTE decides:

* R1 — the cosine floor is ``ctx.cfg.vector_sim_floor`` (the value runtime
  ``brain search`` reads), passed through and echoed, never a literal.
* R6 — ``snippet`` is chunk content, so a confidential candidate is served
  only when BOTH the title lens and the body lens are permissive.
* R7 — ``snippet`` is capped at ``ctx.cfg.snippet_max_chars`` and a cut is
  flagged with ``snippet_truncated``, never silent.
* §6.5 — the module imports nothing from the maintenance / graph / wiki layers.

Documents are seeded with direct SQL plus chunks and embeddings, the way
:mod:`tests.test_related_compute` does, so both the FTS and the vector leg
have something to rank. Fixtures are synthetic throughout (CLAUDE.md rule 15).
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.config import Config
from brain.queries import sync_chunk_search_metadata
from brain.related import DEFAULT_RELATED_LIMIT, RelatedDoc
from brain.sensitivity import CONFIDENTIAL
from brain.ui import routes_related
from brain.ui.app import create_app
from brain.ui.context import UiContext

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"
VECTOR_DIM = 4096

#: Synthetic, fixed UUIDs so a failure message is readable. ``AMBIG_1`` and
#: ``AMBIG_2`` share the six-character prefix ``abcdef`` on purpose.
DOC_A = "aaaaaaaa-0000-4000-8000-00000000000a"
DOC_B = "bbbbbbbb-0000-4000-8000-00000000000b"
DOC_C = "cccccccc-0000-4000-8000-00000000000c"
DOC_D = "dddddddd-0000-4000-8000-00000000000d"
AMBIG_1 = "abcdef01-0000-4000-8000-000000000001"
AMBIG_2 = "abcdef02-0000-4000-8000-000000000002"

#: An off-default floor no other code path would produce by accident.
SENTINEL_FLOOR = 0.1234


def _vector(*components: float) -> str:
    """A pgvector literal of length ``VECTOR_DIM``; leading components set."""
    values = [0.0] * VECTOR_DIM
    for index, value in enumerate(components):
        values[index] = value
    return "[" + ",".join(str(v) for v in values) + "]"


def _insert_doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    vault_path: str | None,
    chunk_contents: list[str],
    chunk_vectors: list[str] | None = None,
    draft: bool = False,
) -> str:
    """Insert a document plus chunks, then sync the weighted search metadata."""
    body = "\n".join(chunk_contents) if chunk_contents else f"{title} body"
    conn.execute(
        """
        INSERT INTO documents
          (id, title, content, content_hash, content_type, vault_path, draft, kind)
        VALUES (%s::uuid, %s, %s, %s, 'note', %s, %s, 'vault')
        """,
        (doc_id, title, body, f"hash-{doc_id}", vault_path, draft),
    )
    vectors = chunk_vectors or []
    for index, content in enumerate(chunk_contents):
        if index < len(vectors):
            conn.execute(
                "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
                "VALUES (%s::uuid, %s, %s, %s::vector)",
                (doc_id, index, content, vectors[index]),
            )
        else:
            conn.execute(
                "INSERT INTO chunks (document_id, chunk_index, content) "
                "VALUES (%s::uuid, %s, %s)",
                (doc_id, index, content),
            )
    sync_chunk_search_metadata(conn, doc_id)
    return doc_id


#: Long enough to be cut by a small ``snippet_max_chars``; short enough to stay
#: under ``brain.related.SNIPPET_LENGTH`` so compute_related does not cut first.
LONG_BODY = "ZZQUARK mention in body too for dual-leg signal across many words here."
SHORT_BODY = "ZZQUARK short body."


def _seed(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """Source ``A``; two neighbours ``B`` (title + body overlap) and ``C``
    (body only), both near ``A`` on the vector leg.
    """
    near = _vector(0.99, 0.05)
    a = _insert_doc(
        conn,
        doc_id=DOC_A,
        title="ZZQUARK",
        vault_path="notes/a-source.md",
        chunk_contents=["ZZQUARK distinctive content paragraph one."],
        chunk_vectors=[_vector(1.0, 0.0)],
    )
    b = _insert_doc(
        conn,
        doc_id=DOC_B,
        title="ZZQUARK canonical guide",
        vault_path="notes/b-title-overlap.md",
        chunk_contents=[LONG_BODY],
        chunk_vectors=[near],
    )
    c = _insert_doc(
        conn,
        doc_id=DOC_C,
        title="QXBFILLER irrelevant alpha",
        vault_path="notes/c-body-only.md",
        chunk_contents=[SHORT_BODY],
        chunk_vectors=[near],
    )
    return {"A": a, "B": b, "C": c}


def _client(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    *,
    titles: bool = True,
    bodies: bool = True,
    **cfg_overrides: Any,
) -> TestClient:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)

    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    cfg = Config(
        database_url="postgresql://unused/in/these/tests",
        vault_path=vault,
        embedder="none",
        **cfg_overrides,
    )
    context = UiContext(
        cfg=cfg,
        conn_factory=conn_factory,
        embedder=fake_embedder,
        search_fn=lambda *a, **k: [],
        allowed_origin=ORIGIN,
        logging_enabled=False,
        serve_confidential_titles=titles,
        serve_confidential_bodies=bodies,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


def _related_of(client: TestClient, doc_id: str) -> dict[str, Any]:
    response = client.get(f"/api/notes/{doc_id}/related")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


# ------------------------------------------------------------------ shape --


def test_related_candidates_are_ranked_and_counted(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    ids = _seed(test_db)
    client = _client(test_db, tmp_path, fake_embedder, vector_sim_floor=0.0)

    payload = _related_of(client, ids["A"])

    assert set(payload) == {"id", "related", "count", "vector_sim_floor"}
    assert payload["id"] == ids["A"]
    related = payload["related"]
    assert [row["id"] for row in related] == [ids["B"], ids["C"]]
    scores = [row["score"] for row in related]
    assert scores == sorted(scores, reverse=True)
    assert payload["count"] == len(related) == 2
    top = related[0]
    assert set(top) == {
        "id", "title", "vault_path", "source", "score", "snippet", "snippet_truncated",
    }
    assert top["title"] == "ZZQUARK canonical guide"
    assert top["vault_path"] == "notes/b-title-overlap.md"
    assert top["source"] == "vault"
    assert "ZZQUARK" in top["snippet"]


def test_a_prefix_resolves_like_a_full_uuid(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    ids = _seed(test_db)
    client = _client(test_db, tmp_path, fake_embedder, vector_sim_floor=0.0)

    payload = _related_of(client, ids["A"][:8])

    assert payload["id"] == ids["A"]
    assert payload["count"] == 2


# --------------------------------------------------------------------- R1 --


def test_the_floor_is_the_configured_one_and_is_echoed(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1: the floor reaching ``compute_related`` IS ``cfg.vector_sim_floor``.

    The echo alone would pass a route that echoes the config but ranks with a
    literal, so the call itself is recorded through pytest ``monkeypatch``.
    """
    ids = _seed(test_db)
    calls: list[dict[str, Any]] = []

    def recording_compute_related(
        conn: Any, doc_id: str, **kwargs: Any
    ) -> list[RelatedDoc]:
        calls.append({"doc_id": doc_id, **kwargs})
        return []

    monkeypatch.setattr(routes_related, "compute_related", recording_compute_related)
    client = _client(test_db, tmp_path, fake_embedder, vector_sim_floor=SENTINEL_FLOOR)

    payload = _related_of(client, ids["A"])

    assert payload["vector_sim_floor"] == SENTINEL_FLOOR
    assert len(calls) == 1
    assert calls[0]["doc_id"] == ids["A"]
    assert calls[0]["vector_sim_floor"] == SENTINEL_FLOOR
    assert calls[0]["limit"] == DEFAULT_RELATED_LIMIT


def test_the_route_source_carries_no_floor_literal() -> None:
    """R1, structurally: no float literal anywhere in the route module.

    A default or fallback floor written into the route is exactly the silent
    divergence from ``brain search`` that the ruling forbids.
    """
    tree = ast.parse(inspect.getsource(routes_related))
    floats = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert floats == []


# --------------------------------------------------------------------- R7 --


def test_a_long_snippet_is_cut_and_flagged_a_short_one_is_not(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    ids = _seed(test_db)
    cap = len(SHORT_BODY) + 5
    assert len(LONG_BODY) > cap  # the fixture must actually exercise the cut
    client = _client(
        test_db, tmp_path, fake_embedder, vector_sim_floor=0.0, snippet_max_chars=cap
    )

    rows = {row["id"]: row for row in _related_of(client, ids["A"])["related"]}

    long_row, short_row = rows[ids["B"]], rows[ids["C"]]
    assert long_row["snippet"] == LONG_BODY[:cap]
    assert len(long_row["snippet"]) == cap
    assert long_row["snippet_truncated"] is True
    assert short_row["snippet"] == SHORT_BODY
    assert short_row["snippet_truncated"] is False


def test_with_the_default_cap_nothing_is_flagged(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    ids = _seed(test_db)
    client = _client(test_db, tmp_path, fake_embedder, vector_sim_floor=0.0)

    rows = _related_of(client, ids["A"])["related"]

    assert [row["snippet_truncated"] for row in rows] == [False, False]
    assert rows[0]["snippet"] == LONG_BODY


# --------------------------------------------------------------------- R6 --


# Also covers the other direction of the withheld-root gate: the root ``A`` is
# NON-confidential, so under every strict lens below it is still served (200).
@pytest.mark.parametrize(
    ("titles", "bodies", "present"),
    [
        (False, True, False),
        (True, False, False),
        (False, False, False),
        (True, True, True),
    ],
    ids=["titles-off", "bodies-off", "both-off", "both-on"],
)
def test_a_confidential_candidate_needs_both_lenses(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    titles: bool,
    bodies: bool,
    present: bool,
) -> None:
    """``snippet`` is body egress, so the title lens alone is not enough.

    ``C`` is asserted present in every case: without it, ``B`` being absent
    would pass just as well on a fixture that produced no neighbours at all.
    """
    ids = _seed(test_db)
    test_db.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s::uuid",
        (CONFIDENTIAL, ids["B"]),
    )
    client = _client(
        test_db, tmp_path, fake_embedder, titles=titles, bodies=bodies, vector_sim_floor=0.0
    )

    returned = [row["id"] for row in _related_of(client, ids["A"])["related"]]

    assert (ids["B"] in returned) is present
    assert ids["C"] in returned


# ------------------------------------------------------------------ draft --


def test_draft_candidates_never_appear(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    ids = _seed(test_db)
    draft = _insert_doc(
        test_db,
        doc_id=DOC_D,
        title="ZZQUARK draft companion",
        vault_path="notes/d-draft.md",
        chunk_contents=[LONG_BODY],
        chunk_vectors=[_vector(0.99, 0.05)],
        draft=True,
    )
    client = _client(test_db, tmp_path, fake_embedder, vector_sim_floor=0.0)

    returned = [row["id"] for row in _related_of(client, ids["A"])["related"]]

    assert draft not in returned
    assert returned == [ids["B"], ids["C"]]


# ----------------------------------------------------------------- errors --


def test_unknown_id_is_a_typed_404(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    _seed(test_db)
    client = _client(test_db, tmp_path, fake_embedder)

    response = client.get("/api/notes/deadbeefdead/related")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "note_not_found"


def test_an_ambiguous_prefix_is_a_typed_400(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    for doc_id, title in ((AMBIG_1, "Ambiguous One"), (AMBIG_2, "Ambiguous Two")):
        _insert_doc(
            test_db,
            doc_id=doc_id,
            title=title,
            vault_path=f"notes/{doc_id}.md",
            chunk_contents=[f"{title} body."],
        )
    client = _client(test_db, tmp_path, fake_embedder)

    response = client.get("/api/notes/abcdef/related")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "id_prefix_ambiguous"


def test_a_short_prefix_is_a_typed_400(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    client = _client(test_db, tmp_path, fake_embedder)

    response = client.get("/api/notes/abc/related")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "id_prefix_too_short"


def test_a_database_failure_is_a_leak_free_503(
    tmp_path: Path, fake_embedder: Any
) -> None:
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
    response = TestClient(create_app(context), base_url=ORIGIN).get(
        f"/api/notes/{DOC_A}/related"
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    body = response.text
    assert "10.0.0.1" not in body and "brain" not in body and "SELECT" not in body


def test_a_failure_inside_compute_related_is_a_503(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must wrap the related read too, not only the connect."""
    ids = _seed(test_db)

    def failing_compute_related(*_a: Any, **_k: Any) -> list[RelatedDoc]:
        raise psycopg.errors.QueryCanceled("SELECT secret FROM documents")

    monkeypatch.setattr(routes_related, "compute_related", failing_compute_related)
    client = _client(test_db, tmp_path, fake_embedder)

    response = client.get(f"/api/notes/{ids['A']}/related")

    assert response.status_code == 503
    assert "SELECT" not in response.text and "secret" not in response.text


# ------------------------------------------------------------- degenerate --


def test_a_document_without_chunks_still_answers(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    """A root with no chunks gets a well-formed 200 whose ``count`` matches its list.

    The list is deliberately NOT asserted empty: with no chunks there is no
    vector leg, but the FTS leg can still match the root's title against the
    seeded neighbours, so a non-empty list is correct too.
    """
    _seed(test_db)
    bare = _insert_doc(
        test_db,
        doc_id=DOC_D,
        title="ZZQUARK unchunked",
        vault_path="notes/d-bare.md",
        chunk_contents=[],
    )
    client = _client(test_db, tmp_path, fake_embedder, vector_sim_floor=0.0)

    payload = _related_of(client, bare)

    assert isinstance(payload["related"], list)
    assert payload["count"] == len(payload["related"])


def test_a_lone_document_has_an_empty_related_list(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    solo = _insert_doc(
        test_db,
        doc_id=DOC_A,
        title="ZZQUARK",
        vault_path="notes/solo.md",
        chunk_contents=["ZZQUARK distinctive content paragraph one."],
        chunk_vectors=[_vector(1.0, 0.0)],
    )
    client = _client(test_db, tmp_path, fake_embedder, vector_sim_floor=0.0)

    payload = _related_of(client, solo)

    assert payload["related"] == []
    assert payload["count"] == 0


# §6.5 (no build path) is asserted for this route, beside the graph surfaces,
# by the one parametrised test in tests/test_ui_routes_graph.py.


def test_config_fields_the_route_reads_exist() -> None:
    """Guards the attribute names against a rename in ``brain.config``."""
    names = {f.name for f in dataclasses.fields(Config)}
    assert {"vector_sim_floor", "snippet_max_chars"} <= names


# ------------------------------------------------- confidential ROOT document --


@pytest.mark.parametrize(
    ("titles", "bodies"),
    [(False, True), (True, False), (False, False)],
    ids=["titles-off", "bodies-off", "both-off"],
)
def test_a_confidential_root_in_a_strict_session_is_a_403(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
    titles: bool,
    bodies: bool,
) -> None:
    """``compute_related`` gates CANDIDATES, not the source: "documents like
    the withheld one" is a précis of the withheld one. Refused before ranking.
    """
    ids = _seed(test_db)
    test_db.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s::uuid",
        (CONFIDENTIAL, ids["A"]),
    )
    calls: list[str] = []

    def recording_compute_related(conn: Any, doc_id: str, **_k: Any) -> list[RelatedDoc]:
        calls.append(doc_id)
        return []

    monkeypatch.setattr(routes_related, "compute_related", recording_compute_related)
    client = _client(test_db, tmp_path, fake_embedder, titles=titles, bodies=bodies)

    response = client.get(f"/api/notes/{ids['A']}/related")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "related_withheld"
    body = response.text
    assert "ZZQUARK" not in body and "QXBFILLER" not in body
    assert ids["B"] not in body and ids["C"] not in body
    assert calls == [], "the check must run BEFORE compute_related"


def test_a_confidential_root_in_a_permissive_session_is_served(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any
) -> None:
    ids = _seed(test_db)
    test_db.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s::uuid",
        (CONFIDENTIAL, ids["A"]),
    )
    client = _client(test_db, tmp_path, fake_embedder, vector_sim_floor=0.0)

    payload = _related_of(client, ids["A"])

    assert [row["id"] for row in payload["related"]] == [ids["B"], ids["C"]]
