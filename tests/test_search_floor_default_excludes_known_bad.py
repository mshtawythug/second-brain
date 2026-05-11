"""Phase D — empirical floor tuning against the live local corpus.

Per plan revision #3, ``DEFAULT_VECTOR_SIM_FLOOR`` is committed to
:mod:`brain.config` only after this test proves it sits ≥0.05 above
the highest cosine similarity that a known-bad doc scores against the
real ``person-x`` query embedding.

The test:

1. Loads the configured embedder (default ``arctic`` via Ollama).
2. Embeds ``person-x``.
3. Reads the stored chunk embeddings of the known-bad doc
   ``7aeb2167-febe-470a-a108-079f120bac29`` (``cheatsheet-numbers``)
   from the live local Postgres.
4. Computes cosine similarity for each chunk.
5. Asserts ``DEFAULT_VECTOR_SIM_FLOOR ≥ max + 0.05``.

Skipped automatically when the live DB or Ollama is unreachable —
this is a one-shot empirical tuner, not a CI gate.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import psycopg
import pytest

from brain import config as config_module
from brain.config import DEFAULT_VECTOR_SIM_FLOOR, Config
from brain.db import connect
from brain.embeddings import OllamaEmbedError, make_embedder

# The known-bad doc surfaced by the Phase 0 baseline as a top-5 false
# positive for ``person-x``. Doc title: ``cheatsheet-numbers``.
KNOWN_BAD_DOC_ID = "7aeb2167-febe-470a-a108-079f120bac29"
KNOWN_BAD_QUERY = "person-x"

LIVE_DB_URL = "postgresql://brain:brain@localhost:5433/second_brain"


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    assert len(a) == len(b), "vector lengths must match"
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Block .env file sources so delenv tests aren't undone by T1.0 setdefault."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: tmp_path / "project.env")
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)


@pytest.mark.live_db
def test_default_floor_excludes_known_bad_person-b_match(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """``DEFAULT_VECTOR_SIM_FLOOR`` must sit ≥0.05 above the cheatsheet doc's
    max cosine to the real ``person-x`` query embedding."""
    # Force the live DB URL — the session-wide fixture pins DATABASE_URL
    # to the test DB. Restoring via monkeypatch is safe; ``Config.load``
    # uses the env var directly.
    monkeypatch.setenv("DATABASE_URL", LIVE_DB_URL)
    # Ensure no test-leaked override of the floor reaches the loader.
    monkeypatch.delenv("BRAIN_VECTOR_SIM_FLOOR", raising=False)

    try:
        cfg = Config.load()
    except Exception as exc:  # noqa: BLE001 — broad on intent: any config
        pytest.skip(f"could not load config: {exc}")

    try:
        embedder = make_embedder(cfg)
    except Exception as exc:  # noqa: BLE001 — embedder constructors raise
        pytest.skip(f"embedder unavailable: {exc}")

    try:
        with connect(LIVE_DB_URL) as conn:
            doc_row = conn.execute(
                "SELECT id, title FROM documents WHERE id = %s",
                (KNOWN_BAD_DOC_ID,),
            ).fetchone()
            if doc_row is None:
                pytest.skip(
                    f"known-bad doc {KNOWN_BAD_DOC_ID} not present in live "
                    f"DB; skipping empirical floor check"
                )
            chunk_rows = conn.execute(
                "SELECT id::text, embedding::text FROM chunks "
                "WHERE document_id = %s AND embedding IS NOT NULL",
                (KNOWN_BAD_DOC_ID,),
            ).fetchall()
    except (psycopg.OperationalError, psycopg.errors.ConnectionTimeout) as exc:
        pytest.skip(f"live DB unreachable: {exc}")

    if not chunk_rows:
        pytest.skip(
            f"known-bad doc {KNOWN_BAD_DOC_ID} has no embedded chunks"
        )

    # Embed the query with the real backend.
    try:
        q_emb = embedder.embed([KNOWN_BAD_QUERY], input_type="query")[0]
    except (OllamaEmbedError, OSError) as exc:
        pytest.skip(f"embedder runtime error (Ollama unreachable?): {exc}")

    # Parse pgvector text → list[float]. Stored as e.g. `[0.1,0.2,...]`.
    cosines: list[float] = []
    for _, emb_text in chunk_rows:
        # pgvector ::text round-trip = "[v1,v2,...]"
        cleaned = emb_text.strip().lstrip("[").rstrip("]")
        chunk_emb = [float(x) for x in cleaned.split(",")]
        cosines.append(_cosine(q_emb, chunk_emb))

    max_bad = max(cosines)
    margin = 0.05
    required_floor = max_bad + margin

    # Surface the actual measurement so a future tweak shows up in the
    # test report (also useful when the floor is widened or narrowed).
    print(
        f"\n[empirical floor] doc={KNOWN_BAD_DOC_ID} chunks={len(cosines)} "
        f"max_cosine_to_query={max_bad:.4f} required≥{required_floor:.4f} "
        f"committed={DEFAULT_VECTOR_SIM_FLOOR}"
    )
    # Tee the measurement to a file too — handy for the audit doc.
    audit_log = os.environ.get("BRAIN_FLOOR_AUDIT_LOG")
    if audit_log:
        with open(audit_log, "a", encoding="utf-8") as fh:
            fh.write(
                f"max_cosine={max_bad:.4f} required≥{required_floor:.4f} "
                f"committed={DEFAULT_VECTOR_SIM_FLOOR}\n"
            )

    assert required_floor <= DEFAULT_VECTOR_SIM_FLOOR, (
        f"DEFAULT_VECTOR_SIM_FLOOR ({DEFAULT_VECTOR_SIM_FLOOR}) is not "
        f"high enough to exclude the known-bad doc {KNOWN_BAD_DOC_ID} — "
        f"max cosine to '{KNOWN_BAD_QUERY}' is {max_bad:.4f}, need "
        f"≥ {required_floor:.4f} (= max + 0.05). Update the default in "
        f"src/brain/config.py."
    )
