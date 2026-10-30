"""The Ingest tab's inbox folder over HTTP, against a real Postgres (RM-40).

A ``tmp_path`` inbox, configured in a ``tmp_path`` BRAIN_HOME's ``drop-folders.toml``;
jobs run with ``run_next()`` on the test thread (the client is never entered, so the
worker never starts). One test per refusal (AC1), the file swapped after the listing
and after the submit (AC2), the file in ``imported/`` and in the tree (AC4), and every
failure moving the file back (RM-40.D4). All names and bytes are synthetic.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.drop_folder import IMPORTED_DIR, DropFolderError, FileOutcome
from brain.errors import EmbedError
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.ui.ingest_schemas import InboxSpec
from brain.ui.ingest_service import INBOX_KIND, inbox_failure, inbox_runner
from brain.ui.jobs import JobRegistry, JobSnapshot
from tests.conftest import FakeEmbedder
from tests.secret_fixtures import SYNTHETIC_AWS_KEY
from tests.ui_ingest_support import (
    ORIGIN,
    WRITE_HEADERS,
    make_context,
    make_registry,
    make_vault,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

BODY = b"# Glorp plan\n\nA synthetic paragraph about flonking the zib.\n"


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    path = tmp_path / "inbox"
    path.mkdir()
    return path


@pytest.fixture
def home(tmp_path: Path, inbox: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    _configure(path, inbox)
    return path


def _configure(home: Path, inbox: Path, *, extra: str = "") -> None:
    (home / "drop-folders.toml").write_text(
        f'[[folder]]\npath = "{inbox}"\nkind = "manual"\nauto = false\n'
        f'extensions = [".md", ".txt", ".pdf", ".png"]\n{extra}',
        encoding="utf-8",
    )


@pytest.fixture
def registry() -> JobRegistry:
    return make_registry()


def _context(
    conn: psycopg.Connection[Any], tmp_path: Path, home: Path, registry: JobRegistry,
    *, embedder: Any = None, cfg: dict[str, Any] | None = None, **kw: Any,
) -> UiContext:
    return make_context(
        conn, make_vault(tmp_path), embedder=embedder or FakeEmbedder(), jobs=registry,
        cfg_overrides={"brain_home": home, **(cfg or {})}, **kw,
    )


@pytest.fixture
def ctx(
    test_db: psycopg.Connection[Any], tmp_path: Path, home: Path, registry: JobRegistry
) -> UiContext:
    return _context(test_db, tmp_path, home, registry, serve_confidential_titles=True)


@pytest.fixture
def client(ctx: UiContext) -> TestClient:
    return TestClient(create_app(ctx), base_url=ORIGIN)


def _drop(inbox: Path, name: str, body: bytes = BODY) -> Path:
    path = inbox / name
    path.write_bytes(body)
    os.utime(path, (1_700_000_000, 1_700_000_000))  # well outside the settle window
    return path


def _post(client: TestClient, payload: dict[str, Any]) -> Any:
    return client.post("/api/ingest/inbox", json=payload, headers=WRITE_HEADERS)


def _run(client: TestClient, registry: JobRegistry, payload: dict[str, Any]) -> JobSnapshot:
    response = _post(client, payload)
    assert response.status_code == 202, response.text
    assert registry.run_next()
    return registry.get(response.json()["id"])


def _sha8(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:8]


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _documents(conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT d.title, d.source_path, d.sensitivity, s.kind FROM documents d"
        " LEFT JOIN sources s ON s.id = d.source_id ORDER BY d.ingested_at"
    ).fetchall()


# ------------------------------------------------------------------ the listing --


def test_the_listing_is_names_and_sizes_with_no_path(client: TestClient, inbox: Path) -> None:
    _drop(inbox, "Glorp plan.md")
    _drop(inbox, "picture.png")  # the folder takes it; the inbox does not (D3)

    response = client.get("/api/ingest/inbox")

    payload = response.json()
    assert response.status_code == 200
    assert payload == {
        "available": True, "reason": None, "extensions": [".md", ".pdf", ".txt"],
        "files": [{"name": "Glorp plan.md", "size": len(BODY)}], "truncated": False,
        "default_sensitivity": "confidential",
    }
    assert str(inbox) not in response.text and str(inbox.parent) not in response.text


@pytest.mark.parametrize(
    ("kw", "reason"),
    [({"ingest_enabled": False}, "requires_loopback"), ({"read_only": True}, "read_only")],
)
def test_a_server_that_may_not_ingest_lists_no_names(
    test_db: psycopg.Connection[Any], tmp_path: Path, home: Path, registry: JobRegistry,
    inbox: Path, kw: dict[str, Any], reason: str,
) -> None:
    _drop(inbox, "private name.md")
    client = TestClient(create_app(_context(test_db, tmp_path, home, registry, **kw)),
                        base_url=ORIGIN)

    payload = client.get("/api/ingest/inbox").json()

    assert payload["available"] is False and payload["reason"] == reason
    assert payload["files"] == [] and "private name" not in str(payload)
    assert payload["default_sensitivity"] is None


def test_no_configured_inbox_says_so(client: TestClient, home: Path) -> None:
    (home / "drop-folders.toml").unlink()

    assert client.get("/api/ingest/inbox").json()["reason"] == "inbox_unconfigured"


def test_a_bad_config_file_says_so(client: TestClient, home: Path) -> None:
    (home / "drop-folders.toml").write_text("[[folder]\n", encoding="utf-8")

    assert client.get("/api/ingest/inbox").json()["reason"] == "inbox_misconfigured"


@pytest.fixture
def unreadable(inbox: Path) -> Iterator[Path]:
    inbox.chmod(0o300)  # searchable for the config's is_dir, not listable
    yield inbox
    inbox.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_an_unreadable_inbox_says_so(client: TestClient, unreadable: Path) -> None:
    assert client.get("/api/ingest/inbox").json()["reason"] == "inbox_unreadable"


# ---------------------------------------------------------- the submit (AC4) --


def test_a_picked_file_ends_in_imported_and_in_the_tree(
    client: TestClient, registry: JobRegistry, inbox: Path, test_db: psycopg.Connection[Any]
) -> None:
    _drop(inbox, "Glorp plan.md")

    snap = _run(client, registry, {"name": "Glorp plan.md", "enrich": False})

    imported = inbox / IMPORTED_DIR / f"Glorp plan-{_sha8(BODY)}.md"
    assert snap.kind == INBOX_KIND and snap.state == "succeeded", snap.error
    assert _tree(inbox) == {f"{IMPORTED_DIR}/Glorp plan-{_sha8(BODY)}.md": BODY}
    assert _documents(test_db) == [("Glorp plan", str(imported), "confidential", "manual")]
    result = dict(snap.result or {})
    assert result["outcome"] == "ingested" and result["original_filename"] == "Glorp plan.md"
    tree = client.get("/api/tree").text
    assert "Glorp plan" in tree


def test_the_request_may_override_title_tags_and_sensitivity(
    client: TestClient, registry: JobRegistry, inbox: Path, test_db: psycopg.Connection[Any]
) -> None:
    _drop(inbox, "raw.txt", b"Plain synthetic text for the inbox.\n")

    snap = _run(client, registry, {"name": "raw.txt", "title": "Chosen", "tags": ["Inbox"],
                                   "sensitivity": "normal", "enrich": False})

    assert snap.state == "succeeded"
    row = test_db.execute("SELECT title, sensitivity, tags FROM documents").fetchone()
    assert row == ("Chosen", "normal", ["inbox"])


def test_the_folder_sensitivity_is_the_default(
    client: TestClient, registry: JobRegistry, inbox: Path, home: Path,
    test_db: psycopg.Connection[Any],
) -> None:
    _configure(home, inbox, extra='sensitivity = "normal"\n')
    _drop(inbox, "open.md")

    assert _run(client, registry, {"name": "open.md", "enrich": False}).state == "succeeded"
    assert _documents(test_db)[0][2] == "normal"


def test_a_confidential_result_is_lensed_where_titles_are_not_served(
    test_db: psycopg.Connection[Any], tmp_path: Path, home: Path, registry: JobRegistry,
    inbox: Path,
) -> None:
    client = TestClient(create_app(_context(test_db, tmp_path, home, registry)),
                        base_url=ORIGIN)
    _drop(inbox, "Secret glorp.md")

    response = _post(client, {"name": "Secret glorp.md", "enrich": False})
    registry.run_next()
    job = client.get(f"/api/jobs/{response.json()['id']}").json()

    assert job["state"] == "succeeded"
    assert "Secret glorp" not in str(job)


def test_the_same_file_dropped_again_is_already_in_your_brain(
    client: TestClient, registry: JobRegistry, inbox: Path, test_db: psycopg.Connection[Any]
) -> None:
    _drop(inbox, "twice.md")
    assert _run(client, registry, {"name": "twice.md", "enrich": False}).state == "succeeded"
    _drop(inbox, "twice.md")

    snap = _run(client, registry, {"name": "twice.md", "enrich": False})

    assert dict(snap.result or {})["outcome"] == "skipped"
    assert _tree(inbox) == {f"{IMPORTED_DIR}/twice-{_sha8(BODY)}.md": BODY}
    assert len(_documents(test_db)) == 1


def test_an_empty_file_stays_in_imported(
    client: TestClient, registry: JobRegistry, inbox: Path, test_db: psycopg.Connection[Any]
) -> None:
    blank = b"   \n\n"
    _drop(inbox, "blank.txt", blank)

    snap = _run(client, registry, {"name": "blank.txt", "enrich": False})

    assert dict(snap.result or {})["outcome"] == "empty"
    assert list(_tree(inbox)) == [f"{IMPORTED_DIR}/blank-{_sha8(blank)}.txt"]
    assert _documents(test_db) == []


# ------------------------------------------------------------ refusals (AC1) --


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"name": "sub/x.md"}, 400, "invalid_name"),
        ({"name": "../x.md"}, 400, "invalid_name"),
        ({"name": ".."}, 400, "invalid_name"),
        ({"name": "/etc/x.md"}, 400, "invalid_name"),
        ({"name": "x\x00.md"}, 400, "invalid_name"),
        ({"name": 7}, 400, "invalid_name"),
        ({"path": "x.md"}, 400, "unknown_field"),
        ({"name": "x.md", "force": True}, 400, "unknown_field"),
        ({"name": "picture.png"}, 415, "unsupported_file"),
        ({"name": "missing.md"}, 404, "inbox_file_unknown"),
        ({"name": "x.md", "sensitivity": "secret"}, 400, "invalid_sensitivity"),
    ],
)
def test_each_bad_request_is_refused_and_moves_nothing(
    client: TestClient, registry: JobRegistry, inbox: Path,
    payload: dict[str, Any], status: int, code: str,
) -> None:
    (inbox / "sub").mkdir()
    _drop(inbox / "sub", "x.md")
    _drop(inbox, "x.md")
    _drop(inbox, "picture.png")
    before = _tree(inbox)

    response = _post(client, payload)

    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert _tree(inbox) == before and registry.list_recent() == ()


def test_a_symlink_out_of_the_inbox_is_refused(
    client: TestClient, registry: JobRegistry, inbox: Path, tmp_path: Path
) -> None:
    outside = _drop(tmp_path, "elsewhere.md")
    (inbox / "link.md").symlink_to(outside)

    response = _post(client, {"name": "link.md"})

    assert (response.status_code, response.json()["error"]["code"]) == (
        400, "not_a_regular_file")
    assert outside.read_bytes() == BODY and registry.list_recent() == ()


@pytest.mark.parametrize("cycle", ["self", "pair"])
def test_a_symlink_loop_is_a_4xx_not_a_500(
    client: TestClient, registry: JobRegistry, inbox: Path, cycle: str
) -> None:
    """#110 rev F1: the POST takes names the listing never showed; a loop is one."""
    if cycle == "self":
        (inbox / "loop.md").symlink_to(inbox / "loop.md")
    else:
        (inbox / "loop.md").symlink_to(inbox / "b.md")
        (inbox / "b.md").symlink_to(inbox / "loop.md")

    response = _post(client, {"name": "loop.md"})

    assert (response.status_code, response.json()["error"]["code"]) == (
        400, "not_a_regular_file")
    assert registry.list_recent() == ()


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches any directory")
def test_an_inbox_that_cannot_be_searched_refuses_the_submit_with_409(
    client: TestClient, registry: JobRegistry, inbox: Path
) -> None:
    _drop(inbox, "x.md")
    inbox.chmod(0o600)
    try:
        response = _post(client, {"name": "x.md"})
    finally:
        inbox.chmod(0o700)

    assert (response.status_code, response.json()["error"]["code"]) == (
        409, "inbox_unreadable")
    assert registry.list_recent() == ()


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches any directory")
def test_an_inbox_that_stops_being_searchable_after_the_submit_is_its_own_job_error(
    client: TestClient, registry: JobRegistry, inbox: Path, test_db: psycopg.Connection[Any]
) -> None:
    _drop(inbox, "x.md")
    response = _post(client, {"name": "x.md", "enrich": False})
    assert response.status_code == 202
    inbox.chmod(0o600)
    try:
        assert registry.run_next()
    finally:
        inbox.chmod(0o700)
    snap = registry.get(response.json()["id"])

    assert snap.error is not None and snap.error.code == "inbox_unreadable"
    assert "may already have been saved" not in snap.error.message
    assert _tree(inbox) == {"x.md": BODY} and _documents(test_db) == []


