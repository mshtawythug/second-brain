"""Measure the token cost of agent-facing brain payloads, before and after.

Nothing in the repo can answer "how many tokens does one `brain search` cost an
agent?" today. ``recall`` computes ``used_tokens``, hands it back through
``to_dict()``, and every caller drops it; the token-savings numbers in
``README.md`` and ``docs/configuration.md`` are hand-written prose that no code
produces. This script is the measurement harness that makes those numbers
reproducible, so a payload-size change can be *proven* rather than asserted.

What it measures, per seeded query:

``search``
    ``json.dumps(search_results_json(results), ensure_ascii=False)`` — the
    COMPACT serialization of the shared seven-key projection at
    ``format_search.search_results_json``, the single construction site
    ``brain search --json`` and MCP ``brain_search`` both go through. Neither
    surface puts exactly these bytes on the wire; see :func:`measure_search`
    for what each adds on top. The projection is what the waves move, so the
    projection is what is measured.
``search_brief``
    ``json.dumps(search_results_brief_json(results, cost=embedder.count_tokens),
    ensure_ascii=False)`` — the Wave-1 trimmed projection, in which each
    result's ``snippet`` carries the cheaper of the document's ingest-time
    summary and its query-conditioned chunk snippet, plus a ``snippet_source``
    key. Serialized from the SAME retrieval as ``search`` above (see
    :func:`measure_search`), so the delta between the two arms is the
    projection and nothing else.
``recall``
    ``result.to_dict()`` plus ``context_block``, i.e. the dict MCP
    ``brain_recall`` returns (``mcp_server.brain_recall``, the assembly that
    begins ``payload = result.to_dict()``) — that surface adds no envelope of
    its own.

Tokens come from ``embedder.count_tokens`` — the offline ``tiktoken``
``cl100k_base`` count the chunker and the recall budgeter already spend
against — never a ``chars / 4`` estimate. That half of the ``Embedder``
Protocol needs no network, so the script also works under
``BRAIN_EMBEDDER=none``.

**Whose numbers these are.** MCP is the agent-facing surface, so both measure
functions apply the trust lens MCP applies: ``sensitivity="normal"``
(:data:`AGENT_SENSITIVITY_LENS`), excluding confidential documents from the
match set exactly as ``mcp_server._confidential_lens`` does by default.
Passing ``None`` instead would search BOTH tiers — a different match set on a
corpus with confidential documents, not merely a bigger payload.

**Read-only.** It runs against the live corpus on port 55432 by design — that
is the corpus the numbers are about — so it never writes: no ingest, no
``record_search_query`` (``recall()`` deliberately leaves telemetry to its
surfaces), and the connection is put into a server-enforced read-only
transaction by :func:`enforce_read_only`. Pointing it at production is still
opt-in via ``BRAIN_TOKEN_REPORT_ALLOW_PROD``, a deliberate speed bump against
an accidental run in a destructive context.

**Determinism, honestly.** The two search arms describe ONE retrieval
serialized twice, so any difference between them is the projection — never a
second, differently-decayed search. Two back-to-back RUNS are a different
matter: with ``BRAIN_RECENCY_HALFLIFE_DAYS`` set, each result's ``score``
carries a decay term derived from ``now()``, so the float's repr can gain or
lose a digit between runs. ``score`` is the ONLY field that can differ — the
decay multiplies every result's ``rrf_score`` by the same positive factor
within a run, which is order-preserving, so the ranking and every other field
are invariant.

**Both chars AND tokens drift, and they drift independently.** Measured on the
live corpus across repeated runs of an UNCHANGED arm: ``tokens`` moves by up to
**±4** (a delta of ``+1`` occurs in roughly 20% of runs), and ``chars`` moves by
a similar handful. The two are **sign-uncorrelated** — one trial recorded
``−6`` chars against ``+2`` tokens; the Wave-1 re-measurement recorded ``+1``
token against ``+7`` chars on the ``search`` arm while the untouched ``recall``
arm moved ``0`` tokens against ``−1`` char. Do NOT read "chars are noise,
tokens are signal": **neither is signal at this magnitude.** Treat a delta of a
few tokens *or* a few chars on an arm you did not change as expected run-to-run
noise, not as a regression. The savings a wave is claiming should be orders of
magnitude larger than this floor — Wave 1's ``search_brief`` arm moved −14,644
tokens (−57.4%), which is three orders of magnitude above it.

This harness therefore cannot establish byte-identity of an unchanged path;
it is a magnitude instrument. Byte-identity of the default ``search``
projection is established by ``tests/test_search_output_unchanged.py``, which
compares the projection directly and deterministically.

Set ``BRAIN_RECENCY_HALFLIFE_DAYS`` aside (or hold it fixed) if you need exact
byte reproducibility. The ``--json`` envelope carries a ``config`` block
recording the retrieval knobs in force, so a before/after diff cannot silently
compare two runs made under different settings.

Standalone script (not a ``brain`` subcommand), following the
``scripts/embedding_smoke.py`` precedent: off the user-facing CLI surface, and
outside BOTH gates ``bin/brain-ci`` runs over the package — coverage
(``--cov=brain``) and types (``mypy src/``) — so
``tests/test_token_payload_report.py`` is what holds it honest.

Usage::

    export BRAIN_TOKEN_REPORT_ALLOW_PROD=1
    python scripts/token_payload_report.py --json > before.json
    # ... land a wave ...
    python scripts/token_payload_report.py --json > after.json
    diff <(jq .totals before.json) <(jq .totals after.json)
"""
import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import psycopg
from query_files import load_query_lines

