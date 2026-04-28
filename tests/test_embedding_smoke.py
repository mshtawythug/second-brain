"""Tests for ``scripts/embedding_smoke.py``.

The script lives outside ``src/brain``, so it isn't auto-importable. We load
it via :mod:`importlib.util` and invoke ``main(argv)`` directly with a fake
embedder swapped in via monkeypatch — no subprocess, no Ollama / Voyage HTTP.
"""
import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "embedding_smoke.py"

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


@pytest.fixture
def smoke_module() -> ModuleType:
    """Load ``scripts/embedding_smoke.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("embedding_smoke", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install(
    monkeypatch: pytest.MonkeyPatch,
    smoke_module: ModuleType,
    embedder: object,
) -> None:
    """Wire DATABASE_URL + swap the script's embedder factory for a fake."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(smoke_module, "_build_embedder", lambda cfg: embedder)


def test_smoke_runs_against_seeded_db(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    seed_doc: Callable[..., str],
    smoke_module: ModuleType,
    tmp_path: Path,
) -> None:
    seed_doc(
        title="Q1 review with person-x",
        content="person-x mentioned the Q1 numbers came in above forecast.",
    )
    _install(monkeypatch, smoke_module, fake_embedder)
    queries_file = tmp_path / "queries.txt"
    queries_file.write_text("person-x Q1\n")

    rc = smoke_module.main(["--queries-file", str(queries_file)])
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "Q1 review with person-x" in captured.out
    assert '[query 1/1] "person-x Q1"' in captured.out


def test_smoke_empty_queries_file_exits_zero(
    capsys: pytest.CaptureFixture[str],
    smoke_module: ModuleType,
    tmp_path: Path,
) -> None:
    queries_file = tmp_path / "queries.txt"
    queries_file.write_text("")

    rc = smoke_module.main(["--queries-file", str(queries_file)])
    captured = capsys.readouterr()

    assert rc == 0
    assert "no queries" in captured.out
    assert str(queries_file) in captured.out


def test_smoke_only_comments_exits_zero(
    capsys: pytest.CaptureFixture[str],
    smoke_module: ModuleType,
    tmp_path: Path,
) -> None:
    queries_file = tmp_path / "queries.txt"
    queries_file.write_text("# just a comment\n\n   # indented comment\n")

    rc = smoke_module.main(["--queries-file", str(queries_file)])
    captured = capsys.readouterr()

    assert rc == 0
    assert "no queries" in captured.out


def test_smoke_missing_queries_file_exits_one(
    capsys: pytest.CaptureFixture[str],
    smoke_module: ModuleType,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does_not_exist.txt"

    rc = smoke_module.main(["--queries-file", str(missing)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "queries file not found" in captured.err
    assert str(missing) in captured.err


def test_smoke_json_output_is_valid_jsonl(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    seed_doc: Callable[..., str],
    smoke_module: ModuleType,
    tmp_path: Path,
) -> None:
    seed_doc(title="Doc Alpha", content="alpha bravo charlie discussion")
    seed_doc(title="Doc Delta", content="delta echo foxtrot conversation")
    _install(monkeypatch, smoke_module, fake_embedder)
    queries_file = tmp_path / "q.txt"
    queries_file.write_text("alpha\ndelta\n")

    rc = smoke_module.main(["--queries-file", str(queries_file), "--json"])
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 2

    payloads = [json.loads(line) for line in lines]
    assert [p["query"] for p in payloads] == ["alpha", "delta"]
    for p in payloads:
        assert isinstance(p["results"], list)
        # FTS leg should match the seeded doc on each query.
        assert p["results"], f"expected at least one result for query {p['query']!r}"
        first = p["results"][0]
        assert {"id", "title", "snippet", "score", "source_kind", "tags"}.issubset(first)


def test_smoke_skips_comments_and_blanks(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    seed_doc: Callable[..., str],
    smoke_module: ModuleType,
    tmp_path: Path,
) -> None:
    seed_doc(title="Greetings", content="hello world how are you")
    _install(monkeypatch, smoke_module, fake_embedder)
    queries_file = tmp_path / "q.txt"
    queries_file.write_text(
        "# top-level comment\n"
        "\n"
        "hello\n"
        "  # indented comment with leading spaces\n"
        "\n"
        "world\n"
    )

    rc = smoke_module.main(
        ["--queries-file", str(queries_file), "--json"]
    )
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 2
    queries = [json.loads(line)["query"] for line in lines]
    assert queries == ["hello", "world"]


def test_smoke_human_no_results(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    smoke_module: ModuleType,
    tmp_path: Path,
) -> None:
    """Empty DB → human output reports ``(no results)`` per query, exits 0."""
    _install(monkeypatch, smoke_module, fake_embedder)
    queries_file = tmp_path / "q.txt"
    queries_file.write_text("nothing-here-xyz\n")

    rc = smoke_module.main(["--queries-file", str(queries_file)])
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "(no results)" in captured.out