def test_a_symlink_inside_the_inbox_is_refused(
    client: TestClient, registry: JobRegistry, inbox: Path
) -> None:
    _drop(inbox, "real.md")
    (inbox / "alias.md").symlink_to(inbox / "real.md")

    response = _post(client, {"name": "alias.md"})

    assert (response.status_code, response.json()["error"]["code"]) == (
        400, "not_a_regular_file")
    assert registry.list_recent() == ()


def test_a_server_off_loopback_refuses_the_submit(
    test_db: psycopg.Connection[Any], tmp_path: Path, home: Path, registry: JobRegistry,
    inbox: Path,
) -> None:
    _drop(inbox, "x.md")
    client = TestClient(create_app(_context(test_db, tmp_path, home, registry,
                                            ingest_enabled=False)), base_url=ORIGIN)

    response = _post(client, {"name": "x.md"})

    assert (response.status_code, response.json()["error"]["code"]) == (
        403, "ingest_requires_loopback")
    assert (inbox / "x.md").exists()


def test_no_inbox_refuses_the_submit(client: TestClient, home: Path) -> None:
    (home / "drop-folders.toml").unlink()

    response = _post(client, {"name": "x.md"})

    assert (response.status_code, response.json()["error"]["code"]) == (
        404, "inbox_unconfigured")


