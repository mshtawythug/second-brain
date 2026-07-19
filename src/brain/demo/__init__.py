"""`brain demo` — zero-Ollama taste test over a synthetic Larkspur corpus.

Orchestration core for the demo experience: load the packaged synthetic
corpus, provision an isolated throwaway Postgres (stock ``pgvector`` image, a
Docker named volume, NEVER the prod container), seed it deterministically with
:class:`~brain.demo.embedder.DemoEmbedder` (no Ollama), run FTS-only hybrid
search, and tear the whole thing down.

The isolation contract is binding (see the module constants): a dedicated
compose project / container / database / named volume, and a hard guard
(:func:`_assert_not_demo_prod_db`) that refuses to operate against the prod
database on port 55432 / db ``second_brain``. The Typer surface lives in
:mod:`brain.cli_demo`; this module owns the provision / seed / query / status /
teardown primitives it calls.
"""
from __future__ import annotations

import importlib.resources
import json
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from brain.db import connect, ensure_embedding_column, run_migrations
from brain.errors import BrainError
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import finalize_embedding_index
from brain.search import SearchResult, hybrid_search

from .embedder import DemoEmbedder

# --- Isolation contract (binding) ------------------------------------------
# A dedicated compose project + container + database + Docker NAMED VOLUME, on
# the stock pgvector image (NOT the custom AGE prod image). Nothing here may
# ever resolve to the prod container (port 55432, db ``second_brain``,
# ./data/postgres bind-mount) — the demo is a throwaway sandbox.
COMPOSE_PROJECT = "brain-demo"
CONTAINER_NAME = "second-brain-demo-postgres"
DEMO_DB_NAME = "second_brain_demo"
DEMO_IMAGE = "pgvector/pgvector:pg16"
DEMO_VOLUME = "brain-demo-pgdata"
DEMO_DB_USER = "brain"
DEMO_DB_PASSWORD = "brain"  # noqa: S105 — throwaway local sandbox credential
DEFAULT_DEMO_PORT = 55433

# Where the generated compose file is materialized (a throwaway dir under the
# user's home — never the repo, never $BRAIN_HOME prod state).
DEMO_HOME = Path.home() / ".brain-demo"
COMPOSE_FILENAME = "docker-compose.yml"

# Prod-safety guard values — mirror ``tests/conftest.py``'s ``_assert_not_prod_db``.
_PROD_PORTS = frozenset({5433, 55432})
_PROD_DB_NAME = "second_brain"
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})

# How long to wait for the freshly-provisioned container to accept connections.
_PROVISION_READY_TIMEOUT_S = 30.0
_PROVISION_POLL_INTERVAL_S = 0.5
_SUBPROCESS_TIMEOUT_S = 120


@dataclass(frozen=True)
class SeedReport:
    """Outcome of :func:`seed_demo`.

    ``ingested`` counts newly-created documents, ``skipped`` counts content-hash
    /source dedup no-ops (a re-seed reports every doc skipped). ``total`` is the
    corpus size — ``ingested + skipped`` on a clean seed.
    """

    ingested: int
    skipped: int
    total: int


@dataclass(frozen=True)
class DemoStatus:
    """Snapshot of the demo sandbox for ``brain demo status``."""

    running: bool
    container: str
    database_url: str | None
    doc_count: int | None


def load_corpus() -> list[dict[str, Any]]:
    """Load the packaged synthetic corpus manifest (22 docs).

    Resolved via :func:`importlib.resources.files` so it works in both editable
    checkouts and pipx/wheel installs. Returns the raw record list exactly as
    authored in ``corpus/manifest.json``.
    """
    resource = importlib.resources.files("brain.demo") / "corpus" / "manifest.json"
    text = resource.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, list):
        raise BrainError("demo corpus manifest must be a JSON list of records")
    return data


def demo_database_url(port: int) -> str:
    """Return the Postgres URL for the demo container at ``port``."""
    return (
        f"postgresql://{DEMO_DB_USER}:{DEMO_DB_PASSWORD}"
        f"@localhost:{port}/{DEMO_DB_NAME}"
    )


