"""The Ingest tab's ``possible_duplicates`` key, and its nested lens (RM-19; plan R5, R7).

After a successful ingest the job calls ``near_dup_store.check_document`` and lists the new
document's near-copies. ``routes_jobs.lensed_result`` lenses each entry: unless the server serves
confidential titles, an entry that is not ``normal`` (a missing level included) loses its id,
title and vault path, and when the job's own document is withheld every entry is withheld too. A
failed check never fails the ingest. Jobs run with ``run_next()`` on the test thread. All content
is synthetic.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain import near_dup_store
from brain.ui.app import create_app
from brain.ui.ingest_schemas import PasteSpec
from brain.ui.ingest_service import submit_paste
from brain.ui.jobs import JobRegistry
from brain.ui.routes_jobs import lensed_result
from tests.conftest import FakeEmbedder
from tests.near_dup_fakes import body, insert_doc, near_copy
from tests.ui_ingest_support import ORIGIN, WRITE_HEADERS, make_context, make_registry, make_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_TEXT = body(400, 41)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return make_vault(tmp_path)


def _spec(**overrides: Any) -> PasteSpec:
    fields: dict[str, Any] = {
        "title": "Pasted copy",
        "body": near_copy(_TEXT),
        "tags": (),
        "content_type": "note",
        "sensitivity": "normal",
        "enrich": False,
    }
    fields.update(overrides)
    return PasteSpec(**fields)


def _seed_original(conn: psycopg.Connection[Any], *, sensitivity: str = "normal") -> str:
    doc = insert_doc(
        conn, _TEXT, title="Original note", sensitivity=sensitivity, vault_path="notes/original.md"
    )
    near_dup_store.scan(conn)
    return doc


def _client(
    conn: psycopg.Connection[Any], vault: Path, registry: JobRegistry, **kw: Any
) -> TestClient:
    ctx = make_context(conn, vault, embedder=FakeEmbedder(), jobs=registry, **kw)
    return TestClient(create_app(ctx), base_url=ORIGIN)


def _paste_and_poll(client: TestClient, registry: JobRegistry, **spec: Any) -> dict[str, Any]:
    payload = {"title": "Pasted copy", "body": near_copy(_TEXT), "enrich": False, **spec}
    queued = client.post("/api/ingest/paste", json=payload, headers=WRITE_HEADERS)
    assert queued.status_code == 202, queued.text
    assert registry.run_next()
    done = client.get(f"/api/jobs/{queued.json()['id']}").json()
    assert done["state"] == "succeeded", done["error"]
    result: dict[str, Any] = done["result"]
    return result


# --- the job fills the key ------------------------------------------------------------------


def test_the_job_names_a_signed_near_copy(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    original = _seed_original(test_db)
    ctx = make_context(test_db, vault, embedder=FakeEmbedder())
    assert ctx.jobs is not None and ctx.ingest_runtime is not None

    job = submit_paste(ctx, ctx.jobs, ctx.ingest_runtime, _spec())
    assert ctx.jobs.run_next()
    result = dict(ctx.jobs.get(job.id).result or {})

    [entry] = result["possible_duplicates"]
    assert entry["document_id"] == original
    assert entry["title"] == "Original note"
    assert entry["vault_path"] == "notes/original.md"
    assert entry["sensitivity"] == "normal"
    assert entry["status"] == "open"
    # The new document was signed by the check, so the pair is recorded too.
    assert near_dup_store.count_open(test_db, exclude_confidential=False) == 1


def test_a_unique_paste_lists_no_duplicates(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    registry = make_registry()
    client = _client(test_db, vault, registry)

    result = _paste_and_poll(client, registry, body=body(400, 42))

    assert result["possible_duplicates"] == []


def test_a_failed_check_never_fails_the_ingest(
    test_db: psycopg.Connection[Any],
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # R7: the document is saved, the list is empty, and the log names the class only.
    _seed_original(test_db)

    def _broken(conn: psycopg.Connection[Any], document_id: str) -> list[Any]:
        raise psycopg.errors.InternalError("synthetic failure text that must not be logged")

    monkeypatch.setattr("brain.ui.ingest_service.check_document", _broken)
    registry = make_registry()
    client = _client(test_db, vault, registry)

    with caplog.at_level(logging.WARNING, logger="brain.ui.ingest_service"):
        result = _paste_and_poll(client, registry)

    assert result["outcome"] == "ingested"
    assert result["possible_duplicates"] == []
    assert "InternalError" in caplog.text
    assert "synthetic failure text" not in caplog.text


@pytest.mark.fresh_schema
def test_without_migration_030_the_key_is_empty(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    test_db.execute("DROP TABLE near_duplicates")
    test_db.execute("DROP TABLE document_minhash")
    registry = make_registry()
    client = _client(test_db, vault, registry)

    result = _paste_and_poll(client, registry)

    assert result["outcome"] == "ingested"
    assert result["possible_duplicates"] == []


# --- the nested lens, through the route --------------------------------------------------------


def test_the_route_withholds_a_confidential_duplicate(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    original = _seed_original(test_db, sensitivity="confidential")
    registry = make_registry()
    client = _client(test_db, vault, registry)

    result = _paste_and_poll(client, registry)

    [entry] = result["possible_duplicates"]
    assert entry["withheld"] is True
    assert (entry["document_id"], entry["title"], entry["vault_path"]) == (None, None, None)
    assert original not in str(result)
    assert "Original note" not in str(result)
    # The pasted document itself is normal and stays named.
    assert result["title"] == "Pasted copy"


def test_the_route_serves_a_confidential_duplicate_when_titles_are_served(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    original = _seed_original(test_db, sensitivity="confidential")
    registry = make_registry()
    client = _client(test_db, vault, registry, serve_confidential_titles=True)

    [entry] = _paste_and_poll(client, registry)["possible_duplicates"]

    assert entry["withheld"] is False
    assert entry["document_id"] == original


def test_a_confidential_paste_withholds_every_duplicate(
    test_db: psycopg.Connection[Any], vault: Path
) -> None:
    # A named near-copy of a withheld document discloses its content, so it is withheld too.
    _seed_original(test_db)
    registry = make_registry()
    client = _client(test_db, vault, registry)

    result = _paste_and_poll(client, registry, sensitivity="confidential")

    assert result["title_withheld"] is True
    [entry] = result["possible_duplicates"]
    assert entry["withheld"] is True
    assert "Original note" not in str(result)


# --- the nested lens, as a function ------------------------------------------------------------

_NORMAL = {
    "document_id": "d1", "title": "Seen", "vault_path": "a.md", "sensitivity": "normal",
    "similarity": 0.9, "status": "open",
}
_SECRET = {**_NORMAL, "document_id": "d2", "title": "Hidden", "sensitivity": "confidential"}
_UNLABELLED = {k: v for k, v in _NORMAL.items() if k != "sensitivity"} | {"title": "Unknown"}


def _lensed(entries: Any, *, titles: bool = False, parent: str = "normal") -> Any:
    result = {"title": "Parent", "document_id": "p", "sensitivity": parent,
              "possible_duplicates": entries}
    lensed = lensed_result(result, serve_confidential_titles=titles)
    assert lensed is not None
    return lensed["possible_duplicates"]


@pytest.mark.nodb
def test_the_nested_lens_withholds_confidential_and_unlabelled_entries() -> None:
    normal, secret, unlabelled = _lensed([_NORMAL, _SECRET, _UNLABELLED])

    assert normal == {**_NORMAL, "withheld": False}
    for entry in (secret, unlabelled):
        assert entry["withheld"] is True
        assert (entry["document_id"], entry["title"], entry["vault_path"]) == (None, None, None)
        assert entry["similarity"] == 0.9


@pytest.mark.nodb
def test_the_nested_lens_is_off_when_titles_are_served() -> None:
    entries = _lensed([_NORMAL, _SECRET], titles=True)

    assert [e["withheld"] for e in entries] == [False, False]
    assert entries[1]["title"] == "Hidden"


@pytest.mark.nodb
def test_a_withheld_parent_withholds_every_entry() -> None:
    entries = _lensed([_NORMAL], parent="confidential")

    assert entries[0]["withheld"] is True
    assert entries[0]["title"] is None


@pytest.mark.nodb
@pytest.mark.parametrize("bad", [None, "x", {"title": "Seen"}, 3])
def test_a_malformed_value_fails_closed(bad: Any) -> None:
    assert _lensed(bad) == []


@pytest.mark.nodb
def test_a_malformed_entry_fails_closed() -> None:
    [entry] = _lensed(["not a mapping"])

    assert entry == {"document_id": None, "title": None, "vault_path": None, "withheld": True}


@pytest.mark.nodb
def test_a_result_without_the_key_gains_none() -> None:
    lensed = lensed_result({"title": "x", "sensitivity": "normal"}, serve_confidential_titles=False)

    assert lensed is not None
    assert "possible_duplicates" not in lensed


@pytest.mark.nodb
def test_the_snapshot_is_not_mutated() -> None:
    entries = [dict(_SECRET)]
    result = {"title": "Parent", "sensitivity": "normal", "possible_duplicates": entries}

    lensed_result(result, serve_confidential_titles=False)

    assert entries == [_SECRET]
