"""`brain recall` — retrieval sized for an agent's context window (F2).

Sibling of `brain search`, not a replacement. `search` ranks pointers for a
human reading a table; `recall` returns the material itself, packed to an
explicit token budget and cited so the reader can attribute a claim back to a
document.

The rendering rule here is small and easy to get wrong: the context block goes
out through plain ``typer.echo``, **never** ``console.print``. Rich parses
``[1]`` as a style tag and raises ``MissingStyle`` on the very citation
markers that make the output useful.
"""
from __future__ import annotations

from datetime import datetime

import psycopg
import typer

from .agent import resolve_agent_id
from .config import Config
from .db import connect
from .durations import since_window
from .errors import PersonAmbiguous, PersonNotFound
from .format import emit_json
from .gaps import record_search_query
from .ingest import Embedder
from .queries import PersonMatch
from .recall import recall as recall_core


def _build_embedder(cfg: Config) -> Embedder:
    """Build the configured embedder via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli._build_embedder(cfg)  # type: ignore[attr-defined]


def resolve_person_to_keys(
    conn: psycopg.Connection, name_or_email: str
) -> PersonMatch:
    """Resolve a person to participant keys via the ``brain.cli`` patch point."""
    from . import cli as _cli

    return _cli.resolve_person_to_keys(conn, name_or_email)


def recall(
    query: str = typer.Argument(..., help="What to recall."),
    budget: int | None = typer.Option(
        None,
        "--budget",
        "-b",
        min=1,
        help="Token budget for the whole emitted block (default: BRAIN_RECALL_BUDGET_TOKENS).",
    ),
    max_candidates: int | None = typer.Option(
        None,
        "--max-candidates",
        min=1,
        help="Cap on documents considered before packing (default: BRAIN_RECALL_MAX_CANDIDATES).",
    ),
    source: str | None = typer.Option(None, "--source"),
    tag: str | None = typer.Option(None, "--tag"),
    since: str | None = typer.Option(
        None, "--since", help="Only documents from the last N days (e.g. 30, 30d)."
    ),
    person: str | None = typer.Option(
        None, "--person", help="Only documents involving this person."
    ),
    after: datetime | None = typer.Option(
        None, "--after", formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]
    ),
    before: datetime | None = typer.Option(
        None, "--before", formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]
    ),
    kind: str | None = typer.Option(
        None, "--kind", help="Only documents of this content type."
    ),
    thread: str | None = typer.Option(None, "--thread"),
    without_tag: str | None = typer.Option(None, "--without-tag"),
    fts_only: bool = typer.Option(
        False, "--fts-only", help="Skip the vector leg (lexical search only)."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of the block."
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        help="Attribute this recall to an agent id. Overrides BRAIN_AGENT_ID.",
    ),
) -> None:
    """Retrieve context sized to fit a token budget, with citations.

    Prints a ``# recall:`` header followed by cited passages, one per matching
    document. The whole block — header included — is guaranteed to fit the
    budget. If the budget cannot hold even the top passage, one truncated
    passage is returned rather than nothing.

    Unlike ``brain search``, this returns the material itself, so it is what
    you want when the next step is reading rather than choosing.
    """
    cfg = Config.load()
    effective_budget = cfg.recall_budget_tokens if budget is None else budget
    effective_candidates = (
        cfg.recall_max_candidates if max_candidates is None else max_candidates
    )
    since_days = None if since is None else since_window(since, unit="days")
    resolved_agent = resolve_agent_id(agent, cfg)
    embedder = _build_embedder(cfg)

    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        person_match: PersonMatch | None = None
        if person is not None:
            try:
                person_match = resolve_person_to_keys(conn, person)
            except (PersonNotFound, PersonAmbiguous) as e:
                raise typer.BadParameter(str(e)) from e

        result = recall_core(
            conn,
            cfg,
            embedder=embedder,
            query=query,
            budget_tokens=effective_budget,
            max_candidates=effective_candidates,
            source_kind=source,
            tag=tag,
            since_days=since_days,
            fts_only=fts_only,
            person_keys=person_match.keys if person_match else None,
            person_display_name=(
                person_match.display_name if person_match else None
            ),
            after=after,
            before=before,
            content_type=kind,
            thread_id=thread,
            without_tag=without_tag,
        )
        # session_id=None: a recall's result IS the content, so no follow-up
        # open will ever arrive and the no_click detector must not mine it as
        # a failure. The fts_count=0 lexical-miss signal stays live.
        record_search_query(
            conn,
            query=query,
            result_count=result.candidates_considered,
            fts_count=result.fts_count,
            session_id=None,
            source="cli",
            agent_id=resolved_agent,
            tenant_id=cfg.graph_tenant_id,
        )

    if json_output:
        emit_json(result.to_dict())
        return

    # Plain echo, never console.print — Rich would read ``[1]`` as a style tag.
    typer.echo(result.context_block())


def register(app: typer.Typer) -> None:
    """Attach ``brain recall`` to ``app``."""
    app.command()(recall)
