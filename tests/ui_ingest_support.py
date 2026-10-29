"""Shared fixtures for the Ingest tab's integration and route tests (RM-10).

Every value here is synthetic. The registry is driven with ``run_next()`` on the
test thread, so no test starts the worker unless it says so, and the connection
factory yields the suite's own ``test_db`` connection.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from brain.config import Config
from brain.ingest.chunker import chunk_text
from brain.ui.context import UiContext
from brain.ui.ingest_service import IngestRuntime
from brain.ui.jobs import JobRegistry
from brain.vault import init_vault

ORIGIN = "http://127.0.0.1:8765"
WRITE_HEADERS = {"Origin": ORIGIN}


def make_registry(**overrides: Any) -> JobRegistry:
    kwargs: dict[str, Any] = {
        "clock": lambda: datetime.now(UTC),
        "max_queued": 4,
        "retain": 50,
        "retain_for": timedelta(hours=1),
    }
    kwargs.update(overrides)
    return JobRegistry(**kwargs)


def make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    init_vault(vault)
    return vault


def make_context(
    conn: psycopg.Connection[Any],
    vault: Path,
    *,
    embedder: Any,
    enricher: Any | None = None,
    jobs: JobRegistry | None = None,
    ingest_enabled: bool = True,
    read_only: bool = False,
    token: str = "",
    cfg_overrides: dict[str, Any] | None = None,
    **context_overrides: Any,
) -> UiContext:
    """A loopback, writable context whose job runtime uses ``embedder``."""

    @contextlib.contextmanager
    def conn_factory() -> Iterator[psycopg.Connection[Any]]:
        yield conn

    cfg = Config(
        database_url="postgresql://unused/in/these/tests",
        vault_path=vault,
        embedder="none",
        **(cfg_overrides or {}),
    )
    return UiContext(
        cfg=cfg,
        conn_factory=conn_factory,
        embedder=embedder,
        search_fn=lambda *a, **k: [],
        allowed_origin=ORIGIN,
        logging_enabled=False,
        read_only=read_only,
        token=token,
        jobs=jobs if jobs is not None else make_registry(),
        ingest_runtime=IngestRuntime(
            make_embedder=lambda: embedder, make_enricher=lambda: enricher
        ),
        ingest_enabled=ingest_enabled,
        **context_overrides,
    )


def body_with_chunks(n: int, count_tokens: Any, *, marker: str = "para") -> str:
    """A synthetic body that chunks into exactly ``n`` chunks.

    Each paragraph is ~500 tokens under a 1-token-per-4-chars counter, so no two
    fit one 600-token chunk. The count is ASSERTED, never assumed (Ingest spec
    §9.2), because a chunker change would otherwise make the cancel cases test a
    different batch boundary than they claim.
    """
    paragraphs = [
        f"{marker} {i}: " + " ".join(f"word{i}x{j}" for j in range(200)) for i in range(n)
    ]
    body = "\n\n".join(paragraphs)
    assert len(chunk_text(body, count_tokens=count_tokens)) == n
    return body
