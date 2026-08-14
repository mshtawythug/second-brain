"""`brain search` / `brain explain` — hybrid retrieval commands.

Extracted verbatim from :mod:`brain.cli` (which had grown past the 800-line
ceiling in CLAUDE.md) so retrieval work can proceed independently of the rest
of the CLI. Behaviour is unchanged — command names, flags, help text, output
and exit codes are identical to the previous in-``cli.py`` definitions.

Shared helpers still owned by ``cli.py`` are resolved through the ``brain.cli``
module object *at call time* (see the delegation block below) rather than bound
at import: ``cli.py`` imports this module to register its commands, so a
module-level import back would be a cycle. Reading the attribute at call time
additionally keeps ``monkeypatch.setattr("brain.cli.<name>", ...)`` — the patch
point the existing test suite uses — effective for these commands. Same pattern
as :mod:`brain._capture_command`.
"""
from __future__ import annotations

from datetime import datetime
from time import perf_counter
from typing import Any

import psycopg
import typer
from rich.console import Console

from .agent import resolve_agent_id
from .config import Config
from .db import connect
from .durations import since_window
from .errors import PersonAmbiguous, PersonNotFound
from .facets import SearchFacets, compute_facets
from .format import console, emit_json, explain_table, search_table
from .format_search import (
    NO_FACETS_MESSAGE,
    facets_renderable,
    search_envelope_json,
    search_meta_line,
    search_results_brief_json,
    search_results_json,
)
from .gaps import record_search_query
from .ingest import Embedder
from .queries import PersonMatch
from .search import SearchDiagnostics, build_tsquery
from .search_predicate import build_predicate
from .sensitivity import VALID_SENSITIVITY_LEVELS
from .token_report import count_results_tokens

# Rich console bound to stderr, for the facet panel. Rich resolves ``file``
# lazily, so a module-level instance still honours a redirected ``sys.stderr``
# — which is what keeps this assertable under ``CliRunner``.
_err_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# Delegation to `brain.cli`-owned helpers.
#
# These names stay in `cli.py` because commands that did NOT move still call
# them (`_validate_source_choice` -> `resurface`; `_build_embedder` -> ~20
# commands) and because the test suite patches them at `brain.cli.<name>`.
# Each wrapper resolves the attribute at call time, so the moved command
# bodies below are byte-identical to their pre-move form.
# ---------------------------------------------------------------------------