from brain.config import Config, ConfigError
from brain.db import connect
from brain.embeddings import make_embedder
from brain.errors import BrainError
from brain.format_search import search_results_brief_json, search_results_json
from brain.ingest import Embedder
from brain.recall import recall
from brain.search import SearchResult, _build_tsquery, hybrid_search
from brain.snippet_context import NEIGHBOR_WINDOW, expand_snippet_with_neighbors
from brain.token_budget import TokenCost
from brain.token_report import count_payload_tokens, serialize_payload

DEFAULT_QUERIES_FILE = Path(__file__).resolve().parent / "token_payload_queries.txt"

#: Opt-in for the live corpus. Named in the refusal message so the fix is
#: copy-pasteable from the error itself.
ALLOW_PROD_ENV = "BRAIN_TOKEN_REPORT_ALLOW_PROD"

#: Ports that mean PRODUCTION — 55432 is the live corpus (docker-compose.yml,
#: ./data/postgres bind-mount); 5433 was the historical mapping and is refused
#: defensively. Mirrors ``tests/conftest.py::_PROD_PORTS`` and ``bin/brain-ci``.
PROD_PORTS = frozenset({55432, 5433})
PROD_DB_NAME = "second_brain"
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: The F6 trust lens both measure functions apply, matching the surface these
#: numbers describe. MCP passes ``_confidential_lens(include_confidential)``
#: inside both ``mcp_server.brain_search`` and ``mcp_server.brain_recall``
#: (grep that call — it appears at each tool's ``hybrid_search`` /``recall``
#: call site), which is ``"normal"`` unless the caller opts in. ``None`` would
#: mean BOTH tiers — see :func:`brain.recall.recall`'s docstring paragraph
#: beginning '``sensitivity`` is forwarded straight to ``hybrid_search``' — a
#: different match set, not just a different payload size.
#:
#: Cited by SYMBOL, not by line. An earlier revision of this file carried five
#: ``mcp_server.py:NNN`` pointers; a single wave added +263 lines to that file
#: and invalidated every one of them, and one had been wrong when written. A
#: pointer that rots silently is worse than no pointer, because it still reads
#: as evidence.
AGENT_SENSITIVITY_LENS = "normal"

#: Exit codes. Distinct so a wrapper can tell a refusal from a real failure.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PROD_REFUSED = 2

#: Every surface label a run can emit, so the set a ``--json`` consumer may see
#: is enumerable from one place. **Documentation only — nothing type-checks it.**
#: ``bin/brain-ci`` runs ``mypy src/``, and this file is not under ``src/``
#: (as the module docstring above already states); a typo'd label would
#: therefore reach the JSON envelope unflagged. What catches that is
#: ``test_main_json_envelope_reports_every_surface``, which pins the exact
#: surface labels a run emits. The ``Literal`` earns its place as the readable
#: enumeration, not as a guard.
#: The document-fetch surface (``brain show`` / MCP ``brain_get_document``)
#: joins this union in Wave 3, when something actually emits it.
Surface = Literal["search", "search_brief", "recall"]


@dataclass(frozen=True)
class PayloadMeasurement:
    """One surface's cost for one query."""

    query: str
    surface: Surface
    chars: int
    tokens: int
    results: int

    def to_dict(self) -> dict[str, Any]:
        """JSON projection — the diffable unit of a ``--json`` run."""
        return {
            "query": self.query,
            "surface": self.surface,
            "chars": self.chars,
            "tokens": self.tokens,
            "results": self.results,
        }


