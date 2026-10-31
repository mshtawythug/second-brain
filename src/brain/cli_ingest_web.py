"""`brain ingest-web` — revisited and bookmarked pages on allowlisted hosts (RM-41).

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so
``cli.py`` is not touched. The store read is :mod:`brain.ingest.browser_store`,
the page selection and document shape :mod:`brain.ingest.browser`, the opt-in
fetch :mod:`brain.ingest.web_fetch`; this module checks the scope, runs the
ingest loop and prints the report.

**Scope (protocol point 5).** ``--browser chrome|safari`` and ``--since
YYYY-MM-DD`` are required, and ``$BRAIN_HOME/web-allowlist.txt``
(:mod:`brain.settings.web_allowlist`) must list at least one host: with no file,
an empty one or a malformed line, nothing is read and the command exits 2. The
allowlist is the default-deny boundary; there is no "all history".

**The profile.** ``--profile DIR`` names the directory holding the store;
without it the macOS default under :data:`home_dir` is used, and any other
platform must pass it (RM-41.D9).

**Re-runs (protocol point 3, RM-41.D3).** A page whose ``(web, <url hash>)`` row
exists gets only its volatile metadata patched (``refresh_volatile``): no
re-chunk, no embed, no fetch, and the stored title and body stay. ``--refresh``
rebuilds the body instead, keeping the other browser's keys and the first
``date`` (:func:`brain.ingest.browser.with_stored_keys`).

**Sensitivity (sec-114 F11).** A new page is stored at ``--sensitivity``
(default ``normal``). For a page already stored, an explicit
``--sensitivity confidential`` raises it to confidential, mirror and fences
included (``set_document_sensitivity`` + ``propagate_sensitivity_to_vault``, the
``brain mark-confidential`` path); without the flag the stored tier is left
alone. A run never LOWERS a stored tier: an explicit ``--sensitivity normal``
on a confidential page prints a notice naming ``brain mark-normal`` instead,
the escalate-only rule every ingest path follows.

**Interruption (sec-114 F10).** The store read runs with SIGTERM and SIGHUP
raising ``SystemExit``, so a closed terminal still removes the snapshot, and
each run first sweeps this user's stale snapshot directories.

**Cost (protocol point 9).** ``web_page`` skips concept extraction; the summary
runs only with ``--fetch`` (a title-and-URL body is too short to summarize);
at most ``--max`` pages a run.

**Privacy.** Nothing here logs. URLs and titles reach only the terminal the
owner ran the command in, through ``one_line``. Errors are fixed messages or a
class name (``describe_failure``).
"""
from __future__ import annotations

import functools
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as day_start
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import psycopg
import typer

from .cli_ingest import (
    _ALLOW_SECRETS_HELP,
    _build_embedder,
    _build_enricher,
    _build_graph_syncer,
    _emit_egress_notice,
    _emit_secret_notice,
    _validate_sensitivity_or_exit,
)
from .config import Config, ConfigError, _brain_home_root
from .db import connect
from .errors import SecretGuardError, describe_failure
from .ingest import Embedder, browser_store, ingest_document, web_fetch
from .ingest import browser as br
from .ingest.browser_store import (
    BROWSERS,
    Browser,
    BrowserRows,
    BrowserStoreError,
    default_profile,
    read_browser,
    sweep_stale_snapshots,
)
from .ingest.capped_process import sigterm_as_system_exit
from .ingest.text_safety import one_line
from .ingest.volatile_patch import rare_outcomes, refresh_volatile, settle, stored_document
from .queries import set_document_sensitivity
from .sensitivity import DEFAULT_SENSITIVITY, is_confidential
from .settings.web_allowlist import WebAllowlist, load_web_allowlist
from .tags import normalize_tags
from .vault.sensitivity_propagate import propagate_sensitivity_to_vault

if TYPE_CHECKING:
    from .enrichment import OllamaEnricher
    from .graph_rag.sync import GraphSyncer