def _build_embedder(cfg: Config) -> Embedder:
    """Build the configured embedder via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli._build_embedder(cfg)  # type: ignore[attr-defined]


def _validate_sensitivity_choice(level: str | None) -> str | None:
    """Validate ``--sensitivity``, or return ``None`` for "both tiers".

    Raises :class:`typer.BadParameter` (exit 2, Typer's usage-error convention)
    rather than letting an unrecognized value reach SQL. Left un-validated it
    would bind cleanly, match zero rows, and print an empty table — which reads
    as "you have not marked anything", the most misleading possible answer to
    the one question this flag exists to ask.

    Deliberately does NOT route through ``brain.sensitivity.normalize_level``:
    that helper maps ``None``/``""`` to ``"normal"``, which is right for a WRITE
    (an omitted flag means store the default) and wrong for a FILTER (an omitted
    flag means do not filter at all).
    """
    if level is None:
        return None
    if level not in VALID_SENSITIVITY_LEVELS:
        raise typer.BadParameter(
            f"--sensitivity must be one of "
            f"{'/'.join(sorted(VALID_SENSITIVITY_LEVELS))} (got {level!r})"
        )
    return level


def _validate_source_choice(source: str | None) -> str | None:
    """Validate ``--source`` via the ``brain.cli`` owner of the source enum."""
    from . import cli as _cli

    return _cli._validate_source_choice(source)


def resolve_person_to_keys(
    conn: psycopg.Connection[Any], name_or_email: str
) -> PersonMatch:
    """Resolve a person to participant keys via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli.resolve_person_to_keys(conn, name_or_email)


def hybrid_search(*args: Any, **kwargs: Any) -> Any:
    """Run the hybrid search via the ``brain.cli`` patch point.

    A pass-through rather than a typed re-declaration: ``hybrid_search`` takes
    ~18 keyword arguments and duplicating that signature here would be a second
    place to keep in sync. The real signature is enforced at the definition
    site in :mod:`brain.search`.
    """
    from . import cli as _cli

    return _cli.hybrid_search(*args, **kwargs)


# ---------------------------------------------------------------------------
# Search-only helpers (moved with their commands — no other caller).
# ---------------------------------------------------------------------------


def _reconcile_tag_flags(
    tag: str | None, has_tag: str | None
) -> str | None:
    """Reconcile ``--tag`` and its ``--has-tag`` alias for ``search`` / ``explain``.

    ``--has-tag`` is a strict alias of ``--tag`` per plan D3. Both flags
    add the same ``%s = ANY(d.tags)`` predicate; supplying both with
    different values is a user error and exits with ``BadParameter``.
    Returns the single effective tag value to thread into ``hybrid_search``.
    """
    if tag is not None and has_tag is not None and tag != has_tag:
        raise typer.BadParameter(
            "--tag and --has-tag both given with different values"
        )
    return tag if tag is not None else has_tag


def _resolve_search_person(
    conn: psycopg.Connection[Any], person: str | None
) -> PersonMatch | None:
    """Resolve a ``--person`` argument or return ``None`` for the absent case.

    Maps :class:`brain.errors.PersonNotFound` / :class:`PersonAmbiguous`
    to Typer's :class:`BadParameter` so the CLI surface stays consistent
    with the rest of the flag-validation path. Returns ``None`` when
    ``person`` is itself ``None`` so the caller threads ``person_keys=None``
    / ``person_display_name=None`` into ``hybrid_search`` unchanged.
    """
    if person is None:
        return None
    try:
        return resolve_person_to_keys(conn, person)
    except (PersonNotFound, PersonAmbiguous) as e:
        raise typer.BadParameter(str(e)) from e


def _warn_if_fts_only_degraded(embedder: Embedder) -> None:
    """Print a one-line stderr hint when semantic search is off (``none`` backend).

    Mirrors the ``hybrid_search`` degradation condition (duck-typed
    ``produces_embeddings``) so ``brain search`` / ``brain explain`` tell the
    user WHY only the lexical leg ran and how to enable hybrid search. Emitted to
    stderr so it never pollutes ``--json`` stdout.
    """
    if not getattr(embedder, "produces_embeddings", True):
        typer.echo(
            "semantic search off (BRAIN_EMBEDDER=none) — install Ollama, set "
            "BRAIN_EMBEDDER=arctic, then 'brain init' + 'brain reembed' for "
            "hybrid search",
            err=True,
        )


def search(
    query: str = typer.Argument(...),
    limit: int = typer.Option(5, "--limit", "-n", min=1),
    source: str | None = typer.Option(None, "--source"),
    tag: str | None = typer.Option(None, "--tag"),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Lookback window; a bare number is DAYS (e.g. 7 = 7d). "
             "Suffixes: 7d / 24h / 90m.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of the table."
    ),
    fts_only: bool = typer.Option(False, "--fts-only"),
    # — F5 search transparency —
    facets: bool = typer.Option(
        False, "--facets",
        help="Group the full match set by source, content type, and top tags. "
             "With --json this implies --meta.",
    ),
    no_meta: bool = typer.Option(
        False, "--no-meta",
        help="Suppress the match-count / latency footer (written to stderr).",
    ),
    meta: bool = typer.Option(
        False, "--meta",
        help="With --json, wrap results in an envelope carrying counts, "
             "timings, and facets. Without --json this flag is a no-op "
             "(the footer is already on).",
    ),
    brief: bool = typer.Option(
        False, "--brief",
        help="With --json: return each document's ingest-time summary "
             "instead of its snippet when the summary is smaller, plus a "
             "snippet_source key naming which one you got. Much cheaper for "
             "agents; loses the why-this-matched signal on substituted "
             "results. No-op without --json.",
    ),
    # — Q1-C metadata filters — same set on `brain explain` below.
    person: str | None = typer.Option(
        None, "--person",
        help="Match docs where this person participated. "
             "Resolved through the directory (same as `brain people`).",
    ),
    after: datetime | None = typer.Option(
        None, "--after",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs sent/ingested on or after this ISO date "
             "(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS).",
    ),
    before: datetime | None = typer.Option(
        None, "--before",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs sent/ingested strictly before this ISO date.",
    ),
    updated_after: datetime | None = typer.Option(
        None, "--updated-after",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs EDITED on or after this ISO date. A different axis "
             "from --after: that filters by the document's own date "
             "(sent/ingested), this by when you last changed it.",
    ),
    updated_before: datetime | None = typer.Option(
        None, "--updated-before",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs EDITED strictly before this ISO date "
             "(see --updated-after for the --before distinction).",
    ),
    kind: str | None = typer.Option(
        None, "--kind",
        help="Filter by documents.content_type "
             "(transcript, email, email_thread, note, markdown, pdf, ...).",
    ),
    thread: str | None = typer.Option(
        None, "--thread", help="Filter by Gmail thread id.",
    ),
    draft: bool | None = typer.Option(
        None, "--draft/--no-draft",
        help="Include only drafts (--draft) or only published "
             "(--no-draft). Default: both.",
    ),
    has_tag: str | None = typer.Option(
        None, "--has-tag", help="Strict alias for --tag.",
    ),
    without_tag: str | None = typer.Option(
        None, "--without-tag",
        help="Exclude docs carrying this tag (combines with --tag).",
    ),
    sensitivity: str | None = typer.Option(
        None, "--sensitivity",
        help=(
            "Show only documents at this tier: normal|confidential. "
            "Default: both. This is a LENS, not an access control — an "
            "unfiltered search still returns confidential bodies, because the "
            "local CLI is inside the trust boundary."
        ),
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        help=(
            "Attribute this search to an agent id. Overrides BRAIN_AGENT_ID. "
            "Unset means unattributed. Recorded alongside (not instead of) "
            "the surface, which is always 'cli' here."
        ),
    ),
) -> None:
    """Hybrid search across the brain.

    Human runs end with a one-line footer on **stderr**
    (``544 matched · 3 shown · embed …ms · sql …ms · total …ms``), so
    ``brain search q --json | jq`` and ``brain search q > out.txt`` are both
    byte-identical to before. ``--no-meta`` turns the footer — and the extra
    count query behind it — off entirely.

    ``N matched`` counts documents that LEXICALLY match the query; the vector
    leg may additionally surface near-neighbours that this number does not
    include.
    """
    _validate_source_choice(source)
    effective_tag = _reconcile_tag_flags(tag, has_tag)
    # Validate before any query so `--sensitivity confidental` is a usage error
    # rather than a silent empty result set that reads as "nothing is marked".
    sensitivity = _validate_sensitivity_choice(sensitivity)
    since_days = None if since is None else since_window(since, unit="days")
    show_meta = not no_meta
    # ``--facets`` needs the whole match set measured, and the ``--json
    # --meta`` envelope reports the total explicitly. Anything that would
    # print or emit the number turns the count query on; ``--no-meta`` is the
    # escape hatch for a corpus where it is too slow.
    want_total = show_meta or facets or (json_output and meta)
    cfg = Config.load()
    embedder = _build_embedder(cfg)
    _warn_if_fts_only_degraded(embedder)
    facet_data: SearchFacets | None = None
    with connect(cfg.database_url) as conn:
        # Autocommit so the Plan 08 search-failure log INSERT below is a single
        # round-trip that persists immediately (hybrid_search reads are fine
        # under autocommit).
        conn.autocommit = True
        person_match = _resolve_search_person(conn, person)
        # The diagnostics holder captures the FTS-leg hit count from work the
        # search already does (no extra query) — the lexical-miss signal that
        # `brain gaps` keys off (the vector leg always returns filler).
        diagnostics = SearchDiagnostics()
        results = hybrid_search(
            conn,
            embedder=embedder,
            query=query,
            limit=limit,
            source_kind=source,
            tag=effective_tag,
            since_days=since_days,
            fts_only=fts_only,
            vector_sim_floor=cfg.vector_sim_floor,
            recency_halflife_days=cfg.recency_halflife_days,
            snippet_context_tokens=cfg.snippet_context_tokens,
            snippet_max_chars=cfg.snippet_max_chars,
            diagnostics=diagnostics,
            total_count=want_total,
            person_keys=person_match.keys if person_match else None,
            person_display_name=(
                person_match.display_name if person_match else None
            ),
            after=after,
            before=before,
            updated_after=updated_after,
            updated_before=updated_before,
            content_type=kind,
            thread_id=thread,
            draft=draft,
            without_tag=without_tag,
            sensitivity=sensitivity,
        )
        if facets:
            # ``hybrid_search`` does not return its predicate, so rebuild it
            # here from the SAME kwargs rather than widening that function's
            # return type. Assembly is pure string work; ``build_predicate``
            # is the single construction site, so the facet buckets provably
            # describe the same match set the results came from.
            t_facets = perf_counter()
            facet_data = compute_facets(
                conn,
                predicate=build_predicate(
                    source_kind=source,
                    tag=effective_tag,
                    since_days=since_days,
                    person_keys=person_match.keys if person_match else None,
                    after=after,
                    before=before,
                    updated_after=updated_after,
                    updated_before=updated_before,
                    content_type=kind,
                    thread_id=thread,
                    draft=draft,
                    without_tag=without_tag,
                    sensitivity=sensitivity,
                ),
                tsquery=build_tsquery(conn, query),
            )
            diagnostics.facets_ms = (perf_counter() - t_facets) * 1000.0
        # Wave 5 — what this call COST the caller, measured, not estimated.
        #
        # Only the ``--json`` paths are priced. The human path delivers a Rich
        # table with 120-char previews, not a payload; counting the JSON a
        # terminal caller never received would file a counterfactual under a
        # column that means "measured". It stays NULL, which is what NULL is
        # for. It also spares that path the tiktoken encode entirely.
        #
        # The projection is built ONCE here and reused by the bare-list emit
        # branch below, so pricing the payload does not re-run the per-result
        # summary-vs-snippet comparison. The ``--meta`` envelope path is the
        # exception: it rebuilds through ``search_envelope_json`` on purpose —
        # that function's "same call" guarantee is what keeps the envelope
        # from drifting from the bare list, and is worth one extra projection.
        projected: list[dict[str, Any]] | None = None
        baseline_tokens: int | None = None
        if json_output:
            projected = (
                search_results_brief_json(results, cost=embedder.count_tokens)
                if brief
                else search_results_json(results)
            )
            diagnostics.results_tokens = count_results_tokens(
                projected, cost=embedder.count_tokens
            )
            if brief:
                # The counterfactual, and ONLY when a cheaper mode was really
                # in effect: what these same results would have cost in the
                # default projection. A non-brief call had no alternative, so
                # it gets no baseline — inventing one would make every search
                # look like a saving.
                baseline_tokens = count_results_tokens(
                    search_results_json(results), cost=embedder.count_tokens
                )
        # Plan 08 — best-effort search-failure logging. ``record_search_query``
        # is the single narrow-catch chokepoint: it swallows a transient
        # ``psycopg.OperationalError`` AND the missing-table
        # ``psycopg.errors.UndefinedTable`` (migration 019 not applied) AND the
        # missing-column ``psycopg.errors.UndefinedColumn`` for ``fts_count``
        # (migration 023 not applied) — each warns with a `brain init` hint;
        # search must keep working on a pre-019/pre-023 DB. Other schema errors
        # propagate. CLI searches have no session, so ``session_id=None``
        # (no-click detection is MCP-only).
        record_search_query(
            conn,
            query=query,
            result_count=len(results),
            fts_count=diagnostics.fts_count,
            duration_ms=(
                None
                if diagnostics.total_ms is None
                else round(diagnostics.total_ms)
            ),
            session_id=None,
            source="cli",
            agent_id=resolve_agent_id(agent, cfg),
            payload_tokens=diagnostics.results_tokens,
            baseline_tokens=baseline_tokens,
            tenant_id=cfg.graph_tenant_id,
        )

    if json_output:
        # ``--facets`` implies ``--meta`` under ``--json``: facet data has
        # nowhere else to live. Without either flag this is the pre-F5 path,
        # emitting the bare list unchanged.
        #
        # ``--brief`` is wired on BOTH json branches, not just the bare list:
        # an envelope whose ``results`` stayed full-fat while the bare list
        # went brief would describe the same search two ways — the drift
        # ``search_envelope_json``'s "same call" guarantee exists to prevent.
        # ``cost`` is the embedder built above — ``_build_embedder(cfg)`` is
        # unconditional, so this is never None; under ``BRAIN_EMBEDDER=none``
        # it is a ``NullEmbedder``, which implements ``count_tokens`` too
        # (``count_tokens`` is offline ``tiktoken`` and needs no backend). So the
        # summary-vs-snippet choice is priced in ``cl100k_base`` tokens rather
        # than characters. On the human table path ``--brief`` is a NO-OP.
        if meta or facets:
            emit_json(
                search_envelope_json(
                    query,
                    results,
                    diagnostics,
                    facet_data,
                    brief=brief,
                    cost=embedder.count_tokens,
                )
            )
        else:
            # ``projected`` is the exact list that was priced above — emitting
            # a second, independently-built projection would risk reporting a
            # cost for a payload the caller never got. Non-None on every
            # ``json_output`` path by construction.
            assert projected is not None
            emit_json(projected)
    elif not results:
        typer.echo("(no results)")
    else:
        console.print(search_table(results))

    # Everything below goes to stderr, following ``_warn_if_fts_only_degraded``
    # — so it never pollutes ``--json`` stdout or a redirected table.
    if show_meta:
        typer.echo(search_meta_line(diagnostics, returned=len(results)), err=True)
        if diagnostics.total_documents is None:
            # The count was requested (``want_total`` is implied by
            # ``show_meta``) and failed. The footer already printed
            # ``? matched``; say why rather than leaving a bare question mark.
            typer.echo(
                "match count unavailable — the count query failed; "
                "results above are unaffected",
                err=True,
            )
    if facets:
        if facet_data is not None and facet_data.total_documents > 0:
            _err_console.print(facets_renderable(facet_data))
        else:
            typer.echo(NO_FACETS_MESSAGE, err=True)