def _measure(
    *,
    query: str,
    surface: Surface,
    payload: object,
    results: int,
    embedder: Embedder,
) -> PayloadMeasurement:
    """Serialize ``payload`` the way an agent receives it, then size it.

    The single place JSON is produced, so every surface is measured by the
    same rule: the serialized bytes, not the dataclasses behind them.

    The serialization is :func:`brain.token_report.serialize_payload`, imported
    rather than re-implemented, so this harness and the persisted
    ``payload_tokens`` column cannot drift apart. See that module's docstring:
    it used to claim the two were cross-checked while this function duplicated
    the ``dumps`` call.
    """
    serialized = serialize_payload(payload)
    return PayloadMeasurement(
        query=query,
        surface=surface,
        chars=len(serialized),
        tokens=embedder.count_tokens(serialized),
        results=results,
    )


def _search_payload(
    results: list[SearchResult],
    *,
    brief: bool,
    cost: TokenCost,
) -> object:
    """The projection an agent receives for ``results``.

    ---- WAVE 1 SEAM (FLIPPED) -------------------------------------------
    Before Wave 1 both arms returned the full projection and ``brief`` only
    LABELLED the measurement, so a pre-Wave-1 run stayed comparable with a
    post-Wave-1 one. Wave 1 landed ``search_results_brief_json``, so the
    ``search_brief`` arm now measures what it is named after. Nothing else had
    to be threaded: ``cost`` was carried through the seam from the start,
    precisely so this stayed a one-body change.

    ``cost`` matters. :func:`measure_search` passes ``embedder.count_tokens``,
    so the summary-vs-snippet choice inside the brief projection is priced in
    the same ``cl100k_base`` count as the ``tokens`` column beside it. The
    ``len`` default would price it in CHARACTERS — a different, silently
    inconsistent measurement.
    """
    return (
        search_results_brief_json(results, cost=cost)
        if brief
        else search_results_json(results)
    )


def measure_search(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    embedder: Embedder,
    query: str,
    limit: int,
    sensitivity: str | None = AGENT_SENSITIVITY_LENS,
) -> list[PayloadMeasurement]:
    """Cost of the shared search projection for one query — both arms.

    Returns the ``search`` and ``search_brief`` measurements from a SINGLE
    ``hybrid_search`` call. Running the retrieval twice would double the DB
    work and let the two arms disagree for a reason that has nothing to do
    with the projection: with ``BRAIN_RECENCY_HALFLIFE_DAYS`` set (180 by
    default) each ``score`` carries a ``now()``-derived decay term, so two
    executions can legitimately serialize to different bytes. One retrieval,
    two serializations — so the delta between the arms is attributable to the
    PROJECTION alone, which is the only thing the comparison is claiming.

    **What the number is.** ``json.dumps(..., ensure_ascii=False)`` over
    :func:`brain.format_search.search_results_json` — the COMPACT
    serialization of the frozen seven-key projection. Neither agent-facing
    surface emits exactly these bytes:

    * ``brain search --json`` (:func:`brain.cli_search.search`, the
      ``search_results_json(results)`` branch) hands the projection to
      ``format.emit_json``, which calls Rich's ``console.print_json`` — that
      parses the string back and re-serializes it with ``indent=2``. Over a
      5-result list that is ~35 newlines plus leading whitespace, i.e. the CLI
      writes several hundred bytes MORE than measured here. (Rich's
      ``ensure_ascii`` default has moved across releases and the ``rich>=13.9``
      pin has no upper bound, so no claim is made about non-ASCII escaping in
      either direction.)
    * MCP ``brain_search`` returns
      ``{"session_id": ..., "results": <this>, **search_meta_json(...)}``
      (``mcp_server.brain_search``'s ``return`` statement — the one whose
      comment reads "The two original keys come FIRST"); the envelope around
      ``results`` is NOT included here.

    Both are constants on either side of a before/after diff, and the
    projection is what every wave of the plan actually changes.

    ``cfg``'s ranking knobs are passed through so the measurement reflects the
    search the operator actually runs, not library defaults. ``sensitivity``
    defaults to :data:`AGENT_SENSITIVITY_LENS` so the match set is MCP's.
    """
    results = hybrid_search(
        conn,
        embedder=embedder,
        query=query,
        limit=limit,
        vector_sim_floor=cfg.vector_sim_floor,
        recency_halflife_days=cfg.recency_halflife_days,
        snippet_context_tokens=cfg.snippet_context_tokens,
        sensitivity=sensitivity,
    )
    return [
        _measure(
            query=query,
            surface=surface,
            payload=_search_payload(
                results, brief=brief, cost=embedder.count_tokens
            ),
            results=len(results),
            embedder=embedder,
        )
        for surface, brief in (("search", False), ("search_brief", True))
    ]


