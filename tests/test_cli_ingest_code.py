"""`brain ingest-code` end to end (RM-43), through the CLI and the 5434 database.

Every repository is synthetic (``tests/code_repo_fixtures.py``), built in
``tmp_path`` with the real ``git``; ``HOME`` points inside ``tmp_path`` so no
test reads the owner's git config. Acceptance criteria AC1–AC5 and the first
half of AC6 are here; the graph half of AC6 is in
``tests/test_graphrag_code_concept_skip.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner, Result

from brain.cli import app
from brain.ingest import code_files as cf
from tests import code_repo_fixtures as fx
from tests.conftest import CountingEmbedder
from tests.secret_fixtures import SYNTHETIC_AWS_KEY


@pytest.fixture
def embedder(counting_embedder: CountingEmbedder, patch_embedder: Any) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    built = fx.build_repo(tmp_path)
    monkeypatch.setenv("HOME", str(fx.home_of(tmp_path)))
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    return built


@pytest.fixture
def enricher_builds(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every ``_build_enricher`` call; the run never builds one unless --enrich."""
    from tests.conftest import build_fake_enricher

    calls: list[object] = []

    def build(cfg: object) -> object:
        calls.append(cfg)
        return build_fake_enricher()

    monkeypatch.setattr("brain.cli._build_enricher", build)
    return calls


def _run(repo_dir: Path, *args: str, emails: tuple[str, ...] = (fx.OWNER_EMAIL,),
         since: str | None = fx.SINCE) -> Result:
    argv = ["ingest-code", str(repo_dir)]
    for email in emails:
        argv += ["--author-email", email]
    if since is not None:
        argv += ["--since", since]
    return CliRunner().invoke(app, [*argv, *args])


def _docs(conn: psycopg.Connection[Any]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.metadata->>'path', d.id::text, d.title, d.content, d.content_type,
               d.source_path, d.sensitivity, d.summary, d.metadata, s.kind
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE s.kind = 'code'
        """
    ).fetchall()
    keys = ("id", "title", "content", "content_type", "source_path", "sensitivity", "summary",
            "metadata", "kind")
    return {str(r[0]): dict(zip(keys, r[1:], strict=True)) for r in rows}


def _chunk_heads(conn: psycopg.Connection[Any], document_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT content FROM chunks WHERE document_id=%s ORDER BY chunk_index", (document_id,)
    ).fetchall()
    return [str(r[0]).split("\n", 1)[0] for r in rows]


# --- refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("emails", "since"), [((), fx.SINCE), ((fx.OWNER_EMAIL,), None), ((), None)]
)
def test_the_scope_flags_are_required(
    repo: Path, emails: tuple[str, ...], since: str | None, test_db: psycopg.Connection
) -> None:
    result = _run(repo, emails=emails, since=since)

    assert result.exit_code == 2
    assert "--author-email and --since are required" in result.output
    assert _docs(test_db) == {}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--since", "2026/06/01"), "--since must be YYYY-MM-DD"),
        (("--ext", "."), "--ext takes a file extension"),
    ],
)
def test_malformed_flags_are_refused(repo: Path, args: tuple[str, ...], message: str) -> None:
    result = _run(repo, *args, since=None if args[0] == "--since" else fx.SINCE)

    assert result.exit_code == 2
    assert message in result.output


def test_a_malformed_address_is_refused(repo: Path) -> None:
    result = _run(repo, emails=("<owner@example.test>",))

    assert result.exit_code == 2
    assert "--author-email takes one address" in result.output


def test_a_bad_sensitivity_is_refused(repo: Path) -> None:
    result = _run(repo, "--sensitivity", "secretish")

    # The shared up-front check ``ingest-dir`` uses, which exits 1 before anything is read.
    assert result.exit_code == 1
    assert "secretish" in result.output
    assert "found" not in result.output


def test_a_directory_outside_any_work_tree_exits_1(tmp_path: Path, embedder: Any) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    result = _run(plain)

    assert result.exit_code == 1
    assert "ingest-code: not inside a git work tree" in result.output


def test_a_missing_git_exits_1_with_the_install_hint(
    repo: Path, embedder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("brain.ingest.code_repo.shutil.which", lambda _name: None)

    result = _run(repo)

    assert result.exit_code == 1
    assert "git not found on PATH" in result.output


def test_an_unreachable_database_exits_1_by_class(
    repo: Path, embedder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://brain:brain@localhost:1/nowhere")

    result = _run(repo)

    assert result.exit_code == 1
    assert "ingest-code: database unavailable (OperationalError)" in result.output


def test_a_repository_with_no_commit_yet_reports_zero_files(
    tmp_path: Path, embedder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rev-113: it used to exit 1 with "`git log` failed (exit 128)"."""
    home = tmp_path / "home"
    home.mkdir()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    fx.git(fresh, "init", "-q", home=home)
    fx.write(fresh, "a.py", "x = 1\n")
    fx.git(fresh, "add", "a.py", home=home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))

    result = _run(fresh)

    assert result.exit_code == 0, result.output
    assert f"found 0 file(s) you committed since {fx.SINCE}" in result.output


