"""GraphRAG P95 performance gate — BLOCKING (wave G2-k; ``-m benchmark``).

This is the gate spec §16 / §17b Q5 mandate: load the **full-scale** synthetic
graph (**50k entities / 500k CO_OCCURS / 1M mentions / 10 tenants**, seed 1234)
into the Apache AGE test instance ONCE per session, then measure steady-state
retrieval latency and assert two budgets:

* **P95 local traversal ≤ 750 ms** (``graph_rag_search`` mode=local).
* **P95 themes-with-X ≤ 2 s** (``graph_rag_search`` mode=themes, person=…).

Corpus **generation time is excluded** from the latency measurement (timed
separately and budgeted at ≤ 20 min). The whole job is budgeted at ≤ 30 min in
CI. Warm-up calls are run-but-discarded so the measurement reflects steady state.

**Excluded from the default suite** by the ``benchmark`` marker (pyproject
``addopts = -m 'not eval and not benchmark'``) — the load is far too heavy for
every local ``pytest`` run. Opt in with ``pytest -m benchmark --no-cov``. A
dedicated CI workflow (``.github/workflows/benchmark.yml``) runs it against the
pinned AGE Docker image.

**Faithful-to-production measurement.** The gate exercises the real
``AgeBackend`` retrieval path with the production property indexes the backend's
``bootstrap`` creates (``tenant_id`` / ``entity_uuid`` / ``canonical_key`` /
``CO_OCCURS.weight`` — spec §5b). It deliberately adds NO extra indexes and does
not tune memory: the point is to measure what production AGE actually does at
scale. The ONLY config deviation from the defaults is a higher ``frontier_cap``
(the synthetic per-person scope at this corpus density sits at the default-200
safe-bound guard; raising the cap measures the *complete* traversal/scope rather
than a guard short-circuit — the latency itself is cap-insensitive here).

**OOM resilience.** Each latency test uses its OWN connection (opened with a
short connect-retry) rather than a shared one, so if a path that is non-viable at
scale takes the AGE backend down (a backend crash restarts *all* server
processes), the remaining tests still reconnect and report. A backend crash /
dropped connection during a measurement is itself a clear gate FAIL for that path
(it cannot meet its latency budget if it cannot complete at all).

All data is synthetic (``Person {i}`` / ``person-bench-t{N}-{i}``); no PII.

If the gate FAILS, that is the signal for the ``GraphBackend`` kill-switch
decision (spec §16) — a coordinator call, not a thing this test remediates.
"""
from __future__ import annotations

import contextlib
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import psycopg
import pytest

from brain.config import Config
from brain.db import connect_age
from brain.errors import GraphBackendError
from brain.graph_rag import (
    FUSE_MODE,
    GLOBAL_MODE,
    LOCAL_MODE,
    THEMES_MODE,
    build_communities,
    graph_rag_search,
    summarize_communities,
)
from brain.graph_rag.backends import AgeBackend
from brain.vault.derived_links.directory import DirectoryStore
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder, _reset_schema_and_migrate
from tests.graphrag.benchmark_fixture import (
    BENCHMARK_SPEC_FULL,
    BenchmarkSpec,
    _split,
    _uuid,
    generate_benchmark_graph,
)
from tests.graphrag.benchmark_harness import LatencyStats, measure

pytestmark = pytest.mark.benchmark

# --- Budgets (spec §16 / §17.3 / §17b Q5 / §17c Q8) ------------------------- #
_LOCAL_P95_BUDGET_MS = 750.0
_THEMES_P95_BUDGET_MS = 2_000.0
# G3 budgets (spec §17c Q8): global query P95 ≤ 1000 ms; community build (Louvain
# detection + relational persistence, NO summary pass) ≤ 5 min on the full corpus.
_GLOBAL_P95_BUDGET_MS = 1_000.0
_COMMUNITY_BUILD_BUDGET_S = 5 * 60  # 5 minutes
_GENERATION_BUDGET_S = 20 * 60  # 20 minutes
# G4 budget (spec §17d dec 4): fuse query P95 ≤ 1500 ms — local traversal's 750 ms
# plus a bounded hybrid-leg allowance, measured END-TO-END incl. the query
# embedding. The ONE new assertion this wave adds to the gate.
_FUSE_P95_BUDGET_MS = 1_500.0