def measure_recall(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    embedder: Embedder,
    query: str,
    budget_tokens: int,
    sensitivity: str | None = AGENT_SENSITIVITY_LENS,
) -> PayloadMeasurement:
    """Cost of one MCP ``brain_recall`` payload.

    Measures ``to_dict()`` **plus** ``context_block`` **plus**
    ``payload_tokens`` because that is the dict ``mcp_server.brain_recall``
    returns — its assembly is the four consecutive statements
    ``payload = result.to_dict()`` → ``payload["context_block"] = …`` →
    ``payload["payload_tokens"] = …`` → ``return payload``, and this function
    mirrors all four — measuring only ``to_dict()`` would
    under-report by the size of the block the agent actually pastes. Unlike
    search, recall wraps its result in no envelope, so this is the whole
    payload.

    ``payload_tokens`` is Wave 3's additive key and is reproduced here in the
    same ORDER and by the same computation as the tool: counted over the
    payload *before* insertion, then inserted last. That is the only
    non-circular definition (a key cannot count itself), and it is why the
    order matters — inserting it earlier would change what a later
    ``json.dumps`` sees. Mirroring it is not optional bookkeeping: without it
    this harness under-reports the recall surface by the key's own width, and
    every Wave-3-vs-Wave-0 recall comparison — and Wave 5's telemetry baseline
    — silently compares two different payloads.

    Consequence for the committed baseline: the 11 ``recall`` rows in
    ``docs/audits/2026-08-10-token-payload-baseline.json`` were measured BEFORE
    this key existed, so a re-run is expected to come in a few tokens higher
    per query. That gap is the key, not a regression.

    ``sensitivity`` defaults to :data:`AGENT_SENSITIVITY_LENS`, matching
    ``mcp_server.brain_recall``'s own ``_confidential_lens(include_confidential)``
    argument; ``recall`` forwards it straight to ``hybrid_search``
    and its own docstring is explicit that ``None`` means both tiers, which is
    the CLI's lens, not the agent's.
    """
    result = recall(
        conn,
        cfg,
        embedder=embedder,
        query=query,
        budget_tokens=budget_tokens,
        max_candidates=cfg.recall_max_candidates,
        sensitivity=sensitivity,
    )
    payload = result.to_dict()
    payload["context_block"] = result.context_block()
    # Order-sensitive: counted over the payload as it stands, then inserted
    # last — exactly as mcp_server.brain_recall does it. See the docstring.
    payload["payload_tokens"] = count_payload_tokens(
        payload, cost=embedder.count_tokens
    )
    return _measure(
        query=query,
        surface="recall",
        payload=payload,
        results=len(result.passages),
        embedder=embedder,
    )


def prod_refusal(database_url: str) -> str | None:
    """Refusal text when ``database_url`` is production and opt-in is unset.

    ``None`` means "go ahead". Split out from :func:`main` so the refusal is
    testable without running the whole script, and so the message names
    :data:`ALLOW_PROD_ENV` in exactly one place.
    """
    if os.environ.get(ALLOW_PROD_ENV):
        return None
    parsed = urlparse(database_url)
    host = (parsed.hostname or "").lower()
    dbname = (parsed.path or "").lstrip("/")
    is_local = host in _LOCAL_HOSTS
    if not ((is_local and parsed.port in PROD_PORTS) or dbname == PROD_DB_NAME):
        return None
    return (
        "refusing to run against what looks like the PRODUCTION database "
        f"(host={host!r} port={parsed.port!r} db={dbname!r}).\n"
        "This script is read-only, and production IS the corpus the numbers "
        "are about — but an accidental run in a destructive context is not "
        "worth the risk. Opt in explicitly:\n"
        f"\n    export {ALLOW_PROD_ENV}=1\n"
    )


def enforce_read_only(conn: psycopg.Connection[Any]) -> None:
    """Put ``conn`` into a server-enforced read-only transaction.

    Belt to the code's braces: every function this script calls is
    SELECT-only, and this makes a future accidental write fail loudly at the
    server instead of silently mutating the live corpus. The ``rollback()``
    first closes the implicit transaction ``db.connect`` leaves open after
    registering the pgvector adapter — psycopg refuses the property change
    mid-transaction.
    """
    conn.rollback()
    conn.read_only = True


