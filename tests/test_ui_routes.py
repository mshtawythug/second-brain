"""The API routes, end-to-end against a real Postgres and a real vault.

Uses the suite's ``test_db`` connection and ``fake_embedder`` fixtures, plus a
``tmp_path`` vault, so nothing here touches Ollama or the production database.

``UiContext`` carries the connection factory, so the app is handed a factory
that yields the *test* connection rather than opening its own — that is what
keeps a route's writes visible to the test's assertions inside one transaction.
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
from brain.vault import init_vault
from brain.vault.note_builder import create_vault_note

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"
WRITE_HEADERS = {"Origin": ORIGIN}


@pytest.fixture
def ui_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    return vault


@pytest.fixture
def ui_cfg(ui_vault: Path) -> Config:
    return Config(
        database_url="postgresql://unused/in/these/tests",
        vault_path=ui_vault,
        embedder="none",
    )


@pytest.fixture
def seeded(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any
) -> dict[str, str]:
    """Two synthetic vault notes. No PII anywhere in this corpus."""
    ids = {}
    for title, folder, tags, body in [
        (
            "Q3 Planning Sync",
            "projects",
            ["planning"],
            "Three unresolved threads.\n\nSee [[Vendor Evaluation Notes]].\n",
        ),
        (
            "Vendor Evaluation Notes",
            "projects",
            ["vendors"],
            "Acme Holdings scored highest.\nContact person-b@example.invalid\n",
        ),
    ]:
        ids[title] = create_vault_note(
            test_db,
            cfg=ui_cfg,
            vault_path=ui_cfg.vault_path,
            title=title,
            body=body,
            tags=tags,
            template="note",
            folder=folder,
            embedder=fake_embedder,
        )
    return ids


@pytest.fixture
def client(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any
) -> TestClient:
    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    from brain.search import hybrid_search

    context = UiContext(
        cfg=ui_cfg,
        conn_factory=conn_factory,
        embedder=fake_embedder,
        search_fn=hybrid_search,
        allowed_origin=ORIGIN,
        logging_enabled=False,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


# --------------------------------------------------------------------- read --


def test_health_needs_no_database(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["read_only"] is False


def test_status_reports_counts(client: TestClient, seeded: dict[str, str]) -> None:
    payload = client.get("/api/status").json()
    assert payload["documents"] >= 2


def test_tree_nests_the_vault(client: TestClient, seeded: dict[str, str]) -> None:
    payload = client.get("/api/tree").json()
    assert payload["count"] >= 2
    names = [child["name"] for child in payload["children"]]
    assert "projects" in names


def test_facets_offer_all_known_sources(client: TestClient) -> None:
    payload = client.get("/api/facets").json()
    values = {bucket["value"] for bucket in payload["sources"]}
    # Unioned with the known kinds so a dropdown offers slack before the first
    # slack ingest, rather than the option appearing out of nowhere later.
    assert {"manual", "krisp", "gmail", "slack"} <= values


def test_get_note_by_prefix(client: TestClient, seeded: dict[str, str]) -> None:
    doc_id = seeded["Q3 Planning Sync"]
    payload = client.get(f"/api/notes/{doc_id[:8]}").json()
    assert payload["title"] == "Q3 Planning Sync"
    assert payload["tier"] == "vault"
    assert payload["vault_path"] == "projects/q3-planning-sync.md"
    assert payload["body_hash"]
    assert "<p>" in payload["html"]
    assert payload["editable"] is True


def test_wikilink_in_body_resolves_to_the_other_note(
    client: TestClient, seeded: dict[str, str]
) -> None:
    payload = client.get(f"/api/notes/{seeded['Q3 Planning Sync'][:8]}").json()
    assert f'href="?id={seeded["Vendor Evaluation Notes"]}"' in payload["html"]


@pytest.mark.parametrize(
    ("prefix", "status", "code"),
    [
        ("abc", 400, "id_prefix_too_short"),
        ("zzzzzz", 400, "id_prefix_not_hex"),
        ("abcdef123456", 404, "note_not_found"),
    ],
)
def test_bad_prefixes_are_rejected(
    client: TestClient, prefix: str, status: int, code: str
) -> None:
    response = client.get(f"/api/notes/{prefix}")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


# -------------------------------------------------------------------- write --


def test_create_note_writes_file_and_row(
    client: TestClient, ui_cfg: Config
) -> None:
    response = client.post(
        "/api/notes",
        json={"title": "Retro Notes", "folder": "daily", "tags": ["Interview Prep"]},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 201
    created = response.json()
    assert (ui_cfg.vault_path / "daily" / "retro-notes.md").is_file()

    fetched = client.get(f"/api/notes/{created['id'][:8]}").json()
    # Tags normalize at the write boundary: "Interview Prep" -> "interview-prep".
    assert "interview-prep" in fetched["tags"]


def test_folder_traversal_is_blocked_and_writes_nothing(
    client: TestClient, ui_cfg: Config, tmp_path: Path
) -> None:
    response = client.post(
        "/api/notes",
        json={"title": "Escape", "folder": "../../etc"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "folder_escapes_vault"
    assert not (tmp_path / "etc").exists()


def test_update_requires_body_hash(
    client: TestClient, seeded: dict[str, str]
) -> None:
    response = client.put(
        f"/api/notes/{seeded['Q3 Planning Sync'][:8]}",
        json={"body": "rewritten"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_body_hash"


def test_stale_body_hash_is_a_conflict(
    client: TestClient, seeded: dict[str, str]
) -> None:
    """The watcher and brain-mcp are live writers on the same file."""
    response = client.put(
        f"/api/notes/{seeded['Q3 Planning Sync'][:8]}",
        json={"body_hash": "sha256:stale", "body": "rewritten"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_write"


def test_update_rewrites_the_file_and_keeps_the_uuid(
    client: TestClient, seeded: dict[str, str], ui_cfg: Config
) -> None:
    doc_id = seeded["Q3 Planning Sync"]
    current = client.get(f"/api/notes/{doc_id[:8]}").json()
    response = client.put(
        f"/api/notes/{doc_id[:8]}",
        json={"body_hash": current["body_hash"], "body": "# Rewritten\n\nNew body.\n"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["id"] == doc_id          # UUID preserved
    assert response.json()["rechunked"] is True

    on_disk = (ui_cfg.vault_path / "projects" / "q3-planning-sync.md").read_text()
    assert "New body." in on_disk
    assert "id:" in on_disk or "title:" in on_disk   # frontmatter survived


def test_draft_toggle_round_trips(
    client: TestClient, seeded: dict[str, str]
) -> None:
    doc_id = seeded["Q3 Planning Sync"][:8]
    assert client.post(
        f"/api/notes/{doc_id}/draft", json={"draft": True}, headers=WRITE_HEADERS
    ).status_code == 200
    assert client.get(f"/api/notes/{doc_id}").json()["draft"] is True
    # Idempotent: setting the value it already has is a no-op, not an error.
    assert client.post(
        f"/api/notes/{doc_id}/draft", json={"draft": True}, headers=WRITE_HEADERS
    ).status_code == 200
    client.post(
        f"/api/notes/{doc_id}/draft", json={"draft": False}, headers=WRITE_HEADERS
    )
    assert client.get(f"/api/notes/{doc_id}").json()["draft"] is False


# --------------------------------------------------------------- destructive --


def test_delete_without_confirm_is_refused(
    client: TestClient, seeded: dict[str, str]
) -> None:
    response = client.request(
        "DELETE",
        f"/api/notes/{seeded['Q3 Planning Sync'][:8]}",
        json={"expected_title": "Q3 Planning Sync"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirm_required"


def test_delete_with_a_wrong_title_deletes_nothing(
    client: TestClient, seeded: dict[str, str]
) -> None:
    """Server-side title check — the 2026-06-09 incident in test form."""
    doc_id = seeded["Q3 Planning Sync"][:8]
    response = client.request(
        "DELETE",
        f"/api/notes/{doc_id}",
        json={"confirm": True, "expected_title": "Something Else"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "title_mismatch"
    assert client.get(f"/api/notes/{doc_id}").status_code == 200   # still there


def test_delete_removes_row_and_mirror(
    client: TestClient, seeded: dict[str, str], ui_cfg: Config
) -> None:
    doc_id = seeded["Q3 Planning Sync"]
    mirror = ui_cfg.vault_path / "projects" / "q3-planning-sync.md"
    assert mirror.is_file()

    response = client.request(
        "DELETE",
        f"/api/notes/{doc_id[:8]}",
        json={"confirm": True, "expected_title": "Q3 Planning Sync"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["mirror_unlinked"] is True
    # Without the unlink, the next `brain vault sync` would resurrect it.
    assert not mirror.exists()
    assert client.get(f"/api/notes/{doc_id[:8]}").status_code == 404


def test_move_preserves_the_uuid_and_updates_vault_path(
    client: TestClient, seeded: dict[str, str], ui_cfg: Config
) -> None:
    doc_id = seeded["Q3 Planning Sync"]
    response = client.post(
        f"/api/notes/{doc_id[:8]}/move",
        json={"confirm": True, "new_folder": "archive"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["id"] == doc_id
    assert response.json()["vault_path"].startswith("archive/")
    assert (ui_cfg.vault_path / "archive" / "q3-planning-sync.md").is_file()


def test_move_outside_the_vault_is_refused(
    client: TestClient, seeded: dict[str, str]
) -> None:
    response = client.post(
        f"/api/notes/{seeded['Q3 Planning Sync'][:8]}/move",
        json={"confirm": True, "new_folder": "../outside"},
        headers=WRITE_HEADERS,
    )
    assert response.status_code == 400