# --- Measurement plan ------------------------------------------------------- #
# Sample sizes comfortably above the ≥50-100 floor; warm-up calls are discarded
# (measured = total - warmup). Distinct seeds/persons per call avoid cache bias.
_LOCAL_TOTAL = 120
_LOCAL_WARMUP = 20  # → 100 measured
_THEMES_TOTAL = 70
_THEMES_WARMUP = 10  # → 60 measured
# Global (community) retrieval plan — same warm-up-discard discipline as local.
_GLOBAL_TOTAL = 120
_GLOBAL_WARMUP = 20  # → 100 measured
# Fuse (graph ⊕ hybrid) retrieval plan — same warm-up-discard discipline.
_FUSE_TOTAL = 120
_FUSE_WARMUP = 20  # → 100 measured
_QUERY_SEED = 1234  # query SELECTION determinism (matches the corpus seed)

# ``graph_communities.summary_embedding`` ships as vector(1024) (migration 013),
# so a 1024-dim FakeEmbedder writes community embeddings at the migration dim
# (no resize) and embeds the query at the same dim — mirrors the G3-f CLI tests.
_SUMMARY_DIM = 1024

# Fuse is gated to tenant='default' (spec §17d dec 6), but the benchmark corpus
# tenants are bench-t{0..9} — so the fuse gate measures over a dedicated
# 'default'-tenant slice APPENDED on top of the loaded corpus (the bulk loader
# resumes graphids, so the append never collides). The slice matches ONE
# bench-tenant's density (5k entities / 50k CO_OCCURS / 100k mentions) so the
# fuse GRAPH leg's traversal cost equals the measured local path; plus 2k
# documents each carrying one chunk so the HYBRID leg does real work —
# ``hybrid_search`` runs its FTS query + a full sequential cosine scan over the
# 2k-chunk default corpus (chunks are NOT tenantized, so the scan is corpus-wide;
# 4096-dim with no HNSW index, the conservative qwen3-shaped path).
_FUSE_DEFAULT_SPEC = BenchmarkSpec(
    entities=5_000,
    cooccur_edges=50_000,
    mentions=100_000,
    tenants=1,
    documents=2_000,
)
# chunks.embedding ships as vector(4096) in the migrated test schema (migration
# 002 re-adds it at 4096; no resize runs in tests), so the fuse hybrid leg uses a
# 4096-dim FakeEmbedder for both the seeded chunks and the query embedding.
_FUSE_CHUNK_DIM = 4096

# Free-text queries the global gate cycles over. They share tokens with the fake
# community summaries (so the FTS leg matches) while the vector leg always runs a
# full cosine scan over the tenant's ``summary_embedding`` set — the path whose
# steady-state latency the P95 budget guards.
_GLOBAL_QUERIES = (
    "collaboration project themes",
    "recurring discussion topics",
    "community summary covering people",
    "project work and collaboration",
    "themes across the discussion documents",
)

# Caps: production depth/min-edge-weight; frontier_cap raised so the safe-bound
# guard never short-circuits a valid full-scope traversal (see module docstring).
_BENCH_FRONTIER_CAP = 1_000

# A generous per-statement timeout — only to keep a pathological hang from
# blowing the 30-min job; it does NOT mask a fast OOM. A cancelled statement
# surfaces as a clean error the measurement treats as a budget FAIL.
_STATEMENT_TIMEOUT = "120s"


@dataclass(frozen=True)
class _BenchmarkCorpus:
    """The session-loaded corpus descriptor (no live connection — see fixtures)."""

    cfg: Config
    gen_seconds: float
    entities_per_tenant: list[int]
    docs_per_tenant: list[int]
    # (tenant_id, query/person string) plans — directory is pre-seeded for themes.
    local_plan: list[tuple[str, str]]
    themes_plan: list[tuple[str, str]]


@dataclass(frozen=True)
class _CommunityCorpus:
    """The session-built community layer over :class:`_BenchmarkCorpus` (G3-g).

    ``build_seconds`` is the MEASURED wall-clock of community detection +
    persistence across all tenants (the §17c Q8 community-build budget). The
    summary/embedding seeding that follows is setup for the global gate and is
    excluded from it. ``global_plan`` is the (tenant, free-text query) list the
    global P95 measurement runs over.
    """

    cfg: Config
    build_seconds: float
    communities_total: int
    summarized_total: int
    embedded_total: int
    global_plan: list[tuple[str, str]]