def retrieval_config(cfg: Config) -> dict[str, Any]:
    """The retrieval knobs every measured number depends on.

    Emitted into the ``--json`` envelope so a before/after diff cannot
    silently compare two runs made under different settings — the one failure
    mode a before/after harness must not have. A changed embedder or a changed
    ``vector_sim_floor`` moves every number here without touching a single
    line of payload code.
    """
    return {
        "embedder": cfg.embedder,
        "vector_sim_floor": cfg.vector_sim_floor,
        "recency_halflife_days": cfg.recency_halflife_days,
        "snippet_context_tokens": cfg.snippet_context_tokens,
        "snippet_max_chars": cfg.snippet_max_chars,
        "recall_passage_tokens": cfg.recall_passage_tokens,
        "recall_max_candidates": cfg.recall_max_candidates,
        "sensitivity": AGENT_SENSITIVITY_LENS,
    }


def _print_human(measurements: list[PayloadMeasurement], cfg: Config) -> None:
    """Group measurements by query, then print a totals block."""
    print("config")
    for key, value in retrieval_config(cfg).items():
        print(f"  {key:<24} {value}")
    seen: list[str] = []
    for m in measurements:
        if m.query not in seen:
            seen.append(m.query)
            print(f'\nquery: "{m.query}"')
        print(
            f"  {m.surface:<13} {m.results:>3} results  "
            f"{m.chars:>8} chars  {m.tokens:>7} tokens"
        )
    print("\ntotals")
    for surface, totals in _totals(measurements).items():
        print(
            f"  {surface:<13} {totals['chars']:>8} chars  "
            f"{totals['tokens']:>7} tokens  "
            f"(mean {totals['mean_tokens']:.1f} tokens/query)"
        )


def _totals(measurements: list[PayloadMeasurement]) -> dict[str, dict[str, Any]]:
    """Per-surface sums plus a mean.

    Keys are in first-seen surface order, which is what :func:`_print_human`
    renders. The ``--json`` envelope re-sorts them (``sort_keys=True``)
    because a stable key order is what makes two runs diffable — the ordering
    here is a display detail, not part of the JSON contract.
    """
    totals: dict[str, dict[str, Any]] = {}
    for m in measurements:
        bucket = totals.setdefault(
            m.surface, {"queries": 0, "chars": 0, "tokens": 0, "mean_tokens": 0.0}
        )
        bucket["queries"] += 1
        bucket["chars"] += m.chars
        bucket["tokens"] += m.tokens
    for bucket in totals.values():
        bucket["mean_tokens"] = bucket["tokens"] / bucket["queries"]
    return totals


# ---------------------------------------------------------------------------
# Wave 4 — what actually constrains a snippet's size
# ---------------------------------------------------------------------------
#
# This mode began life as ``--adaptive-stats``: the engagement measurement for
# an Otsu cut over neighbour relevance, whose committed artifact is
# ``docs/audits/2026-08-13-adaptive-snippet-engagement.json``. That mechanism
# was measured and REMOVED (the full finding lives in
# ``brain.snippet_context``'s module docstring), so the counterfactual arm went
# with it and what survives is the half that decided the question:
#
#   engagement_rate                   the plan's original question. Kept —
#                                     re-derivable, and a future wave that wants
#                                     to re-open neighbour selection needs it.
#                                     It was NOT the deciding number: it read
#                                     74.5%, comfortably passing, while the
#                                     mechanism changed nothing.
#   results_with_any_neighbor         how often expansion admits anything at
#                                     all. Measured 3/55 — the walk budget is
#                                     ~200 tokens against a ~570-token median
#                                     chunk, so there is usually no neighbour
#                                     set to select from.
#   results_matched_chunk_fills_cap   how often the MATCHED CHUNK alone reaches
#                                     ``max_chars``. Measured 47/55 (85.5%) —
#                                     the cap truncates the matched chunk itself
#                                     before neighbours are ever consulted.
#                                     NOTE: the superseded
#                                     ``adaptive-snippet-engagement.json``
#                                     carries a key of the SAME NAME valued 55,
#                                     under a different definition — there it
#                                     counted rows whose matched-only tokens
#                                     equalled the delivered tokens, i.e. "the
#                                     expansion added nothing", which is 55/55.
#                                     "The matched chunk alone is at or over the
#                                     cap" is 47/55, and that same older artifact
#                                     records it correctly as ``legacy_pinned``.
#                                     The two artifacts do not disagree; only the
#                                     reused key name does.
#
# The last two are the constraints that bind. Anything aiming to make snippet
# truncation content-aware has to move one of them.