def _assert_not_demo_prod_db(database_url: str) -> None:
    """Refuse to operate against the prod database.

    Copied in spirit from ``tests/conftest.py``'s ``_assert_not_prod_db``:
    aborts when the resolved (host, port, dbname) looks like the prod container
    — any known prod port (55432 current, 5433 historical) on a local host, OR
    the exact prod database name on any host. Every demo primitive that opens a
    connection calls this first so a mis-pointed ``--database-url`` can never
    seed / query / drop prod data.
    """
    parsed = urlparse(database_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    dbname = (parsed.path or "").lstrip("/")
    is_local = host in _LOCAL_HOSTS
    if (is_local and port in _PROD_PORTS) or (dbname == _PROD_DB_NAME):
        raise BrainError(
            "REFUSING to run `brain demo` against what looks like the PROD "
            f"database (host={host!r} port={port!r} db={dbname!r}). The demo "
            "must only ever touch its own throwaway container "
            f"(db {DEMO_DB_NAME!r}, named volume {DEMO_VOLUME!r}). Fix "
            "--database-url so it does not point at prod."
        )


def _record_to_doc(record: dict[str, Any]) -> ExtractedDoc:
    """Project a manifest record onto an :class:`ExtractedDoc` for ingest.

    Promotable metadata (``date`` → ``sent_at``, ``participants``,
    ``duration_min``, ``thread_id``) rides in ``metadata`` and is picked up by
    the ingest pipeline's column-promotion. ``None`` / empty values are omitted
    so promotion skips them cleanly. Demo docs are never file-backed, so
    ``source_path`` stays ``None`` (dedup is by ``(source, external_id)``).
    """
    metadata: dict[str, Any] = {"date": record["date"]}
    participants = record.get("participants") or []
    if participants:
        metadata["participants"] = list(participants)
    duration = record.get("duration_min")
    if duration is not None:
        metadata["duration_min"] = duration
    thread_id = record.get("thread_id")
    if thread_id:
        metadata["thread_id"] = thread_id
    return ExtractedDoc(
        title=record["title"],
        content=record["body"],
        content_type=record["content_type"],
        source_path=None,
        metadata=metadata,
    )


def seed_demo(database_url: str, *, with_embeddings: bool = False) -> SeedReport:
    """Ingest the full synthetic corpus into ``database_url``.

    Reconciles ``chunks.embedding`` to :class:`DemoEmbedder`'s dim first (so the
    demo runs regardless of the schema's shipped default dim), then ingests all
    22 docs with the deterministic embedder — no Ollama, no enrichment, no graph
    sync, no vault mirror. Idempotent: a re-seed dedups on ``(source,
    external_id)`` and reports every doc skipped.

    ``with_embeddings=True`` additionally finalizes the embedding column (NOT
    NULL + HNSW cosine index) so the vector leg of hybrid search is exercised;
    the default leaves the column unindexed since the demo query path is
    FTS-only.
    """
    _assert_not_demo_prod_db(database_url)
    embedder = DemoEmbedder()
    corpus = load_corpus()
    ingested = 0
    skipped = 0
    with connect(database_url) as conn:
        conn.autocommit = True
        ensure_embedding_column(conn, embedder)
        for record in corpus:
            result = ingest_document(
                conn,
                embedder=embedder,
                doc=_record_to_doc(record),
                source_kind=record["source"],
                source_external_id=record["external_id"],
                tags=list(record.get("tags") or []),
                enrich=False,
                enricher=None,
                vault_root=None,
                graph_syncer=None,
            )
            if result.created:
                ingested += 1
            else:
                skipped += 1
        if with_embeddings:
            finalize_embedding_index(conn, embedder)
    return SeedReport(ingested=ingested, skipped=skipped, total=len(corpus))


def query_demo(
    database_url: str,
    query: str,
    *,
    limit: int = 5,
    source: str | None = None,
    tag: str | None = None,
    person: str | None = None,
    after: datetime | None = None,
    with_embeddings: bool = False,
) -> list[SearchResult]:
    """Run hybrid search over the seeded demo corpus.

    FTS-only by default (``with_embeddings=False`` → ``fts_only=True``): no
    query embedding, fully deterministic, zero Ollama. ``person`` is resolved
    directly to a lowercased participant key (the demo has no directory layer),
    matching the case-insensitive participant overlap the search SQL applies.
    """
    _assert_not_demo_prod_db(database_url)
    embedder = DemoEmbedder()
    person_keys = [person.strip().lower()] if person else None
    with connect(database_url) as conn:
        conn.autocommit = True
        return hybrid_search(
            conn,
            embedder=embedder,
            query=query,
            limit=limit,
            source_kind=source,
            tag=tag,
            fts_only=not with_embeddings,
            person_keys=person_keys,
            person_display_name=person,
            after=after,
        )


# --- Docker / provisioning primitives --------------------------------------
# All Docker access funnels through :func:`_run` so tests can mock the single
# subprocess boundary (CLAUDE.md rule 13: subprocess-boundary mocks are fine).


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, capturing output. The single mocked Docker boundary."""
    return subprocess.run(
        args,
        check=check,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )


def _compose_file_text(port: int) -> str:
    """Render the minimal demo compose file (stock image, named volume)."""
    return (
        "services:\n"
        "  postgres:\n"
        f"    image: {DEMO_IMAGE}\n"
        f"    container_name: {CONTAINER_NAME}\n"
        "    environment:\n"
        f"      POSTGRES_USER: {DEMO_DB_USER}\n"
        f"      POSTGRES_PASSWORD: {DEMO_DB_PASSWORD}\n"
        f"      POSTGRES_DB: {DEMO_DB_NAME}\n"
        "    ports:\n"
        f'      - "{port}:5432"\n'
        "    volumes:\n"
        f"      - {DEMO_VOLUME}:/var/lib/postgresql/data\n"
        "volumes:\n"
        f"  {DEMO_VOLUME}:\n"
    )


def _write_compose_file(port: int) -> Path:
    """Materialize the compose file under :data:`DEMO_HOME`; return its path."""
    DEMO_HOME.mkdir(parents=True, exist_ok=True)
    path = DEMO_HOME / COMPOSE_FILENAME
    path.write_text(_compose_file_text(port), encoding="utf-8")
    return path


def _port_is_free(port: int) -> bool:
    """True iff ``port`` can be bound on localhost (nothing else listening)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def resolve_port(start: int, *, attempts: int = 20) -> int:
    """Return ``start`` or the next free port above it (auto-bump on collision)."""
    for candidate in range(start, start + attempts):
        if _port_is_free(candidate):
            return candidate
    raise BrainError(
        f"no free port found in [{start}, {start + attempts}) for the demo container"
    )


def _wait_until_ready(database_url: str) -> None:
    """Poll the provisioned container until it accepts connections (≤30s)."""
    deadline = time.monotonic() + _PROVISION_READY_TIMEOUT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with connect(database_url) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001 — retry any connection failure
            last_error = exc
            time.sleep(_PROVISION_POLL_INTERVAL_S)
    raise BrainError(
        f"demo Postgres did not become ready within "
        f"{_PROVISION_READY_TIMEOUT_S:.0f}s: {last_error}"
    )


def provision(port: int) -> str:
    """Bring up the isolated demo Postgres and migrate it; return its URL.

    Writes the minimal compose file, ``docker compose -p brain-demo up -d`` on
    the stock pgvector image + named volume, waits for readiness, then applies
    the relational migrations and reconciles ``chunks.embedding`` to the demo
    embedder's dim. Graph/AGE is never bootstrapped — the stock image has no
    AGE, and the demo query path never needs it.
    """
    database_url = demo_database_url(port)
    _assert_not_demo_prod_db(database_url)
    _write_compose_file(port)
    _run(
        [
            "docker", "compose",
            "-p", COMPOSE_PROJECT,
            "-f", str(DEMO_HOME / COMPOSE_FILENAME),
            "up", "-d",
        ]
    )
    _wait_until_ready(database_url)
    with connect(database_url) as conn:
        conn.autocommit = True
        run_migrations(conn)
        ensure_embedding_column(conn, DemoEmbedder())
    return database_url


def _container_running() -> bool:
    """True iff the demo container is up (``docker ps`` names it)."""
    try:
        result = _run(
            [
                "docker", "ps",
                "--filter", f"name={CONTAINER_NAME}",
                "--format", "{{.Names}}",
            ],
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return CONTAINER_NAME in result.stdout.split()


def _count_documents(database_url: str) -> int | None:
    """Return the demo corpus doc count, or ``None`` if the DB is unreachable."""
    try:
        with connect(database_url) as conn:
            row = conn.execute("SELECT count(*) FROM documents").fetchone()
        return int(row[0]) if row is not None else None
    except Exception:  # noqa: BLE001 — status is best-effort; unreachable → None
        return None


def status(port: int = DEFAULT_DEMO_PORT) -> DemoStatus:
    """Report whether the demo sandbox is running + its doc count."""
    running = _container_running()
    url = demo_database_url(port) if running else None
    doc_count = _count_documents(url) if url is not None else None
    return DemoStatus(
        running=running,
        container=CONTAINER_NAME,
        database_url=url,
        doc_count=doc_count,
    )


def teardown() -> None:
    """Destroy the demo sandbox: ``docker compose -p brain-demo down -v``.

    ``down -v`` removes the named volume too, so nothing of the demo survives.
    Only ever targets the ``brain-demo`` compose project — never prod (a
    separate project on a bind-mount that ``down -v`` cannot reach anyway).
    """
    compose_file = DEMO_HOME / COMPOSE_FILENAME
    args = ["docker", "compose", "-p", COMPOSE_PROJECT]
    if compose_file.is_file():
        args += ["-f", str(compose_file)]
    args += ["down", "-v"]
    _run(args, check=False)