@dataclass(frozen=True)
class _FuseCorpus:
    """The 'default'-tenant fuse corpus appended over :class:`_BenchmarkCorpus` (G4).

    ``mode=fuse`` is gated to tenant='default' (spec §17d dec 6), so the gate
    cannot reuse the bench-t{0..9} corpus directly — it appends a dedicated
    'default' slice (:data:`_FUSE_DEFAULT_SPEC`) + a per-document chunk corpus.
    ``gen_seconds`` is that slice's (excluded-from-the-budget) load wall-clock,
    ``chunk_count`` the seeded hybrid-leg chunks, and ``fuse_plan`` the
    ``person-default-{i}`` seed queries the P95 measurement runs over (each
    resolves a default-tenant graph seed; its shared person/default tokens drive
    the hybrid FTS leg).
    """

    cfg: Config
    gen_seconds: float
    chunk_count: int
    fuse_plan: list[str]


def _make_cfg() -> Config:
    """Production graph defaults, with a benchmark-safe ``frontier_cap``."""
    return Config(
        database_url=TEST_DATABASE_URL,
        graph_tenant_id="default",  # overridden per-call via the ``tenant`` arg
        graph_depth=2,  # DEFAULT_GRAPH_DEPTH
        graph_frontier_cap=_BENCH_FRONTIER_CAP,
        graph_min_edge_weight=0.20,  # DEFAULT_GRAPH_MIN_EDGE_WEIGHT
        graph_generic_df_ratio=0.30,  # DEFAULT_GRAPH_GENERIC_DF_RATIO
        graph_theme_limit=5,  # DEFAULT_GRAPH_THEME_LIMIT
    )