DEFAULT_MAX = 500
_COMMAND = "ingest-web"
SCOPE_REFUSAL = (
    "ingest-web requires --browser chrome|safari and --since YYYY-MM-DD; "
    "whole-history import is not offered"
)
NO_ALLOWLIST = (
    "ingest-web: no allowlist: list the hosts to ingest, one per line, in "
    "$BRAIN_HOME/web-allowlist.txt (docs/guides/browser-history.md); nothing is ingested "
    "without it"
)
EMPTY_ALLOWLIST = (
    "ingest-web: $BRAIN_HOME/web-allowlist.txt lists no hosts; nothing is ingested without one"
)
NEEDS_PROFILE = "ingest-web: the default profile location is macOS-only; pass --profile DIR"

#: Where the default profile is looked for; tests swap it (``tests/browser_isolation_plugin``).
home_dir: Callable[[], Path] = Path.home
#: The platform the default profile is checked against; tests swap it.
host_platform: str = sys.platform
#: The ``--fetch`` resolver and client factory (one client per fetch); tests swap both,
#: so no test reaches the network.
fetch_resolver: web_fetch.Resolver = web_fetch.system_resolver
make_fetch_client: web_fetch.ClientFactory = web_fetch.make_client


def ingest_web(
    browser: str | None = typer.Option(
        None, "--browser", help="chrome or safari (required)."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Count visits on or after this day, YYYY-MM-DD (required)."
    ),
    profile: Path | None = typer.Option(
        None, "--profile", help="The browser profile directory (default: the macOS location)."
    ),
    min_visits: int = typer.Option(
        br.DEFAULT_MIN_VISITS, "--min-visits", min=1,
        help="Visits since --since that make an unbookmarked page qualify.",
    ),
    max_pages: int = typer.Option(
        DEFAULT_MAX, "--max", min=1, help="At most this many pages a run, newest visit first."
    ),
    fetch: bool = typer.Option(
        False, "--fetch",
        help="Download the text of NEW pages (https, allowlisted, public addresses only).",
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Rebuild the body of pages already stored (and re-fetch)."
    ),
    sensitivity: str | None = typer.Option(
        None,
        "--sensitivity",
        help="normal (default) or confidential; confidential also raises stored pages.",
    ),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to each page."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be ingested; write nothing."
    ),
    enrich: bool = typer.Option(
        True, "--enrich/--no-enrich", help="Add the Ollama auto-summary (with --fetch only)."
    ),
    allow_secrets: bool = typer.Option(False, "--allow-secrets", help=_ALLOW_SECRETS_HELP),
) -> None:
    """Ingest revisited and bookmarked pages of allowlisted hosts (Chrome or Safari).

    Requires --browser, --since and $BRAIN_HOME/web-allowlist.txt. Stores one
    document per page URL, with the query string and fragment dropped.
    """
    chosen, first_day = _scope_or_exit(browser, since, today=datetime.now().date())
    level = _validate_sensitivity_or_exit(sensitivity or DEFAULT_SENSITIVITY)
    allowlist = _allowlist_or_exit()
    store = _profile_or_exit(chosen, profile)
    window_start = datetime.combine(first_day, day_start.min).astimezone(UTC)
    sweep_stale_snapshots()
    try:
        with sigterm_as_system_exit(also=(signal.SIGHUP,)):
            rows = read_browser(chosen, store, window_start)
    except BrowserStoreError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from None
    batch = br.select_pages(rows, allowlist, min_visits=min_visits, limit=max_pages)
    _report_read(rows, batch, min_visits=min_visits, limit=max_pages)
    if dry_run:
        for page in batch.pages:
            typer.echo(f"  {page.host}  {one_line(page.title)}")
        return
    _ingest_all(
        batch,
        _Options(
            browser=chosen,
            allowlist=allowlist,
            tags=normalize_tags(list(tag)),
            sensitivity=level,
            explicit_sensitivity=sensitivity is not None,
            fetch=fetch,
            refresh=refresh,
            enrich=enrich and fetch,
            allow_secrets=allow_secrets,
        ),
    )


def _scope_or_exit(browser: str | None, since: str | None, *, today: date) -> tuple[Browser, date]:
    if browser is None or since is None:
        _refuse(SCOPE_REFUSAL)
    if browser not in BROWSERS:
        _refuse(f"{_COMMAND}: --browser must be chrome or safari")
    try:
        first = date.fromisoformat(since)
    except ValueError:
        _refuse(f"{_COMMAND}: --since must be YYYY-MM-DD")
    if first > today:
        _refuse(f"{_COMMAND}: --since is in the future")
    return browser, first


