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


def _tokens_line(report: UsageReport) -> str | None:
    """The token-cost line, or ``None`` when nothing in the window was priced.

    Rendered as::

        tokens served 148,203 (measured, 412 of 517 calls) · counterfactual
        savings 61,880 over 210 brief calls (−29.4%)

    **Two clauses, two denominators, and the word "counterfactual" on the
    second one.** That wording is the deliverable, not decoration. The first
    clause is a measurement: canonically-serialized payload tokens, over the
    calls that really priced them (canonical, not rendered — migration 028's
    header has the why). The second is a comparison against a call
    that never happened — what those same calls would have cost in the default
    projection — and it is computed over a strictly smaller set of rows.
    Blending them into one headline "we save N%" would be the exact claim this
    wave was built to make impossible: a percentage over calls that never had
    an alternative is marketing, not measurement.

    Returns ``None`` when ``measured_calls`` is 0 — a window with no priced
    call has nothing to say, and "tokens served 0" would read as "retrieval
    was free".
    """
    t = report.totals
    if t.measured_calls == 0:
        return None
    served = t.payload_tokens_total or 0
    parts = [
        f"tokens served {served:,} "
        f"(measured, {t.measured_calls:,} of {t.searches:,} calls)"
    ]
    savings = t.counterfactual_savings_tokens
    if savings is not None:
        # "over 1 brief call", not "over 1 brief calls". The singular is
        # reachable in practice — it is the shape of a brand-new brain, and
        # the first thing anyone sees after their first --brief search.
        noun = "call" if t.counterfactual_calls == 1 else "calls"
        clause = (
            # A negative saving is a real outcome — a cheaper mode that turned
            # out dearer — and it keeps the same U+2212 minus the percentage
            # uses, rather than mixing an ASCII hyphen into the same clause.
            f"counterfactual savings {savings:,}".replace("-", "−")
            + f" over {t.counterfactual_calls:,} brief {noun}"
        )
        rate = t.counterfactual_savings_rate
        if rate is not None:
            # Signed as a DELTA against the baseline, so a cheaper mode reads
            # "−29.4%" and a mode that turned out more expensive reads
            # "+10.0%" instead of being laundered into a saving. U+2212 for
            # the minus, matching the "·" already emitted on the line above.
            delta = f"{-rate * 100:+.1f}"
            # ...but a magnitude that RENDERS as zero has no direction, and
            # `format(-0.0, "+.1f")` is "-0.0" — which the U+2212 swap below
            # then turns into "−0.0%", a minus sign in front of a saving of
            # nothing. A reader reports that as a bug in the measurement, not
            # in the formatter. Two ways in, both closed here: a saving of
            # exactly 0 (the counterfactual mode cost precisely what the
            # default would have), and a real saving too small to survive
            # rounding to a tenth. The magnitude, not the raw rate, decides —
            # so "+0.0%" cannot appear either.
            if float(delta) == 0:
                delta = "0.0"
            clause += f" ({delta}%)".replace("-", "−")
        parts.append(clause)
    return " · ".join(parts)


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
    tokens_line = _tokens_line(report)
    if tokens_line is not None:
        typer.echo(f"  {tokens_line}")
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