def explain(
    query: str = typer.Argument(...),
    limit: int = typer.Option(10, "--limit", "-n", min=1),
    source: str | None = typer.Option(None, "--source"),
    tag: str | None = typer.Option(None, "--tag"),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Lookback window; a bare number is DAYS (e.g. 7 = 7d). "
             "Suffixes: 7d / 24h / 90m.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of the table."
    ),
    fts_only: bool = typer.Option(False, "--fts-only"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    # — Q1-C metadata filters — same set as `brain search` above.
    person: str | None = typer.Option(
        None, "--person",
        help="Match docs where this person participated.",
    ),
    after: datetime | None = typer.Option(
        None, "--after",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs sent/ingested on or after this ISO date.",
    ),
    before: datetime | None = typer.Option(
        None, "--before",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs sent/ingested strictly before this ISO date.",
    ),
    updated_after: datetime | None = typer.Option(
        None, "--updated-after",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs EDITED on or after this ISO date. A different axis "
             "from --after: that filters by the document's own date "
             "(sent/ingested), this by when you last changed it.",
    ),
    updated_before: datetime | None = typer.Option(
        None, "--updated-before",
        formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"],
        help="Only docs EDITED strictly before this ISO date "
             "(see --updated-after for the --before distinction).",
    ),
    kind: str | None = typer.Option(
        None, "--kind",
        help="Filter by documents.content_type "
             "(transcript, email, email_thread, note, ...).",
    ),
    thread: str | None = typer.Option(
        None, "--thread", help="Filter by Gmail thread id.",
    ),
    draft: bool | None = typer.Option(
        None, "--draft/--no-draft",
        help="Include only drafts (--draft) or only published (--no-draft).",
    ),
    has_tag: str | None = typer.Option(
        None, "--has-tag", help="Strict alias for --tag.",
    ),
    without_tag: str | None = typer.Option(
        None, "--without-tag",
        help="Exclude docs carrying this tag.",
    ),
) -> None:
    """Show per-result ranking diagnostics for a query.

    Displays FTS rank, vector cosine, RRF contributions, recency boost, and
    the best-matching chunk for each result.  Use ``--verbose`` to also show
    which filter flags were active.  Use ``--json`` for the full machine-readable
    payload including all :class:`~brain.search.SearchExplanation` fields.
    """
    _validate_source_choice(source)
    effective_tag = _reconcile_tag_flags(tag, has_tag)
    since_days = None if since is None else since_window(since, unit="days")
    cfg = Config.load()
    embedder = _build_embedder(cfg)
    _warn_if_fts_only_degraded(embedder)
    with connect(cfg.database_url) as conn:
        person_match = _resolve_search_person(conn, person)
        results = hybrid_search(
            conn,
            embedder=embedder,
            query=query,
            limit=limit,
            source_kind=source,
            tag=effective_tag,
            since_days=since_days,
            fts_only=fts_only,
            vector_sim_floor=cfg.vector_sim_floor,
            recency_halflife_days=cfg.recency_halflife_days,
            snippet_context_tokens=cfg.snippet_context_tokens,
            snippet_max_chars=cfg.snippet_max_chars,
            explain=True,
            person_keys=person_match.keys if person_match else None,
            person_display_name=(
                person_match.display_name if person_match else None
            ),
            after=after,
            before=before,
            updated_after=updated_after,
            updated_before=updated_before,
            content_type=kind,
            thread_id=thread,
            draft=draft,
            without_tag=without_tag,
        )

    if json_output:
        # The seven public keys come from the one shared projection; ``explain``
        # adds an eighth on top. Key ORDER is preserved (``**base`` first), so
        # the emitted payload is byte-identical to the previous inline literal.
        emit_json(
            [
                {
                    **base,
                    "explain": (
                        {
                            "fts_rank": r.explain.fts_rank,
                            "fts_score": r.explain.fts_score,
                            "fts_rrf_contribution": r.explain.fts_rrf_contribution,
                            "vector_rank": r.explain.vector_rank,
                            "vector_cosine": r.explain.vector_cosine,
                            "vector_rrf_contribution": r.explain.vector_rrf_contribution,
                            "rrf_score": r.explain.rrf_score,
                            "recency_age_days": r.explain.recency_age_days,
                            "recency_boost": r.explain.recency_boost,
                            "final_score": r.explain.final_score,
                            "best_chunk_id": r.explain.best_chunk_id,
                            "best_chunk_index": r.explain.best_chunk_index,
                            "matched_filters": r.explain.matched_filters,
                            "reranker_score": r.explain.reranker_score,
                        }
                        if r.explain is not None
                        else None
                    ),
                }
                for r, base in zip(results, search_results_json(results), strict=True)
            ]
        )
        return
    if not results:
        typer.echo("(no results)")
        return
    console.print(explain_table(results, verbose=verbose))


def register(app: typer.Typer) -> None:
    """Attach the retrieval commands to ``app``.

    Called from ``cli.py`` at the point the commands used to be declared —
    Typer lists commands in registration order, so the position of this call
    is what keeps ``brain --help`` byte-identical.
    """
    app.command()(search)
    app.command()(explain)