@dataclass(frozen=True)
class SnippetConstraint:
    """One search RESULT's snippet, and which limit decided its size."""

    query: str
    document_id: str
    #: Whether the neighbour set held >= 2 distinct ``ts_rank`` values — the
    #: precondition any relevance-based neighbour selection would need.
    neighbors_differ: bool
    #: Neighbour CHUNKS admitted, counted by identity against the fetched chunk
    #: rows — NOT by splitting on ``\n\n``, which chunk bodies contain.
    neighbors_admitted: int
    #: Neighbour chunks available in the window, admitted or not.
    neighbors_available: int
    #: Tokens the agent actually receives (post-``max_chars``).
    tokens: int
    #: Tokens BEFORE ``max_chars``. The gap between the two is what the cap
    #: discards, and it is where a content-aware truncation would have room.
    uncapped_tokens: int
    #: Tokens in the matched chunk alone, capped. Equal to ``tokens`` means the
    #: expansion contributed nothing to this result.
    matched_only_tokens: int
    pinned_at_cap: bool
    matched_chunk_fills_cap: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "document_id": self.document_id,
            "neighbors_differ": self.neighbors_differ,
            "neighbors_admitted": self.neighbors_admitted,
            "neighbors_available": self.neighbors_available,
            "tokens": self.tokens,
            "uncapped_tokens": self.uncapped_tokens,
            "matched_only_tokens": self.matched_only_tokens,
            "pinned_at_cap": self.pinned_at_cap,
            "matched_chunk_fills_cap": self.matched_chunk_fills_cap,
        }


#: Decimal places at which two ``ts_rank`` values count as the same.
#:
#: ``ts_rank`` is float4-derived, so two equally-irrelevant neighbours can
#: differ in the last bits; comparing raw floats would report that noise as a
#: real difference and inflate ``engagement_rate``. Nine places is far below any
#: meaningful ``ts_rank`` gap and far above float4 noise. (Inlined here rather
#: than imported: this is the last remaining user of the rounding, and a
#: package module existing only for a script is the dead code CLAUDE.md forbids.)
_DISTINCTNESS_PLACES = 9

#: A cap high enough to be no cap, so the pre-``max_chars`` size is observable.
_UNCAPPED = 10**9


def has_two_distinct(values: list[float]) -> bool:
    """Whether ``values`` holds at least two distinct entries."""
    return len({round(float(v), _DISTINCTNESS_PLACES) for v in values}) >= 2


def _neighbor_rows(
    conn: psycopg.Connection[Any],
    *,
    document_id: str,
    best_chunk_index: int,
    tsquery: str,
    window: int,
) -> list[tuple[str, float]]:
    """``(content, ts_rank)`` for every neighbour in the window, EXCLUDING the match."""
    rows = conn.execute(
        """
        SELECT c.chunk_index, c.content, ts_rank(c.tsv, %s::tsquery)
        FROM chunks c
        WHERE c.document_id = %s
          AND c.chunk_index BETWEEN %s AND %s
        ORDER BY c.chunk_index
        """,
        (
            tsquery,
            document_id,
            max(0, best_chunk_index - window),
            best_chunk_index + window,
        ),
    ).fetchall()
    return [(str(r[1]), float(r[2])) for r in rows if int(r[0]) != best_chunk_index]


def _chunk_content(
    conn: psycopg.Connection[Any], *, document_id: str, chunk_index: int
) -> str:
    row = conn.execute(
        "SELECT content FROM chunks WHERE document_id = %s AND chunk_index = %s",
        (document_id, chunk_index),
    ).fetchone()
    return "" if row is None else str(row[0])