def _allowlist_or_exit() -> WebAllowlist:
    try:
        allowlist = load_web_allowlist(_brain_home_root())
    except ConfigError as exc:
        _refuse(f"{_COMMAND}: {exc}")
    if allowlist is None:
        _refuse(NO_ALLOWLIST)
    if allowlist.is_empty:
        _refuse(EMPTY_ALLOWLIST)
    return allowlist


def _profile_or_exit(browser: Browser, profile: Path | None) -> Path:
    if profile is not None:
        return profile.expanduser()
    if host_platform != "darwin":
        _refuse(NEEDS_PROFILE)
    return default_profile(browser, home_dir())


def _refuse(message: str) -> NoReturn:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=2)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _report_read(rows: BrowserRows, batch: br.PageBatch, *, min_visits: int, limit: int) -> None:
    typer.echo(f"found {_plural(len(batch.pages) + batch.remainder, 'page', 'pages')} to keep")
    notes = (
        (batch.off_allowlist, "off the allowlist skipped"),
        (batch.unusable, "not http(s) skipped"),
        (rows.malformed, "could not be read"),
    )
    for count, what in notes:
        if count:
            typer.echo(f"{_plural(count, 'entry', 'entries')} {what}")
    if batch.below_threshold:
        typer.echo(
            f"{_plural(batch.below_threshold, 'page', 'pages')} below --min-visits "
            f"{min_visits} skipped"
        )
    if rows.bookmarks_truncated:
        typer.secho(
            f"stopped reading bookmarks at {browser_store.MAX_BOOKMARK_NODES} entries",
            fg="yellow",
        )
    if rows.truncated:
        typer.secho(
            f"stopped reading at {browser_store.MAX_ROWS} history entries; narrow --since",
            fg="yellow",
        )
    if batch.remainder:
        typer.secho(
            f"stopped at the --max cap of {limit}: {batch.remainder} more page(s) qualify; "
            "raise --max or narrow --since",
            fg="yellow",
        )


@dataclass(frozen=True)
class _Options:
    browser: Browser
    allowlist: WebAllowlist
    tags: list[str]
    sensitivity: str
    explicit_sensitivity: bool
    fetch: bool
    refresh: bool
    enrich: bool
    allow_secrets: bool


@dataclass
class _Run:
    """What every page of one run is written with, and the run's counts."""

    options: _Options
    cfg: Config
    embedder: Embedder
    enricher: OllamaEnricher | None
    graph_syncer: GraphSyncer
    counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("ingested", "updated", "refreshed", "unchanged", "empty", "same_body", "failed",
             "fetch_failed"),
            0,
        )
    )


def _ingest_all(batch: br.PageBatch, options: _Options) -> None:
    cfg = Config.load()
    run = _Run(
        options=options,
        cfg=cfg,
        embedder=_build_embedder(cfg),
        enricher=_build_enricher(cfg) if options.enrich else None,
        graph_syncer=_build_graph_syncer(cfg),
    )
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for page in batch.pages:
            _guarded(page, run, functools.partial(_write_page, conn, page, run))
    _report_counts(run.counts)


def _report_counts(counts: dict[str, int]) -> None:
    fetch_part = f", {counts['fetch_failed']} fetch failed" if counts["fetch_failed"] else ""
    typer.echo(
        f"{counts['ingested']} ingested, {counts['updated']} updated, "
        f"{counts['refreshed']} refreshed, {counts['unchanged']} unchanged, "
        f"{counts['failed']} failed{rare_outcomes(counts, 'page')}{fetch_part}"
    )


def _guarded(page: br.Page, run: _Run, step: Callable[[], str]) -> None:
    """Write one page; a failure is counted and printed, never fatal."""
    label = one_line(page.url)
    try:
        outcome = step()
    except SecretGuardError as exc:
        # Per page, like ingest-gmail's per thread. The guard's message masks the finding.
        typer.secho(f"  refused {label}", fg="red", err=True)
        typer.secho(str(exc), fg="red", err=True)
        outcome = "failed"
    except (psycopg.Error, ValueError, KeyError) as exc:
        typer.secho(f"  failed {label}: {describe_failure(exc)}", fg="red", err=True)
        outcome = "failed"
    run.counts[outcome] += 1
    if outcome in ("ingested", "updated", "refreshed"):
        typer.echo(f"  {outcome} {label}")


