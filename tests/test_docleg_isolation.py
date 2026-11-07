"""A ``BRAIN_DOC_LEG`` exported in the shell never reaches a test (RM-20).

``tests/docleg_isolation_plugin.py``'s autouse ``_doc_leg_off`` (registered in
``tests/conftest.py``) sets ``BRAIN_DOC_LEG=false`` and clears the weight for
every test. The probe checks that from inside a test, through the real
``run_search(stages=None)`` path; the regression test runs the probe in a CHILD
pytest whose environment exports the leg and its weight. Both are ``nodb``: the
child opens no database and takes no suite lock. All values are synthetic.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from brain.search import SearchResult
from brain.search_pipeline import run_search

pytestmark = pytest.mark.nodb

REPO = Path(__file__).resolve().parents[1]
PROBE = "tests/test_docleg_isolation.py::test_the_doc_leg_is_off_inside_every_test"

EXPORTED = {"BRAIN_DOC_LEG": "true", "BRAIN_DOC_LEG_WEIGHT": "3"}


class _Embedder:
    dim = 4

    def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]

    def count_tokens(self, text: str) -> int:
        return len(text.split())


def _search(conn: Any, *, limit: int, **_: Any) -> list[SearchResult]:
    return [
        SearchResult(
            document_id=f"doc-{n}",
            title=f"Probe {n}",
            source_kind="manual",
            snippet="s",
            score=1.0,
            content_type="note",
            tags=[],
        )
        for n in range(min(limit, 3))
    ]


def test_the_doc_leg_is_off_inside_every_test() -> None:
    assert os.environ.get("BRAIN_DOC_LEG") == "false"
    assert "BRAIN_DOC_LEG_WEIGHT" not in os.environ

    out = run_search(_search, None, embedder=_Embedder(), query="q", limit=3)  # type: ignore[arg-type]

    assert out.stages == ()


def test_an_exported_doc_leg_does_not_reach_a_test() -> None:
    env = {**os.environ, **EXPORTED, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", PROBE,
            "-q", "--no-cov", "-p", "no:cacheprovider", "-m", "nodb",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "1 passed" in result.stdout


def test_every_test_runs_inside_the_doc_leg_guard(request: pytest.FixtureRequest) -> None:
    """The plugin is wired into THIS suite through ``tests/conftest.py``'s ``pytest_plugins``."""
    assert "_doc_leg_off" in request.fixturenames