def measure_snippet_constraints(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    embedder: Embedder,
    query: str,
    limit: int,
    sensitivity: str | None = AGENT_SENSITIVITY_LENS,
) -> list[SnippetConstraint]:
    """Which limit decided each result's snippet size, for one query.

    ``explain=True`` is what makes this possible without re-implementing the
    ranking: :class:`brain.search.SearchExplanation` already carries the
    ``best_chunk_index`` the expansion pivots on.

    The expansion is run UNCAPPED and truncated in Python. The helper's cap is
    a plain suffix truncation (``stitched[:cap]``), so this is byte-equivalent
    to passing ``max_chars`` — and it makes the pre-cap size observable, which
    is the only way to tell "expansion added nothing" from "expansion added
    something the cap then discarded".
    """
    results = hybrid_search(
        conn,
        embedder=embedder,
        query=query,
        limit=limit,
        vector_sim_floor=cfg.vector_sim_floor,
        recency_halflife_days=cfg.recency_halflife_days,
        # 0 would skip expansion entirely and there would be nothing to measure.
        snippet_context_tokens=cfg.snippet_context_tokens,
        explain=True,
        sensitivity=sensitivity,
    )
    tsquery = _build_tsquery(conn, query)
    cap = cfg.snippet_max_chars
    out: list[SnippetConstraint] = []
    for result in results:
        if result.explain is None:
            continue
        idx = result.explain.best_chunk_index
        content = _chunk_content(conn, document_id=result.document_id, chunk_index=idx)
        neighbours = _neighbor_rows(
            conn,
            document_id=result.document_id,
            best_chunk_index=idx,
            tsquery=tsquery,
            window=NEIGHBOR_WINDOW,
        )
        expanded = expand_snippet_with_neighbors(
            conn,
            document_id=result.document_id,
            best_chunk_index=idx,
            best_content=content,
            embedder=embedder,
            budget_tokens=cfg.snippet_context_tokens,
            max_chars=_UNCAPPED,
        )
        out.append(
            SnippetConstraint(
                query=query,
                document_id=result.document_id,
                neighbors_differ=bool(tsquery)
                and has_two_distinct([s for _, s in neighbours]),
                neighbors_admitted=sum(1 for c, _ in neighbours if c and c in expanded),
                neighbors_available=len(neighbours),
                tokens=embedder.count_tokens(expanded[:cap]),
                uncapped_tokens=embedder.count_tokens(expanded),
                matched_only_tokens=embedder.count_tokens(content[:cap]),
                pinned_at_cap=len(expanded) >= cap,
                matched_chunk_fills_cap=len(content) >= cap,
            )
        )
    return out


def constraint_totals(rows: list[SnippetConstraint]) -> dict[str, Any]:
    """The numbers that decide whether snippet size is worth attacking, and where.

    ``engagement_rate`` keeps the plan's original denominator: engaged results
    over ALL results. Dividing by engaged rows would make any non-empty run
    report 100%, which is the shape of a metric that cannot fail.
    """
    n = len(rows)
    differ = [r for r in rows if r.neighbors_differ]
    return {
        "results": n,
        "neighbors_differ": len(differ),
        # The plan's original question, kept re-derivable. It read 74.5% while
        # the mechanism it gated changed nothing — kept as evidence that a high
        # value here does NOT imply an effect on payload size.
        "engagement_rate": (len(differ) / n) if n else 0.0,
        "results_with_any_neighbor": sum(1 for r in rows if r.neighbors_admitted),
        "results_with_any_neighbor_rate": (
            sum(1 for r in rows if r.neighbors_admitted) / n if n else 0.0
        ),
        "neighbors_admitted": sum(r.neighbors_admitted for r in rows),
        "neighbors_available": sum(r.neighbors_available for r in rows),
        "pinned_at_cap": sum(1 for r in rows if r.pinned_at_cap),
        "pinned_at_cap_rate": (
            sum(1 for r in rows if r.pinned_at_cap) / n if n else 0.0
        ),
        "results_matched_chunk_fills_cap": sum(
            1 for r in rows if r.matched_chunk_fills_cap
        ),
        "delivered_tokens": sum(r.tokens for r in rows),
        "uncapped_tokens": sum(r.uncapped_tokens for r in rows),
        # What ``max_chars`` throws away. The headroom a content-aware
        # truncation would be working with.
        "tokens_discarded_by_cap": sum(r.uncapped_tokens - r.tokens for r in rows),
    }