def _write_page(conn: psycopg.Connection[Any], page: br.Page, run: _Run) -> str:
    """``ingested|updated|refreshed|unchanged|empty|same_body`` for one page."""
    options = run.options
    stored = stored_document(conn, br.SOURCE_KIND, page.external_id)
    if stored is not None and not options.refresh:
        outcome = refresh_volatile(
            conn,
            document_id=stored[0],
            stored=stored[1],
            metadata=br.volatile_metadata(page, options.browser),
            keys=br.volatile_keys(options.browser),
            vault_root=run.cfg.vault_path,
            graph_syncer=run.graph_syncer,
        )
        return "refreshed" if _apply_explicit_sensitivity(conn, stored[0], page, run) else outcome
    doc = br.build_document(page, options.browser, fetched_text=_fetched_text(page, run))
    if stored is not None:
        doc = br.with_stored_keys(doc, stored[1], options.browser)
    result = ingest_document(
        conn,
        embedder=run.embedder,
        doc=doc,
        source_kind=br.SOURCE_KIND,
        source_external_id=page.external_id,
        source_metadata={"host": page.host, "url": page.url},
        tags=options.tags,
        vault_root=run.cfg.vault_path,
        enricher=run.enricher,
        enrich=run.enricher is not None,
        enrich_min_tokens=run.cfg.enrich_min_tokens,
        graph_syncer=run.graph_syncer,
        secret_guard=run.cfg.secret_guard,
        allow_secrets=options.allow_secrets,
        sensitivity=options.sensitivity,
    )
    _emit_secret_notice(result.secret_notice)
    _emit_egress_notice(result.egress_notice)
    return settle(
        conn,
        result,
        stored=stored,
        metadata=doc.metadata,
        keys=br.volatile_keys(options.browser),
        vault_root=run.cfg.vault_path,
        graph_syncer=run.graph_syncer,
    )


def _apply_explicit_sensitivity(
    conn: psycopg.Connection[Any], document_id: str, page: br.Page, run: _Run
) -> bool:
    """Raise a stored page to an explicit ``--sensitivity confidential``; ``True`` if changed.

    Escalate only (module docstring): an explicit ``normal`` leaves a confidential page
    as it is and says how to lower it.
    """
    options = run.options
    if not options.explicit_sensitivity:
        return False
    if not is_confidential(options.sensitivity):
        row = conn.execute(
            "SELECT sensitivity FROM documents WHERE id = %s", (document_id,)
        ).fetchone()
        if row is not None and is_confidential(row[0]):
            typer.echo(
                f"  kept confidential {one_line(page.url)} "
                f"(lower it with: brain mark-normal {document_id[:8]})"
            )
        return False
    if not set_document_sensitivity(conn, document_id=document_id, level=options.sensitivity):
        return False
    for failure in propagate_sensitivity_to_vault(
        conn, document_id, level=options.sensitivity, vault_root=run.cfg.vault_path
    ):
        typer.secho(f"  mirror not updated {one_line(page.url)}: {failure.message}",
                    fg="yellow", err=True)
    typer.echo(f"  marked confidential {one_line(page.url)}")
    return True


def _fetched_text(page: br.Page, run: _Run) -> str | None:
    """The page's text under ``--fetch``; ``None`` (title-and-URL body) when the fetch fails."""
    if not run.options.fetch:
        return None
    try:
        return web_fetch.fetch_page(
            page.url,
            run.options.allowlist,
            client_factory=make_fetch_client,
            resolver=fetch_resolver,
        )
    except web_fetch.WebFetchError as exc:
        typer.secho(f"  fetch skipped {one_line(page.url)}: {exc}", fg="yellow", err=True)
        run.counts["fetch_failed"] += 1
        return None


def register(app: typer.Typer) -> None:
    """Attach ``brain ingest-web`` to ``app`` (through ``cli_registry``)."""
    app.command(name="ingest-web")(ingest_web)

