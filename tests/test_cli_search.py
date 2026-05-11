"""Tests for the `brain search` CLI command."""
import os
from datetime import datetime
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.errors import PersonAmbiguous, PersonNotFound
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import PersonMatch

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="COMPANY_REDACTED Notes",
            content="COMPANY_REDACTED was a great gig",
            content_type="txt",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )


def test_search_returns_results(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "company-id"])
    assert result.exit_code == 0, result.output
    assert "COMPANY_REDACTED Notes" in result.output


def test_search_json_output(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "company-id", "--json"])
    assert result.exit_code == 0, result.output
    # Rich's print_json may emit pretty-printed JSON across lines; relax:
    assert "COMPANY_REDACTED Notes" in result.stdout


def test_search_no_results_message(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _setup(monkeypatch, test_db, fake_embedder)
    # Use --fts-only so the fake embedder's vector leg (which always matches the
    # single ingested doc as nearest neighbor) doesn't produce results.
    result = CliRunner().invoke(
        app, ["search", "nonexistent-unique-term-xyz", "--fts-only"]
    )
    assert result.exit_code == 0, result.output
    assert "(no results)" in result.output


# ---------------------------------------------------------------------------
# Q1-C metadata filter flags — patch hybrid_search to capture kwargs.
# ---------------------------------------------------------------------------


def _spy_hybrid_search(captured: dict[str, Any]) -> Any:
    """Build a spy that records kwargs + returns no results."""

    def _spy(*_args: Any, **kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    return _spy


def _install_search_spy(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> dict[str, Any]:
    _setup(monkeypatch, test_db, fake_embedder)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("brain.cli.hybrid_search", _spy_hybrid_search(captured))
    return captured


def test_brain_search_after_threads_to_hybrid_search(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "foo", "--after", "2026-01-01"])
    assert result.exit_code == 0, result.output
    assert captured["after"] == datetime(2026, 1, 1)


def test_brain_search_before_threads_to_hybrid_search(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "foo", "--before", "2026-05-01"])
    assert result.exit_code == 0, result.output
    assert captured["before"] == datetime(2026, 5, 1)


def test_brain_search_bad_after_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "foo", "--after", "notadate"])
    assert result.exit_code != 0


def test_brain_search_has_tag_aliases_tag(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "foo", "--has-tag", "interview"])
    assert result.exit_code == 0, result.output
    assert captured["tag"] == "interview"


def test_brain_search_has_tag_and_tag_conflict_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(
        app, ["search", "foo", "--tag", "a", "--has-tag", "b"]
    )
    assert result.exit_code != 0
    # Typer surfaces BadParameter with the offending message.
    combined = (result.output or "") + (result.stderr or "")
    assert "tag" in combined and "has-tag" in combined


def test_brain_search_has_tag_and_tag_same_value_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(
        app, ["search", "foo", "--tag", "x", "--has-tag", "x"]
    )
    assert result.exit_code == 0, result.output
    assert captured["tag"] == "x"


def test_brain_search_without_tag_threads_through(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(
        app, ["search", "foo", "--without-tag", "private"]
    )
    assert result.exit_code == 0, result.output
    assert captured["without_tag"] == "private"


def test_brain_search_draft_flag_maps_to_true(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "foo", "--draft"])
    assert result.exit_code == 0, result.output
    assert captured["draft"] is True


def test_brain_search_no_draft_flag_maps_to_false(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "foo", "--no-draft"])
    assert result.exit_code == 0, result.output
    assert captured["draft"] is False


def test_brain_search_draft_absent_maps_to_none(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "foo"])
    assert result.exit_code == 0, result.output
    assert captured.get("draft") is None


def test_brain_search_kind_threads_to_content_type(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "foo", "--kind", "email"])
    assert result.exit_code == 0, result.output
    assert captured["content_type"] == "email"


def test_brain_search_thread_threads_to_thread_id(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    result = CliRunner().invoke(app, ["search", "foo", "--thread", "abc123"])
    assert result.exit_code == 0, result.output
    assert captured["thread_id"] == "abc123"


def test_brain_search_person_threads_resolved_keys(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    captured = _install_search_spy(monkeypatch, test_db, fake_embedder)
    monkeypatch.setattr(
        "brain.cli.resolve_person_to_keys",
        lambda _conn, _name: PersonMatch(
            display_name="Alice Doe", keys=["alice@x.com", "alice doe"]
        ),
    )
    result = CliRunner().invoke(app, ["search", "foo", "--person", "Alice"])
    assert result.exit_code == 0, result.output
    assert captured["person_keys"] == ["alice@x.com", "alice doe"]
    assert captured["person_display_name"] == "Alice Doe"


def test_brain_search_person_not_found_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _install_search_spy(monkeypatch, test_db, fake_embedder)

    def _raise(_conn: object, name: str) -> Any:
        raise PersonNotFound(name)

    monkeypatch.setattr("brain.cli.resolve_person_to_keys", _raise)
    result = CliRunner().invoke(app, ["search", "foo", "--person", "Nobody"])
    assert result.exit_code != 0


def test_brain_search_person_ambiguous_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _install_search_spy(monkeypatch, test_db, fake_embedder)

    def _raise(_conn: object, name: str) -> Any:
        raise PersonAmbiguous(name, ["Alice Doe", "Alice Xanthus"])

    monkeypatch.setattr("brain.cli.resolve_person_to_keys", _raise)
    result = CliRunner().invoke(app, ["search", "foo", "--person", "Alice"])
    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    # Surface the candidate list so the user can disambiguate.
    assert "Alice Doe" in combined or "Alice Xanthus" in combined