# --- AC1–AC4 -----------------------------------------------------------------------------


def test_only_the_owners_files_are_ingested_and_every_exclusion_holds(
    repo: Path, embedder: Any, enricher_builds: list[object], test_db: psycopg.Connection
) -> None:
    result = _run(repo)

    assert result.exit_code == 0, result.output
    docs = _docs(test_db)
    # AC1 + AC2: exactly the owner's kept files, nothing excluded, nobody else's.
    assert set(docs) == set(fx.OWNER_KEPT)
    for reason in (cf.SKIP_VENDORED, cf.SKIP_LOCKFILE, cf.SKIP_MINIFIED, cf.SKIP_NOT_REGULAR,
                   cf.SKIP_TOO_LARGE, cf.SKIP_BINARY, cf.SKIP_EMPTY):
        assert f"skipped: {cf.SKIP_TEXT[reason]}" in result.output, reason
    assert "3 ingested, 0 updated, 0 unchanged, 1 refused, 0 failed" in result.output
    widget = docs["src/widget.py"]
    assert widget["title"] == "sample-repo/src/widget.py"
    assert widget["content_type"] == "code"
    assert widget["source_path"] == str(repo.resolve() / "src/widget.py")
    assert widget["metadata"] == {"path": "src/widget.py", "repo": "sample-repo",
                                  "language": "python"}
    # AC4: confidential by default.
    assert {d["sensitivity"] for d in docs.values()} == {"confidential"}
    # RM-42 chunks it by definition.
    assert _chunk_heads(test_db, widget["id"]) == [
        "src/widget.py:1-2 <module>",
        "src/widget.py:5-6 widget_path",
        "src/widget.py:9-11 Gadget",
    ]
    # AC6, first half: no summary requested by default.
    assert enricher_builds == []
    assert {d["summary"] for d in docs.values()} == {None}