def test_a_bad_config_refuses_the_submit(client: TestClient, home: Path) -> None:
    (home / "drop-folders.toml").write_text("nope = 1\n", encoding="utf-8")

    response = _post(client, {"name": "x.md"})

    assert (response.status_code, response.json()["error"]["code"]) == (
        409, "inbox_misconfigured")


# ----------------------------------------------------- the swap (AC2, TOCTOU) --


def test_a_file_swapped_for_a_symlink_after_the_listing_is_refused(
    client: TestClient, registry: JobRegistry, inbox: Path, tmp_path: Path
) -> None:
    _drop(inbox, "swap.md")
    assert client.get("/api/ingest/inbox").json()["files"][0]["name"] == "swap.md"
    outside = _drop(tmp_path, "target.md", b"not the inbox's")
    (inbox / "swap.md").unlink()
    (inbox / "swap.md").symlink_to(inbox / ".." / "target.md")

    response = _post(client, {"name": "swap.md"})

    assert response.status_code == 400
    assert outside.read_bytes() == b"not the inbox's" and registry.list_recent() == ()


@pytest.mark.parametrize("swap", ["symlink", "loop", "fifo", "directory", "gone"])
def test_a_file_swapped_after_the_submit_is_refused_by_the_job(
    client: TestClient, registry: JobRegistry, inbox: Path, tmp_path: Path,
    test_db: psycopg.Connection[Any], swap: str,
) -> None:
    _drop(inbox, "late.md")
    outside = _drop(tmp_path, "target.md", b"not the inbox's")
    response = _post(client, {"name": "late.md", "enrich": False})
    assert response.status_code == 202
    (inbox / "late.md").unlink()
    if swap == "symlink":
        (inbox / "late.md").symlink_to(outside)
    elif swap == "loop":  # #110 rev F1: once a RuntimeError, i.e. internal_error
        (inbox / "late.md").symlink_to(inbox / "late.md")
    elif swap == "fifo":
        os.mkfifo(inbox / "late.md")
    elif swap == "directory":
        (inbox / "late.md").mkdir()

    assert registry.run_next()
    snap = registry.get(response.json()["id"])

    assert snap.state == "failed" and snap.error is not None
    assert snap.error.code == "inbox_file_changed"
    assert "late" not in snap.error.message
    assert "may already have been saved" not in snap.error.message
    assert outside.read_bytes() == b"not the inbox's"
    assert not (inbox / IMPORTED_DIR).exists()
    assert _documents(test_db) == []