def _print_constraints(rows: list[SnippetConstraint], cfg: Config) -> None:
    print("config")
    for key, value in retrieval_config(cfg).items():
        print(f"  {key:<24} {value}")
    seen: list[str] = []
    for r in rows:
        if r.query not in seen:
            seen.append(r.query)
            print(f'\nquery: "{r.query}"')
        print(
            f"  neighbours {r.neighbors_admitted}/{r.neighbors_available} admitted"
            f"{' (differ)' if r.neighbors_differ else '         '}  "
            f"tokens {r.tokens:>5} delivered / {r.uncapped_tokens:>5} uncapped  "
            f"matched-chunk-fills-cap {int(r.matched_chunk_fills_cap)}"
        )
    totals = constraint_totals(rows)
    print("\nsnippet constraints")
    print(
        f"  neighbour scores differ        {totals['neighbors_differ']}/"
        f"{totals['results']} ({totals['engagement_rate'] * 100:.1f}%)"
    )
    print(
        f"  admitted any neighbour         {totals['results_with_any_neighbor']}/"
        f"{totals['results']} ({totals['results_with_any_neighbor_rate'] * 100:.1f}%)"
        "   <- the WALK BUDGET binding"
    )
    print(
        f"  matched chunk alone fills cap  "
        f"{totals['results_matched_chunk_fills_cap']}/{totals['results']}"
        "   <- the CHAR CAP binding"
    )
    print(f"  pinned at cap                  {totals['pinned_at_cap']}/{totals['results']}")
    print(
        f"  tokens delivered {totals['delivered_tokens']} of "
        f"{totals['uncapped_tokens']} produced "
        f"({totals['tokens_discarded_by_cap']} discarded by the cap)"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure the token cost of agent-facing brain payloads."
    )
    parser.add_argument(
        "--queries-file",
        type=Path,
        default=DEFAULT_QUERIES_FILE,
        help="Queries file (one per line; #-comments + blank lines ignored).",
    )
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        dest="queries",
        help=(
            "Measure this query instead of the file. Repeatable. Use this for "
            "anything personal — the committed queries file must stay PII-free."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=5, help="Top-K results per search (default 5)."
    )
    parser.add_argument(
        "--budget-tokens",
        type=int,
        default=None,
        help="Recall budget (default: cfg.recall_budget_tokens).",
    )
    parser.add_argument(
        "--snippet-constraints",
        dest="snippet_constraints",
        action="store_true",
        help=(
            "Wave 4: report which limit decides snippet size — the walk budget, "
            "the character cap, or neighbour relevance — instead of the payload "
            "report. Supersedes the removed --adaptive-stats mode; see "
            "brain.snippet_context's module docstring."
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit one JSON object on stdout instead of the human report.",
    )
    return parser


def _run_snippet_constraints(
    cfg: Config,
    *,
    embedder: Embedder,
    queries: list[str],
    args: argparse.Namespace,
) -> int:
    """The ``--snippet-constraints`` arm of :func:`main`, kept out of its body."""
    rows: list[SnippetConstraint] = []
    try:
        with connect(cfg.database_url) as conn:
            enforce_read_only(conn)
            for query in queries:
                rows.extend(
                    measure_snippet_constraints(
                        conn, cfg, embedder=embedder, query=query, limit=args.limit
                    )
                )
    except psycopg.Error as e:
        print(f"database error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except BrainError as e:
        print(f"brain error: {e}", file=sys.stderr)
        return EXIT_ERROR

    if args.json_output:
        print(
            json.dumps(
                {
                    "config": retrieval_config(cfg),
                    "limit": args.limit,
                    "queries": len(queries),
                    "measurements": [r.to_dict() for r in rows],
                    "totals": constraint_totals(rows),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _print_constraints(rows, cfg)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Parse args, measure every query, print a report. Returns an exit code."""
    args = _build_parser().parse_args(argv)

    queries: list[str] = list(args.queries)
    if not queries:
        queries_file: Path = args.queries_file
        if not queries_file.is_file():
            print(f"queries file not found: {queries_file}", file=sys.stderr)
            return EXIT_ERROR
        queries = load_query_lines(queries_file)
    if not queries:
        print("no queries to measure", file=sys.stderr)
        return EXIT_ERROR

    try:
        cfg = Config.load()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_ERROR

    refusal = prod_refusal(cfg.database_url)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return EXIT_PROD_REFUSED

    try:
        embedder = make_embedder(cfg)
    except (ConfigError, BrainError) as e:
        print(f"embedder error: {e}", file=sys.stderr)
        return EXIT_ERROR

    budget = (
        cfg.recall_budget_tokens if args.budget_tokens is None else args.budget_tokens
    )
    if args.snippet_constraints:
        return _run_snippet_constraints(
            cfg, embedder=embedder, queries=queries, args=args
        )

    measurements: list[PayloadMeasurement] = []
    try:
        with connect(cfg.database_url) as conn:
            enforce_read_only(conn)
            for query in queries:
                measurements.extend(
                    measure_search(
                        conn, cfg, embedder=embedder, query=query, limit=args.limit
                    )
                )
                measurements.append(
                    measure_recall(
                        conn,
                        cfg,
                        embedder=embedder,
                        query=query,
                        budget_tokens=budget,
                    )
                )
    except psycopg.Error as e:
        print(f"database error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except BrainError as e:
        print(f"brain error: {e}", file=sys.stderr)
        return EXIT_ERROR

    if args.json_output:
        print(
            json.dumps(
                {
                    "config": retrieval_config(cfg),
                    "limit": args.limit,
                    "budget_tokens": budget,
                    "queries": len(queries),
                    "measurements": [m.to_dict() for m in measurements],
                    "totals": _totals(measurements),
                },
                ensure_ascii=False,
                indent=2,
                # Stable key order so two runs diff on VALUES only. This is
                # why ``_totals``' insertion order is a display detail.
                sort_keys=True,
            )
        )
    else:
        _print_human(measurements, cfg)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
