"""``brain docembed backfill`` through the real CLI (RM-20).

The embedder is swapped at ``brain.cli._build_embedder`` (the ``patch_embedder``
fixture), so the command runs against the 5434 test database. Output is counts
only; a refusal or an embed failure exits 1 with a class-only line. All text is
synthetic.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.cli_registry import EXTENSION_REGISTRARS
from brain.embeddings import NullEmbedder, OllamaEmbedError
from tests.doc_leg_corpus import FIT_SUMMARY, FIT_TITLE, HostedWordEmbedder, reconcile, seed
from tests.saved_search_fakes import WordEmbedder

SECRET = "synthetic-cli-docembed-detail-3b7a"


class _Down(WordEmbedder):
    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        raise OllamaEmbedError(f"connection refused {SECRET}", transient=True)


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, ["docembed", *args])


@pytest.fixture
def word(test_db: psycopg.Connection[Any], patch_embedder: Any) -> WordEmbedder:
    embedder = WordEmbedder()
    reconcile(test_db, embedder)
    patch_embedder(embedder)
    return embedder


def test_the_command_is_registered_through_the_seam() -> None:
    assert "docembed" in [name for name, _ in EXTENSION_REGISTRARS]

    result = CliRunner().invoke(app, ["docembed", "--help"])

    assert result.exit_code == 0
    assert "backfill" in result.stdout


def test_backfill_reports_counts_and_no_text(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)

    result = _run("backfill")

    assert result.exit_code == 0, result.output
    assert f"embedded {len(ids)} documents" in result.stdout
    assert "0 documents still without a vector" in result.stdout
    assert FIT_TITLE not in result.output
    assert FIT_SUMMARY not in result.output


def test_limit_and_stale_are_accepted(
    test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    ids = seed(test_db, word)

    first = _run("backfill", "--limit", "1")
    second = _run("backfill", "--stale")

    assert "embedded 1 document\n" in first.stdout
    assert f"{len(ids) - 1} documents still without a vector" in first.stdout
    assert f"embedded {len(ids) - 1} documents" in second.stdout


def test_a_hosted_embedder_reports_the_withheld_documents(
    test_db: psycopg.Connection[Any], patch_embedder: Any
) -> None:
    hosted = HostedWordEmbedder()
    reconcile(test_db, hosted)
    seed(test_db, hosted, fit_sensitivity="confidential")
    patch_embedder(hosted)

    result = _run("backfill")

    assert result.exit_code == 0, result.output
    assert "1 confidential document skipped (hosted embedder" in result.stdout


def test_an_embedder_without_vectors_exits_1(
    test_db: psycopg.Connection[Any], patch_embedder: Any  # noqa: ARG001
) -> None:
    patch_embedder(NullEmbedder())

    result = _run("backfill")

    assert result.exit_code == 1
    assert "BRAIN_EMBEDDER=none" in result.output


def test_an_embed_failure_exits_1_class_only(
    test_db: psycopg.Connection[Any], patch_embedder: Any
) -> None:
    down = _Down()
    reconcile(test_db, down)
    seed(test_db, WordEmbedder())
    patch_embedder(down)

    result = _run("backfill")

    assert result.exit_code == 1
    assert "OllamaEmbedError" in result.output
    assert SECRET not in result.output
    assert "re-run to resume" in result.output


def test_a_bad_limit_is_a_usage_error() -> None:
    assert _run("backfill", "--limit", "0").exit_code == 2