# ------------------------------------------- every failure moves it back (D4) --


class _BusyEmbedder(FakeEmbedder):
    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        raise EmbedError("synthetic outage", transient=True)


class _CancellingEmbedder(FakeEmbedder):
    def __init__(self, registry: JobRegistry) -> None:
        super().__init__()
        self.registry = registry

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        (job,) = [j for j in self.registry.list_recent() if j.state == "running"]
        self.registry.cancel(job.id)
        return super().embed(texts, input_type)


@pytest.mark.parametrize(
    ("name", "body", "cfg", "code"),
    [
        ("keys.md", f"Deploy notes.\n\nkey {SYNTHETIC_AWS_KEY}\n".encode(),
         {"secret_guard": "reject"}, "secret_guard_rejected"),
        ("broken.pdf", b"%PDF-1.4 not really a pdf", {}, "unreadable_file"),
    ],
)
def test_a_failed_ingest_puts_the_file_back_in_the_inbox(
    test_db: psycopg.Connection[Any], tmp_path: Path, home: Path, registry: JobRegistry,
    inbox: Path, name: str, body: bytes, cfg: dict[str, Any], code: str,
) -> None:
    client = TestClient(create_app(_context(test_db, tmp_path, home, registry, cfg=cfg)),
                        base_url=ORIGIN)
    _drop(inbox, name, body)

    snap = _run(client, registry, {"name": name, "enrich": False})

    assert snap.state == "failed" and snap.error is not None and snap.error.code == code
    assert SYNTHETIC_AWS_KEY not in snap.error.message
    assert _tree(inbox) == {name: body}
    assert _documents(test_db) == []


