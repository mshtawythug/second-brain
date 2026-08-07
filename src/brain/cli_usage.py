"""`brain usage` — is this brain being used, and by whom (F7).

Rendering only; every number comes from :func:`brain.usage.build_usage_report`.

The one judgement encoded here is the ``--json`` default: raw query strings
are **withheld** unless ``--raw-queries`` is passed. A query log is often more
revealing than any single document it found — it records what someone was
looking for, including the searches that returned nothing. Counts are
identical either way; only the label changes.
"""
from __future__ import annotations

import psycopg
import typer
from rich.table import Table

from .config import Config
from .db import connect
from .format import console, emit_json
from .gaps import search_queries_schema_hint
from .usage import UsageReport, build_usage_report

#: Rows of the daily table shown in human output. The JSON carries every day;
#: a 90-day window rendered in full would bury the summary it sits under.
_DAILY_ROWS_SHOWN = 14


def _totals_line(report: UsageReport) -> str:
    """The headline summary, above the tables."""
    t = report.totals
    rate = f"{t.zero_result_rate * 100:.1f}%"
    parts = [
        f"searches {t.searches}",
        f"opens {t.opens}",
        f"docs ingested {t.documents_ingested}",
        f"sessions {t.sessions}",
        f"feedback {t.feedback}",
        f"zero-result {t.zero_result} ({rate})",
    ]
    if t.duration_p50_ms is not None:
        parts.append(f"latency p50 {t.duration_p50_ms:.0f}ms")
    if t.duration_p95_ms is not None:
        parts.append(f"p95 {t.duration_p95_ms:.0f}ms")
    return "  ·  ".join(parts)


def _daily_table(report: UsageReport) -> Table:
    table = Table(
        title=f"Activity by day (last {_DAILY_ROWS_SHOWN} shown)",
        title_justify="left",
    )
    table.add_column("Day")
    for name in ("Searches", "Sessions", "Opens", "Zero-result"):
        table.add_column(name, justify="right")
    for day in report.daily[:_DAILY_ROWS_SHOWN]:
        table.add_row(
            day.day.isoformat(),
            str(day.searches),
            str(day.sessions),
            str(day.opens),
            str(day.zero_result),
        )
    return table


def _surface_table(report: UsageReport) -> Table:
    table = Table(title="By surface", title_justify="left")
    table.add_column("Surface")
    for name in ("Searches", "Opens", "Feedback"):
        table.add_column(name, justify="right")
    for row in report.by_surface:
        table.add_row(
            row.surface, str(row.searches), str(row.opens), str(row.feedback)
        )
    return table


def _agent_table(report: UsageReport) -> Table:
    table = Table(title="By agent", title_justify="left")
    table.add_column("Agent")
    for name in ("Searches", "Opens", "Feedback"):
        table.add_column(name, justify="right")
    for row in report.by_agent:
        # ``label`` renders a NULL agent_id as "(unattributed)" — an honest
        # bucket rather than a fabricated 'cli' agent.
        table.add_row(
            row.label, str(row.searches), str(row.opens), str(row.feedback)
        )
    return table


def _queries_table(report: UsageReport) -> Table:
    table = Table(title="Top queries", title_justify="left")
    table.add_column("Query")
    table.add_column("Count", justify="right")
    for row in report.top_queries:
        table.add_row(row.query, str(row.count))
    return table


def _sources_table(report: UsageReport) -> Table:
    table = Table(title="Ingested by source", title_justify="left")
    table.add_column("Source")
    table.add_column("Documents", justify="right")
    for row in report.ingested_by_source:
        table.add_row(row.label, str(row.count))
    return table


def usage(
    days: int = typer.Option(30, "--days", "-d", min=1, help="Lookback window in days."),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of the tables."
    ),
    raw_queries: bool = typer.Option(
        False,
        "--raw-queries",
        help=(
            "Include raw query strings in --json output "
            "(default: normalized labels only)."
        ),
    ),
    limit: int = typer.Option(
        10, "--limit", "-n", min=1, help="Rows per top-N section."
    ),
) -> None:
    """Show how much this brain is being read from and written to.

    Counts searches, document opens, feedback events and ingests over the
    trailing window, broken down by day, by surface (cli / mcp / wiki) and by
    agent. A document with no ``agent_id`` is reported as ``(unattributed)``
    rather than being folded into a surface — every pre-027 row is genuinely
    unattributed, and saying so is more useful than guessing.

    ``--json`` withholds raw query strings by default; pass ``--raw-queries``
    to include them.
    """
    cfg = Config.load()
    try:
        with connect(cfg.database_url) as conn:
            report = build_usage_report(
                conn, days=days, tenant_id=cfg.graph_tenant_id, limit=limit
            )
    except psycopg.Error as e:
        # A missing table/column means a migration has not been applied. Fail
        # loudly with the actionable hint rather than reporting confident
        # zeroes — unlike the telemetry WRITE path, where swallowing keeps the
        # daily-driver search alive, a silently incomplete REPORT is worse
        # than no report.
        hint = search_queries_schema_hint(e)
        if hint is None:
            raise
        typer.secho(hint, fg="red", err=True)
        raise typer.Exit(code=1) from e

    if json_output:
        emit_json(report.to_dict(raw_queries=raw_queries))
        return

    typer.echo(f"Brain usage — last {days} day(s)")
    typer.echo(f"  {_totals_line(report)}")
    typer.echo("")
    for build in (
        _daily_table,
        _surface_table,
        _agent_table,
        _queries_table,
        _sources_table,
    ):
        console.print(build(report))


def register(app: typer.Typer) -> None:
    """Attach ``brain usage`` to ``app``."""
    app.command()(usage)