def _build_plans(
    entities_per_tenant: list[int],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Pick distinct (tenant, person) seeds across all tenants, RNG-seeded.

    Both plans target the synthetic person entities by their canonical key
    ``person-bench-t{ti}-{idx}`` — an EXACT seed match for local, and (after the
    directory is seeded with the same display name) a resolvable ``--person`` for
    themes. Spreading across all 10 tenants keeps every query tenant-scoped to a
    single tenant's subgraph (realistic per-query cost; no cross-tenant work).
    """
    rng = random.Random(_QUERY_SEED)
    tenants = len(entities_per_tenant)

    def _distinct(count: int) -> list[tuple[str, str]]:
        seen: set[tuple[int, int]] = set()
        plan: list[tuple[str, str]] = []
        while len(plan) < count:
            ti = rng.randrange(tenants)
            idx = rng.randrange(entities_per_tenant[ti])
            if (ti, idx) in seen:
                continue
            seen.add((ti, idx))
            plan.append((f"bench-t{ti}", f"person-bench-t{ti}-{idx}"))
        return plan

    return _distinct(_LOCAL_TOTAL), _distinct(_THEMES_TOTAL)


class _BenchEnricher:
    """Deterministic, Ollama-free community summarizer for the global gate.

    Conforms to ``brain.graph_rag.communities_summary._CommunitySummarizer``
    (``model`` property + ``summarize_group``). Returns a stable summary that
    embeds the leading member names plus fixed theme tokens, so both the FTS leg
    (``summary_tsv``) and the vector leg (the FakeEmbedder over the summary text)
    have real, distinct content to rank — no live model needed.
    """

    model = "bench-fake:1b"

    def summarize_group(
        self,
        *,
        person: str | None,
        entity_names: list[str],
        doc_titles: list[str],
    ) -> str:
        head = ", ".join(entity_names[:5])
        return (
            f"Community summary covering {head}; recurring collaboration and "
            "project themes across the discussion documents."
        )


def _build_global_plan(tenants: int) -> list[tuple[str, str]]:
    """Pick (tenant, query) pairs spread across all tenants, deterministically.

    Cycles tenants (``k % tenants``) and queries (``k % len(queries)``) so the
    plan touches every tenant's community subgraph with a mix of free-text
    queries — each query stays tenant-scoped (realistic per-query global cost).
    """
    return [
        (f"bench-t{k % tenants}", _GLOBAL_QUERIES[k % len(_GLOBAL_QUERIES)])
        for k in range(_GLOBAL_TOTAL)
    ]


def _build_fuse_plan(entities: int, total: int) -> list[str]:
    """Pick distinct ``person-default-{idx}`` seed queries, RNG-seeded.

    Each query EXACTLY matches a default-tenant entity's ``canonical_key``
    (``person-default-{i}``) so the graph leg resolves a seed and traverses; the
    shared ``person`` / ``default`` tokens also match every seeded chunk so the
    hybrid FTS leg returns docs. Distinct indices avoid per-seed cache bias.
    """
    rng = random.Random(_QUERY_SEED)
    seen: set[int] = set()
    plan: list[str] = []
    while len(plan) < total:
        idx = rng.randrange(entities)
        if idx in seen:
            continue
        seen.add(idx)
        plan.append(f"person-default-{idx}")
    return plan


def _seed_default_chunks(
    conn: psycopg.Connection, embedder: FakeEmbedder, documents: int, *, seed: int
) -> int:
    """Insert one chunk per default-tenant document (the fuse hybrid-leg corpus).

    Content shares the ``person`` / ``default`` / ``benchmark`` tokens with the
    ``person-default-{i}`` fuse queries so the FTS arm matches; the deterministic
    FakeEmbedder vector feeds the vector arm's cosine scan. Document UUIDs mirror
    the generator's deterministic ``_uuid(seed, 'default', 'doc', i)`` so the
    chunks attach to the documents ``generate_benchmark_graph`` already wrote.
    """
    rows = [
        (
            str(_uuid(seed, "default", "doc", i)),
            0,
            f"person default benchmark synthetic body document {i}",
            embedder.embed(
                [f"person default benchmark synthetic body document {i}"],
                input_type="document",
            )[0],
        )
        for i in range(documents)
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
            "VALUES (%s, %s, %s, %s)",
            rows,
        )
    return len(rows)


def _open_bench_conn() -> Iterator[psycopg.Connection]:
    """Open a fresh autocommit AGE connection, riding out a backend restart.

    A non-viable-at-scale path can OOM-kill the AGE backend, which restarts ALL
    server processes; the next test must be able to reconnect. We retry the
    *connect* (never the test body) for a short window, then set a generous
    statement timeout so a pathological hang can't blow the job budget.
    """
    last: Exception | None = None
    for _ in range(20):
        cm = connect_age(TEST_DATABASE_URL)
        try:
            conn = cm.__enter__()
        except psycopg.OperationalError as exc:  # backend still restarting
            last = exc
            time.sleep(1.0)
            continue
        conn.autocommit = True
        conn.execute(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'")
        try:
            yield conn
        finally:
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)
        return
    raise AssertionError(f"could not connect to the AGE test DB after retries: {last!r}")


@pytest.fixture
def bench_conn() -> Iterator[psycopg.Connection]:
    """Function-scoped fresh AGE connection (survives a prior test's backend crash)."""
    yield from _open_bench_conn()


@pytest.fixture(scope="session")
def benchmark_corpus(
    _ensure_test_db_initialized: None,
) -> _BenchmarkCorpus:
    """Load BENCHMARK_SPEC_FULL into the AGE test DB ONCE; time generation.

    Self-contained + order-independent: resets the schema + AGE graph to a clean
    state, then bulk-loads the full corpus via the fast direct-COPY path
    (``age_bulk=True``) so generation fits the ≤20-min budget. Generation is
    timed in isolation (excluded from the P95 measurement). The themes target
    persons are seeded into ``directory_entries`` so ``resolve_person_to_keys``
    resolves them (the synthetic graph itself carries no directory rows). The
    loader connection is closed before the tests run — they reconnect per-test,
    so a backend crash in one test never poisons another.

    Depends on ``_ensure_test_db_initialized`` purely for session ordering; it
    does its OWN reset so a prior aborted run in the named test volume can't leak.
    """
    spec = BENCHMARK_SPEC_FULL
    entities_per_tenant = _split(spec.entities, spec.tenants)
    docs_per_tenant = _split(
        spec.documents if spec.documents is not None else spec.entities, spec.tenants
    )
    local_plan, themes_plan = _build_plans(entities_per_tenant)

    with connect_age(TEST_DATABASE_URL) as conn:
        conn.autocommit = True
        _reset_schema_and_migrate(conn)

        backend = AgeBackend()
        start = time.perf_counter()
        result = generate_benchmark_graph(
            conn, spec, backend=backend, materialize_age=True, age_bulk=True
        )
        gen_seconds = time.perf_counter() - start

        # Planner stats for the relational source-of-truth tables (the bulk
        # loader already ANALYZEd the AGE backing tables). Setup, not generation
        # — excluded from gen_seconds and from the measured P95.
        conn.execute("ANALYZE")

        # Verify the loaded sizes match the spec headline (50k/500k/1M/10).
        assert result.entities == spec.entities
        assert result.relationships == spec.cooccur_edges
        assert result.mentions == spec.mentions
        assert result.tenants == spec.tenants

        # Seed the directory so each themes person resolves to its graph entity.
        store = DirectoryStore(conn)
        for _tenant, person in themes_plan:
            store.upsert_pair(
                display_name=person,
                email=f"{person}@bench.invalid",
                source="gmail",
            )

    # Connection closed; the corpus persists in the DB. Tests reconnect per-test.
    return _BenchmarkCorpus(
        cfg=_make_cfg(),
        gen_seconds=gen_seconds,
        entities_per_tenant=entities_per_tenant,
        docs_per_tenant=docs_per_tenant,
        local_plan=local_plan,
        themes_plan=themes_plan,
    )


@pytest.fixture(scope="session")
def community_corpus(benchmark_corpus: _BenchmarkCorpus) -> _CommunityCorpus:
    """Build + summarize communities over the full corpus ONCE (G3-g, §17c Q8).

    Two phases, mirroring how generation is excluded from the latency budgets:

    * **Measured** — :func:`build_communities` (Louvain detection + relational
      persistence, NO summary pass) for all 10 tenants, timed in isolation. This
      wall-clock is the §17c Q8 community-build budget (≤ 5 min).
    * **Setup (not measured)** — :func:`summarize_communities` with a
      deterministic FAKE enricher + a 1024-dim FakeEmbedder (no live Ollama) so
      every community gets a ``summary`` + ``summary_embedding``, giving the
      global P95 gate both an FTS leg and a vector leg to rank over. Excluded
      from the build budget exactly as corpus generation is excluded from the
      retrieval budgets.

    ``force=True`` builds unconditionally (the dirty gate is irrelevant on a
    fresh corpus). The loader connection is autocommit (matching
    ``benchmark_corpus``); ``build_communities`` brackets its own transaction.
    A targeted ``ANALYZE`` of the two community tables gives the planner real
    stats without re-analyzing the (already-analyzed) base corpus — so the
    local/themes plans are untouched.
    """
    corpus = benchmark_corpus
    tenants = [f"bench-t{ti}" for ti in range(BENCHMARK_SPEC_FULL.tenants)]
    enricher = _BenchEnricher()
    embedder = FakeEmbedder(dim=_SUMMARY_DIM)

    with connect_age(TEST_DATABASE_URL) as conn:
        conn.autocommit = True

        # --- Measured: community build (Louvain + relational writes only). ---
        start = time.perf_counter()
        communities_total = 0
        for tenant in tenants:
            result = build_communities(conn, corpus.cfg, tenant=tenant, force=True)
            communities_total += result.communities_total
        build_seconds = time.perf_counter() - start

        # --- Setup (NOT timed): summaries + embeddings for the global gate. ---
        summarized_total = 0
        embedded_total = 0
        for tenant in tenants:
            summary = summarize_communities(
                conn, corpus.cfg, tenant=tenant, enricher=enricher, embedder=embedder
            )
            summarized_total += summary.summarized
            embedded_total += summary.embedded

        # Targeted planner stats over only the freshly-written community tables.
        conn.execute("ANALYZE graph_communities")
        conn.execute("ANALYZE graph_community_members")

    return _CommunityCorpus(
        cfg=corpus.cfg,
        build_seconds=build_seconds,
        communities_total=communities_total,
        summarized_total=summarized_total,
        embedded_total=embedded_total,
        global_plan=_build_global_plan(BENCHMARK_SPEC_FULL.tenants),
    )


@pytest.fixture(scope="session")
def fuse_corpus(benchmark_corpus: _BenchmarkCorpus) -> _FuseCorpus:
    """Append a 'default'-tenant graph + chunked docs for the fuse P95 gate (G4-c).

    Depends on (and builds ON TOP OF) :func:`benchmark_corpus` — it does NOT
    reset, so the 10-tenant corpus the local/themes/global gates measure stays
    untouched (fuse is gated to tenant='default', a tenant the base corpus does
    not use). It appends two things, both timed in isolation (EXCLUDED from the
    P95 budget, exactly as corpus generation is excluded from the other gates):

    * a single ``"default"``-tenant graph slice (:data:`_FUSE_DEFAULT_SPEC`, one
      bench-tenant's density) via the fast bulk path — the bulk loader resumes
      graphids from the live id-sequences, so the append never collides; and
    * one chunk per default document (deterministic 4096-dim FakeEmbedder, no
      live Ollama) so the hybrid leg (``hybrid_search``) has a real FTS + vector
      corpus to scan.

    A targeted ANALYZE gives the planner stats for the freshly-written rows
    (leaving the already-analyzed base corpus untouched). The loader connection
    is closed before the gate runs — it reconnects per-test (``bench_conn``).
    """
    spec = _FUSE_DEFAULT_SPEC
    documents = spec.documents
    assert documents is not None  # _FUSE_DEFAULT_SPEC sets it explicitly
    embedder = FakeEmbedder(dim=_FUSE_CHUNK_DIM)

    with connect_age(TEST_DATABASE_URL) as conn:
        conn.autocommit = True

        start = time.perf_counter()
        backend = AgeBackend()
        result = generate_benchmark_graph(
            conn,
            spec,
            backend=backend,
            materialize_age=True,
            age_bulk=True,
            tenant_names=["default"],
        )
        chunk_count = _seed_default_chunks(conn, embedder, documents, seed=spec.seed)
        gen_seconds = time.perf_counter() - start

        # Targeted planner stats over only the freshly-written rows.
        conn.execute("ANALYZE chunks")
        conn.execute("ANALYZE graph_entities")
        conn.execute("ANALYZE graph_entity_mentions")
        conn.execute("ANALYZE graph_relationships")

    # The append loaded exactly one default tenant's slice.
    assert result.entities == spec.entities
    assert result.mentions == spec.mentions
    assert chunk_count == documents

    return _FuseCorpus(
        cfg=_make_cfg(),
        gen_seconds=gen_seconds,
        chunk_count=chunk_count,
        fuse_plan=_build_fuse_plan(spec.entities, _FUSE_TOTAL),
    )


# --------------------------------------------------------------------------- #
# Generation budget (≤ 20 min) — a precondition for the latency gates.
# --------------------------------------------------------------------------- #
def test_generation_within_budget(
    benchmark_corpus: _BenchmarkCorpus, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = BENCHMARK_SPEC_FULL
    gen = benchmark_corpus.gen_seconds
    with capsys.disabled():
        print(
            f"\n[benchmark] corpus generation: {gen:.1f}s "
            f"({gen / 60:.2f} min) for {spec.entities} entities / "
            f"{spec.cooccur_edges} CO_OCCURS / {spec.mentions} mentions / "
            f"{spec.tenants} tenants (budget {_GENERATION_BUDGET_S / 60:.0f} min)"
        )
    assert gen <= _GENERATION_BUDGET_S, (
        f"corpus generation took {gen / 60:.2f} min, over the "
        f"{_GENERATION_BUDGET_S / 60:.0f} min budget"
    )


# --------------------------------------------------------------------------- #
# Gate 1 — P95 local traversal ≤ 750 ms.
# --------------------------------------------------------------------------- #
def test_p95_local_traversal_budget(
    benchmark_corpus: _BenchmarkCorpus,
    bench_conn: psycopg.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = benchmark_corpus
    backend = AgeBackend()

    def _call(tenant: str, query: str) -> Callable[[], object]:
        return lambda: graph_rag_search(
            bench_conn, corpus.cfg, query, backend=backend, tenant=tenant, mode=LOCAL_MODE
        )

    calls = [_call(tenant, query) for tenant, query in corpus.local_plan]
    stats = measure(calls, warmup=_LOCAL_WARMUP)
    _report(capsys, "LOCAL traversal", stats, _LOCAL_P95_BUDGET_MS)

    assert stats.p95_ms <= _LOCAL_P95_BUDGET_MS, (
        f"P95 local traversal {stats.p95_ms:.1f}ms exceeds the "
        f"{_LOCAL_P95_BUDGET_MS:.0f}ms budget "
        f"(p50={stats.p50_ms:.1f} p99={stats.p99_ms:.1f} max={stats.max_ms:.1f}, "
        f"n={stats.samples}) — AGE local traversal is over budget at 50k/500k/1M."
    )


# --------------------------------------------------------------------------- #
# Gate 2 — P95 themes-with-X ≤ 2 s.
# --------------------------------------------------------------------------- #
def test_p95_themes_with_x_budget(
    benchmark_corpus: _BenchmarkCorpus,
    bench_conn: psycopg.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = benchmark_corpus
    backend = AgeBackend()

    def _call(tenant: str, person: str) -> Callable[[], object]:
        return lambda: graph_rag_search(
            bench_conn, corpus.cfg, "", backend=backend, tenant=tenant,
            mode=THEMES_MODE, person=person,
        )

    calls = [_call(tenant, person) for tenant, person in corpus.themes_plan]
    try:
        stats = measure(calls, warmup=_THEMES_WARMUP)
    except (psycopg.OperationalError, GraphBackendError) as exc:
        # A dropped/killed backend (e.g. OOM) or a backend cap-failure mid-run
        # means themes cannot complete — a hard FAIL of the 2s budget.
        with capsys.disabled():
            print(
                f"\n[benchmark] THEMES-with-X: did NOT complete a measurement run "
                f"(backend error {type(exc).__name__}) → FAIL (budget P95 ≤ "
                f"{_THEMES_P95_BUDGET_MS:.0f}ms)"
            )
        pytest.fail(
            "P95 themes-with-X is NON-VIABLE on AGE at full scale: scope_person "
            "did not complete a single measured query before the backend dropped "
            f"the connection (cannot meet the {_THEMES_P95_BUDGET_MS:.0f}ms "
            f"budget). Underlying error: {exc!r}"
        )

    _report(capsys, "THEMES-with-X", stats, _THEMES_P95_BUDGET_MS)
    assert stats.p95_ms <= _THEMES_P95_BUDGET_MS, (
        f"P95 themes-with-X {stats.p95_ms:.1f}ms exceeds the "
        f"{_THEMES_P95_BUDGET_MS:.0f}ms budget "
        f"(p50={stats.p50_ms:.1f} p99={stats.p99_ms:.1f} max={stats.max_ms:.1f}, "
        f"n={stats.samples}) — AGE themes-with-X is over budget at 50k/500k/1M."
    )


# --------------------------------------------------------------------------- #
# Tenant-correctness during the benchmark — a local query for tenant T only ever
# touches T's subgraph (no cross-tenant leak inflating per-query cost).
# --------------------------------------------------------------------------- #
def test_benchmark_queries_stay_tenant_scoped(
    benchmark_corpus: _BenchmarkCorpus,
    bench_conn: psycopg.Connection,
) -> None:
    corpus = benchmark_corpus
    tenant, query = corpus.local_plan[0]
    ctx = graph_rag_search(
        bench_conn, corpus.cfg, query, backend=AgeBackend(), tenant=tenant,
        mode=LOCAL_MODE,
    )

    assert ctx.tenant_id == tenant
    # The seed resolves + the traversal reaches neighbours (non-trivial result).
    assert len(ctx.entities) > 1
    # EVERY entity (seed + reached) belongs to this tenant — no cross-tenant leak.
    prefix = f"person-{tenant}-"
    assert all(e.canonical_key.startswith(prefix) for e in ctx.entities), (
        "cross-tenant entity leaked into a tenant-scoped local traversal"
    )


# --------------------------------------------------------------------------- #
# Gate 3 — community build ≤ 5 min (spec §17c Q8). Measures Louvain detection +
# relational persistence across all tenants (the summary pass is excluded).
# --------------------------------------------------------------------------- #
def test_community_build_within_budget(
    community_corpus: _CommunityCorpus, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = community_corpus
    with capsys.disabled():
        print(
            f"\n[benchmark] community build: {corpus.build_seconds:.1f}s "
            f"({corpus.build_seconds / 60:.2f} min) for "
            f"{corpus.communities_total} communities across "
            f"{BENCHMARK_SPEC_FULL.tenants} tenants "
            f"(then summarized {corpus.summarized_total} / embedded "
            f"{corpus.embedded_total}; budget "
            f"{_COMMUNITY_BUILD_BUDGET_S / 60:.0f} min)"
        )
    assert corpus.build_seconds <= _COMMUNITY_BUILD_BUDGET_S, (
        f"community build took {corpus.build_seconds / 60:.2f} min, over the "
        f"{_COMMUNITY_BUILD_BUDGET_S / 60:.0f} min budget "
        f"({corpus.communities_total} communities / "
        f"{BENCHMARK_SPEC_FULL.tenants} tenants) — Louvain detection + relational "
        "persistence is over budget at 50k/500k."
    )


# --------------------------------------------------------------------------- #
# Gate 4 — P95 global (community) query ≤ 1 s (spec §17c Q8). Measures retrieval
# over PREBUILT communities + summary embeddings (the §17c Q8 "global" budget).
# --------------------------------------------------------------------------- #
def test_p95_global_query_budget(
    community_corpus: _CommunityCorpus,
    bench_conn: psycopg.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = community_corpus
    backend = AgeBackend()

    def _embedder_factory() -> FakeEmbedder:
        # The vector leg embeds the QUERY only (community embeddings are
        # prebuilt). A fresh deterministic FakeEmbedder keeps it Ollama-free.
        return FakeEmbedder(dim=_SUMMARY_DIM)

    def _call(tenant: str, query: str) -> Callable[[], object]:
        return lambda: graph_rag_search(
            bench_conn, corpus.cfg, query, backend=backend, tenant=tenant,
            mode=GLOBAL_MODE, embedder_factory=_embedder_factory,
        )

    calls = [_call(tenant, query) for tenant, query in corpus.global_plan]
    stats = measure(calls, warmup=_GLOBAL_WARMUP)
    _report(capsys, "GLOBAL query", stats, _GLOBAL_P95_BUDGET_MS)

    assert stats.p95_ms <= _GLOBAL_P95_BUDGET_MS, (
        f"P95 global query {stats.p95_ms:.1f}ms exceeds the "
        f"{_GLOBAL_P95_BUDGET_MS:.0f}ms budget "
        f"(p50={stats.p50_ms:.1f} p99={stats.p99_ms:.1f} max={stats.max_ms:.1f}, "
        f"n={stats.samples}) — community-level RRF retrieval is over budget at "
        "50k/500k/1M."
    )


# --------------------------------------------------------------------------- #
# Gate 5 — P95 fuse query ≤ 1.5 s (spec §17d dec 4). Measures graph ⊕ hybrid fuse
# end-to-end (incl. the query embedding) on the appended 'default'-tenant slice:
# the graph (local) leg traverses the default subgraph, the hybrid leg runs
# ``hybrid_search`` (FTS + full cosine scan) over the 2k-chunk default corpus.
# --------------------------------------------------------------------------- #
def test_p95_fuse_query_budget(
    fuse_corpus: _FuseCorpus,
    bench_conn: psycopg.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = fuse_corpus
    backend = AgeBackend()

    def _embedder_factory() -> FakeEmbedder:
        # The hybrid leg embeds the QUERY only (chunk embeddings are prebuilt). A
        # fresh deterministic 4096-dim FakeEmbedder keeps it Ollama-free and
        # dim-matched to the seeded chunks.embedding column.
        return FakeEmbedder(dim=_FUSE_CHUNK_DIM)

    def _call(query: str) -> Callable[[], object]:
        return lambda: graph_rag_search(
            bench_conn, corpus.cfg, query, backend=backend, tenant="default",
            mode=FUSE_MODE, embedder_factory=_embedder_factory,
        )

    calls = [_call(query) for query in corpus.fuse_plan]
    stats = measure(calls, warmup=_FUSE_WARMUP)
    _report(capsys, "FUSE query", stats, _FUSE_P95_BUDGET_MS)

    assert stats.p95_ms <= _FUSE_P95_BUDGET_MS, (
        f"P95 fuse query {stats.p95_ms:.1f}ms exceeds the "
        f"{_FUSE_P95_BUDGET_MS:.0f}ms budget "
        f"(p50={stats.p50_ms:.1f} p99={stats.p99_ms:.1f} max={stats.max_ms:.1f}, "
        f"n={stats.samples}) — graph⊕hybrid fuse is over budget on the "
        f"default-tenant slice + {corpus.chunk_count}-chunk hybrid corpus."
    )


def _report(
    capsys: pytest.CaptureFixture[str],
    label: str,
    stats: LatencyStats,
    budget_ms: float,
) -> None:
    """Print the P50/P95/P99/max line + PASS/FAIL verdict (always visible)."""
    verdict = "PASS" if stats.p95_ms <= budget_ms else "FAIL"
    with capsys.disabled():
        print(
            f"\n[benchmark] {stats.describe(label)} "
            f"| budget P95 ≤ {budget_ms:.0f}ms → {verdict}"
        )
