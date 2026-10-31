"""AC6, graph half (RM-43): an ingested ``code`` document skips concept extraction.

Ingests a synthetic repository through `brain ingest-code`, then runs
`brain graphrag build --backfill --concepts` twice against the AGE test
instance (port 5434) with an extractor that FAILS the test if it is called. The
second run reports the code documents under ``skipped``, not ``reconciled``, the
shape RM-30's calendar test pins (``tests/test_graphrag_concept_skip_policy.py``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from tests import code_repo_fixtures as fx
from tests.conftest import TEST_DATABASE_URL
from tests.test_graphrag_concept_skip_policy import _ExtractorThatMustNotRun


def test_backfill_skips_concepts_for_ingested_code_on_the_second_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: Any,
    patch_embedder: Any,
    test_db: psycopg.Connection[Any],
) -> None:
    patch_embedder(fake_embedder)
    repo = fx.build_repo(tmp_path)
    monkeypatch.setenv("HOME", str(fx.home_of(tmp_path)))
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(
        "brain.graph_rag.extract.make_extractor", lambda cfg: _ExtractorThatMustNotRun()
    )
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    ingested = CliRunner().invoke(
        app,
        ["ingest-code", str(repo), "--author-email", fx.OWNER_EMAIL, "--since", fx.SINCE],
    )
    assert ingested.exit_code == 0, ingested.output
    code_docs = test_db.execute(
        "SELECT count(*) FROM documents WHERE content_type = 'code'"
    ).fetchone()
    assert code_docs == (len(fx.OWNER_KEPT),)
    args = ["graphrag", "build", "--backfill", "--concepts"]

    first = CliRunner().invoke(app, args)
    second = CliRunner().invoke(app, args)

    n = len(fx.OWNER_KEPT)
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert f"{n} processed (reconciled 0, skipped {n})" in second.output