def test_a_busy_embedder_puts_the_file_back(
    test_db: psycopg.Connection[Any], tmp_path: Path, home: Path, registry: JobRegistry,
    inbox: Path,
) -> None:
    ctx = _context(test_db, tmp_path, home, registry, embedder=_BusyEmbedder())
    client = TestClient(create_app(ctx), base_url=ORIGIN)
    _drop(inbox, "later.md")

    snap = _run(client, registry, {"name": "later.md", "enrich": False})

    assert snap.error is not None and snap.error.code == "embedder_busy"
    assert _tree(inbox) == {"later.md": BODY}


def test_a_cancel_mid_embed_puts_the_file_back(
    test_db: psycopg.Connection[Any], tmp_path: Path, home: Path, registry: JobRegistry,
    inbox: Path,
) -> None:
    ctx = _context(test_db, tmp_path, home, registry, embedder=_CancellingEmbedder(registry))
    client = TestClient(create_app(ctx), base_url=ORIGIN)
    _drop(inbox, "stop.md")

    snap = _run(client, registry, {"name": "stop.md", "enrich": False})

    assert snap.state == "cancelled"
    assert _tree(inbox) == {"stop.md": BODY}
    assert _documents(test_db) == []


# ------------------------------------- what run_one reports without an ingest --


def _spec(name: str = "x.md") -> InboxSpec:
    return InboxSpec(name=name, title=None, tags=(), sensitivity="normal", enrich=False)


def _run_with(ctx: UiContext, registry: JobRegistry, inbox: Path, run: Any) -> JobSnapshot:
    from brain.ui.inbox import find_inbox

    folder = find_inbox(ctx.cfg.brain_home)
    assert folder is not None and ctx.ingest_runtime is not None
    job = registry.submit(INBOX_KIND, inbox_runner(ctx, ctx.ingest_runtime, folder, _spec(),
                                                   run=run), on_error=inbox_failure)
    assert registry.run_next()
    return registry.get(job.id)


@pytest.mark.parametrize(
    ("outcome", "code", "says"),
    [
        (FileOutcome("x.md", "vanished"), "inbox_file_gone", "no longer"),
        (FileOutcome("x.md", "failed", "MoveRaceError"), "inbox_move_failed", "MoveRaceError"),
        (FileOutcome("x.md", "failed", "PermissionError[EPERM]"), "inbox_move_failed",
         "PermissionError[EPERM]"),
        (FileOutcome("x.md", "failed", "a different file holds x-0123abcd.md"),
         "inbox_move_failed", "it changed"),
    ],
)
def test_a_move_that_never_reached_the_ingest_is_named_by_class_only(
    ctx: UiContext, registry: JobRegistry, inbox: Path,
    outcome: FileOutcome, code: str, says: str,
) -> None:
    _drop(inbox, "x.md")

    snap = _run_with(ctx, registry, inbox, lambda *a, **k: outcome)

    assert snap.error is not None and snap.error.code == code
    assert says in snap.error.message and "x-0123abcd" not in snap.error.message


def test_an_unusable_imported_folder_is_its_own_error(
    ctx: UiContext, registry: JobRegistry, inbox: Path
) -> None:
    _drop(inbox, "x.md")

    def run(*a: Any, **k: Any) -> FileOutcome:
        raise DropFolderError("imported/ is not a real directory")

    snap = _run_with(ctx, registry, inbox, run)

    assert snap.error is not None and snap.error.code == "inbox_unusable"


def test_an_inbox_that_cannot_be_opened_is_a_class_only_failure(
    ctx: UiContext, registry: JobRegistry, inbox: Path
) -> None:
    _drop(inbox, "x.md")

    def run(*a: Any, **k: Any) -> FileOutcome:
        raise PermissionError(1, "Operation not permitted", str(inbox))

    snap = _run_with(ctx, registry, inbox, run)

    assert snap.error is not None and snap.error.code == "inbox_move_failed"
    assert str(inbox) not in snap.error.message