def test_the_planted_key_is_refused_even_when_the_guard_is_set_to_warn(
    repo: Path, embedder: Any, test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: reject mode is forced; BRAIN_SECRET_GUARD cannot weaken it."""
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "warn")

    result = _run(repo)

    assert result.exit_code == 0, result.output
    assert fx.SECRET_PATH not in _docs(test_db)
    assert f"refused {fx.SECRET_PATH}: aws_access_key_id at line 1" in result.output
    assert "1 file(s) NOT ingested: each holds a credential-shaped string" in result.output
    assert SYNTHETIC_AWS_KEY not in result.output
    assert test_db.execute(
        "SELECT count(*) FROM documents WHERE source_path LIKE %s", (f"%{fx.SECRET_PATH}",)
    ).fetchone() == (0,)


def test_sensitivity_normal_overrides_the_default(
    repo: Path, embedder: Any, test_db: psycopg.Connection
) -> None:
    result = _run(repo, "--sensitivity", "normal")

    assert result.exit_code == 0, result.output
    assert {d["sensitivity"] for d in _docs(test_db).values()} == {"normal"}


def test_ext_and_a_subdirectory_narrow_the_run(
    repo: Path, embedder: Any, test_db: psycopg.Connection
) -> None:
    result = _run(repo / "web", "--ext", ".TS")

    assert result.exit_code == 0, result.output
    assert set(_docs(test_db)) == {"web/app.ts"}
    assert "skipped: outside --ext" in result.output


def test_a_repeated_address_is_used_once_and_tags_apply(
    repo: Path, embedder: Any, test_db: psycopg.Connection
) -> None:
    result = _run(repo, "--tag", "Side Project", emails=(fx.OWNER_EMAIL, fx.OWNER_EMAIL))

    assert result.exit_code == 0, result.output
    tags = test_db.execute(
        "SELECT DISTINCT unnest(tags) FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE s.kind = 'code'"
    ).fetchall()
    assert tags == [("side-project",)]


# --- AC5: re-run ------------------------------------------------------------------------


def test_a_rerun_is_unchanged_and_an_edit_updates_in_place(
    repo: Path, embedder: Any, test_db: psycopg.Connection
) -> None:
    first = _run(repo)
    before = _docs(test_db)

    second = _run(repo)
    (repo / "src/widget.py").write_text(fx.PY_MODULE + "\n\ndef added():\n    return 3\n")
    third = _run(repo)

    assert first.exit_code == second.exit_code == third.exit_code == 0
    assert "0 ingested, 0 updated, 3 unchanged" in second.output
    assert "0 ingested, 1 updated, 2 unchanged" in third.output
    assert "  updated src/widget.py" in third.output
    after = _docs(test_db)
    assert after["src/widget.py"]["id"] == before["src/widget.py"]["id"]
    assert "def added()" in after["src/widget.py"]["content"]
    assert _chunk_heads(test_db, after["src/widget.py"]["id"])[-1] == "src/widget.py:14-15 added"


# --- AC6, first half: the cap and the summary opt-in -------------------------------------


def test_max_files_caps_the_writes_and_reports_the_remainder(
    repo: Path, embedder: Any, test_db: psycopg.Connection
) -> None:
    capped = _run(repo, "--max-files", "2")

    assert capped.exit_code == 0, capped.output
    assert len(_docs(test_db)) == 2
    assert "stopped at --max-files 2: 1 file(s) remain; run again to continue" in capped.output

    rest = _run(repo, "--max-files", "2")

    assert len(_docs(test_db)) == 3
    assert "1 ingested, 0 updated, 2 unchanged" in rest.output
    assert "remain" not in rest.output


def test_past_the_cap_a_stored_unchanged_file_is_not_counted_as_remaining(
    repo: Path, embedder: Any, test_db: psycopg.Connection
) -> None:
    _run(repo)
    (repo / "README.md").write_text(fx.README + "\nMore.\n")

    capped = _run(repo, "--max-files", "1")

    # README sorts first, so it is the one write; the two after it are unchanged.
    assert "0 ingested, 1 updated, 2 unchanged" in capped.output
    assert "remain" not in capped.output


def test_enrich_opts_in_to_the_summary(
    repo: Path, embedder: Any, enricher_builds: list[object]
) -> None:
    result = _run(repo, "--enrich")

    assert result.exit_code == 0, result.output
    assert len(enricher_builds) == 1


def test_a_per_file_failure_is_counted_by_class_and_the_run_goes_on(
    repo: Path, embedder: Any, test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brain.ingest import ingest_document as real

    def flaky(conn: Any, **kwargs: Any) -> Any:
        if kwargs["doc"].metadata["path"] == "README.md":
            raise ValueError("synthetic failure text")
        return real(conn, **kwargs)

    monkeypatch.setattr("brain.cli_ingest_code.ingest_document", flaky)

    result = _run(repo)

    assert result.exit_code == 0, result.output
    assert "  failed README.md: ValueError" in result.output
    assert "synthetic failure text" not in result.output
    assert "2 ingested, 0 updated, 0 unchanged, 1 refused, 1 failed" in result.output


def test_a_repository_with_nothing_of_the_owners_writes_nothing(
    repo: Path, embedder: Any, test_db: psycopg.Connection
) -> None:
    result = _run(repo, emails=("nobody@example.test",))

    assert result.exit_code == 0, result.output
    assert "found 0 file(s) you committed since 2026-06-01" in result.output
    assert _docs(test_db) == {}


def test_a_path_with_control_characters_is_printed_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder: Any
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "r"
    repo.mkdir()
    fx.git(repo, "init", "-q", home=home)
    fx.commit(repo, {"evil\x1b[31m.py": "x = 1\n"}, home=home, author=fx.owner())
    monkeypatch.setenv("HOME", str(home))

    result = _run(repo)

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output
    assert "  ingested evil[31m.py" in result.output


def test_past_the_cap_a_file_with_a_key_is_refused_not_remaining(
    repo: Path, embedder: Any, test_db: psycopg.Connection
) -> None:
    """Otherwise the run would report it as left to write on every run, forever."""
    result = _run(repo, "--max-files", "1")

    # README is the one write; settings.py (the key) comes after the cap.
    assert f"refused {fx.SECRET_PATH}: aws_access_key_id at line 1" in result.output
    assert "1 ingested, 0 updated, 0 unchanged, 1 refused, 0 failed" in result.output
    assert "stopped at --max-files 1: 2 file(s) remain" in result.output
