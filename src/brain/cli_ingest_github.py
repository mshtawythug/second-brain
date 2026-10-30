"""`brain ingest-github` — the owner's pull requests, issues and READMEs (RM-31).

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so
``cli.py`` is not touched. The ``gh`` reads live in :mod:`brain.ingest.github`
and the document shapes in :mod:`brain.ingest.github_docs`; this module checks
the scope, applies the cap, runs the ingest loop and prints the report.

**Scope (protocol point 5).** At least one of ``--repo OWNER/REPO`` or
``--mine``, and ``--since``; no account is ever bulk-imported. Every item read
is the owner's: author, assignee or reviewer. ``--mine`` alone reads that
activity everywhere; ``--repo`` narrows it to the named repositories, and never
reads their other contributors' items (the #100 review ruling).

**The cap (protocol point 9).** At most :data:`MAX_ITEMS` documents a run: PRs
and issues first, newest ``updatedAt`` first, then READMEs in the budget left.
The cut is reported. Every new document is summarized (``--no-enrich`` skips
it) and, with the graph on, concept-extracted at minutes per document, so a
full run occupies the graph lane for days; start with a short ``--since``.

**READMEs** (plan ruling R1): each ``--repo`` repository's, and under
``--mine`` those of the result repositories the authenticated user owns.

**Re-runs (protocol point 3).** ``ingest_document`` deduplicates by ``(github,
<owner>/<repo>#<n>)`` or ``(github, <owner>/<repo>:README)``. A hit whose stored
``updated_at``, ``with_thread`` and ``repo_private`` all match is ``unchanged``
before any per-item read. Otherwise an unchanged body is a no-op there, and the
volatile keys (``github_docs.VOLATILE_KEYS``) are applied with
``update_document(metadata_patch=…)``, which neither re-chunks nor embeds.

**Sensitivity.** A document from a repository that is not public, or whose
privacy could not be read (fail closed), is stored ``confidential``.

**Errors.** A :class:`~brain.ingest.github.GithubError` message quotes no ``gh``
output (that module's docstring), so it is printed as it is; any other failure
is printed by class only (``describe_failure``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import typer

from .cli_ingest import (
    _ALLOW_SECRETS_HELP,
    _build_embedder,
    _build_enricher,
    _build_graph_syncer,
    _emit_egress_notice,
    _emit_secret_notice,
)
from .config import Config
from .db import connect
from .errors import SecretGuardError, describe_failure
from .ingest import Embedder, ingest_document, update_document
from .ingest import github as gh
from .ingest.github import GithubError, SearchHit, SearchScope
from .ingest.github_docs import (
    VOLATILE_KEYS,
    GithubDoc,
    build_item_document,
    build_readme_document,
    one_line,
)
from .sensitivity import CONFIDENTIAL, DEFAULT_SENSITIVITY
from .tags import normalize_tags

if TYPE_CHECKING:
    from .enrichment import OllamaEnricher
    from .graph_rag.sync import GraphSyncer

MAX_ITEMS = 500
SCOPE_REFUSAL = "ingest-github requires --repo or --mine, and --since"

#: The production runner; tests swap it for a fake (``monkeypatch.setattr``).
github_gh_runner: gh.GhRunner = gh.gh_runner


def ingest_github(
    repo: list[str] = typer.Option(
        [], "--repo", help="Only your items in OWNER/REPO (repeatable); also its README."
    ),
    mine: bool = typer.Option(
        False, "--mine", help="Your items everywhere (author, assignee or reviewer)."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Items updated on or after this day (YYYY-MM-DD)."
    ),
    with_thread: bool = typer.Option(
        False, "--with-thread", help="Keep other people's comments too (default: yours only)."
    ),
    readmes: bool = typer.Option(True, "--readmes/--no-readmes", help="Also ingest READMEs."),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to each document."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be read; write nothing."
    ),
    enrich: bool = typer.Option(True, "--enrich/--no-enrich", help="Add the Ollama auto-summary."),
    allow_secrets: bool = typer.Option(False, "--allow-secrets", help=_ALLOW_SECRETS_HELP),
) -> None:
    """Ingest your pull requests, issues and READMEs via the `gh` CLI.

    Requires --repo or --mine, and --since. At most 500 documents a run.

    Keeps only your own comments unless --with-thread. Stores items from
    private repositories as confidential.
    """
    scope = _scope_or_exit(repo, mine, since, today=datetime.now(UTC).date())
    try:
        login = gh.viewer_login(runner=github_gh_runner)
        hits = gh.search_items(scope, limit=MAX_ITEMS + 1, runner=github_gh_runner)
    except GithubError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=1) from exc
    readme_repos = _readme_repos(scope, hits, login) if readmes else []
    hits, readme_repos = _apply_cap(hits, readme_repos)
    typer.echo(f"found {len(hits)} item(s) and {len(readme_repos)} README(s)")
    if dry_run:
        for hit in hits:
            title = one_line(str(hit.raw.get("title") or ""))
            typer.echo(f"  {hit.kind:<6} {hit.external_id}  {title}")
        for name in readme_repos:
            typer.echo(f"  readme {name}")
        return
    _ingest_all(
        hits,
        readme_repos,
        login=login,
        # Tags are canonical at every write boundary; ingest_document
        # normalizes only when it merges into an existing row.
        tags=normalize_tags(list(tag)),
        with_thread=with_thread,
        enrich=enrich,
        allow_secrets=allow_secrets,
    )


def _scope_or_exit(
    repos: list[str], mine: bool, since: str | None, *, today: date
) -> SearchScope:
    if since is None or not (repos or mine):
        _refuse(SCOPE_REFUSAL)
    assert since is not None  # _refuse exits
    try:
        first = date.fromisoformat(since)
    except ValueError:
        _refuse(f"{SCOPE_REFUSAL}: --since must be YYYY-MM-DD")
    if first > today:
        _refuse(f"{SCOPE_REFUSAL}: --since is in the future")
    for name in repos:
        if not gh.is_repo_name(name):
            _refuse(f"{SCOPE_REFUSAL}: --repo must be OWNER/REPO")
    return SearchScope(repos=tuple(dict.fromkeys(repos)), mine=mine, since=first)


def _refuse(message: str) -> None:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=2)


def _readme_repos(scope: SearchScope, hits: list[SearchHit], login: str) -> list[str]:
    """Plan ruling R1: the named repositories, and under ``--mine`` the owner's own."""
    named = list(scope.repos)
    if scope.mine:
        owned = (h.repo for h in hits if h.repo.split("/")[0].lower() == login.lower())
        named += sorted(set(owned) - set(named))
    return named


def _apply_cap(
    hits: list[SearchHit], readme_repos: list[str]
) -> tuple[list[SearchHit], list[str]]:
    if len(hits) > MAX_ITEMS:
        typer.secho(
            f"stopped at the {MAX_ITEMS}-item cap: more than {MAX_ITEMS} matched; "
            "narrow --since or --repo",
            fg="yellow",
        )
        hits = hits[:MAX_ITEMS]
    budget = MAX_ITEMS - len(hits)
    if len(readme_repos) > budget:
        typer.secho(
            f"stopped at the {MAX_ITEMS}-item cap: {len(readme_repos)} README(s) are due, "
            f"{budget} fit; narrow --since or --repo",
            fg="yellow",
        )
        readme_repos = readme_repos[:budget]
    return hits, readme_repos


@dataclass
class _Run:
    """What every document of one run is ingested with, and the run's counts."""

    cfg: Config
    login: str
    tags: list[str]
    with_thread: bool
    embedder: Embedder
    enricher: OllamaEnricher | None
    graph_syncer: GraphSyncer
    allow_secrets: bool
    private: dict[str, bool] = field(default_factory=dict)
    counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("ingested", "updated", "refreshed", "unchanged", "failed"), 0
        )
    )


def _ingest_all(
    hits: list[SearchHit],
    readme_repos: list[str],
    *,
    login: str,
    tags: list[str],
    with_thread: bool,
    enrich: bool,
    allow_secrets: bool,
) -> None:
    cfg = Config.load()
    run = _Run(
        cfg=cfg,
        login=login,
        tags=tags,
        with_thread=with_thread,
        embedder=_build_embedder(cfg),
        enricher=_build_enricher(cfg) if enrich else None,
        graph_syncer=_build_graph_syncer(cfg),
        allow_secrets=allow_secrets,
    )
    unreadable = 0
    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        for hit in hits:
            _guarded(hit.external_id, run, lambda h=hit: _ingest_hit(conn, h, run))
        for name in readme_repos:
            readme = gh.fetch_readme(name, runner=github_gh_runner)
            if readme is None:
                unreadable += 1
                continue
            gd = build_readme_document(name, readme, confidential=_is_private(name, run))
            _guarded(
                gd.external_id,
                run,
                lambda d=gd: _write(conn, d, run, stored=_stored_metadata(conn, d.external_id)),
            )
    if unreadable:
        typer.echo(f"{unreadable} README(s) could not be read (none, or the read failed)")
    c = run.counts
    typer.echo(
        f"{c['ingested']} ingested, {c['updated']} updated, {c['refreshed']} refreshed, "
        f"{c['unchanged']} unchanged, {c['failed']} failed"
    )


def _guarded(external_id: str, run: _Run, step: Any) -> None:
    """Run one document's ingest; a failure is counted and printed, never fatal."""
    try:
        outcome = step()
    except SecretGuardError as exc:
        # Per document, like ingest-gmail's per thread: one false positive
        # must not abort the run. The guard's own message masks the finding.
        typer.secho(f"  refused {external_id}", fg="red", err=True)
        typer.secho(str(exc), fg="red", err=True)
        outcome = "failed"
    except GithubError as exc:
        typer.secho(f"  failed {external_id}: {exc}", fg="red", err=True)
        outcome = "failed"
    except (psycopg.Error, ValueError, KeyError) as exc:
        typer.secho(f"  failed {external_id}: {describe_failure(exc)}", fg="red", err=True)
        outcome = "failed"
    run.counts[outcome] += 1
    if outcome not in ("unchanged", "failed"):
        typer.echo(f"  {outcome} {external_id}")


def _is_private(repo: str, run: _Run) -> bool:
    """Whether ``repo`` is not public; an unreadable one counts as private (fail closed)."""
    if repo not in run.private:
        try:
            run.private[repo] = gh.fetch_repo(repo, runner=github_gh_runner)
        except GithubError as exc:
            typer.secho(
                f"could not read {repo} ({exc}); its documents are stored confidential",
                fg="yellow",
                err=True,
            )
            run.private[repo] = True
    return run.private[repo]


def _ingest_hit(conn: psycopg.Connection[Any], hit: SearchHit, run: _Run) -> str:
    confidential = _is_private(hit.repo, run)
    stored = _stored_metadata(conn, hit.external_id)
    if stored is not None and (
        stored[1].get("updated_at") == (hit.updated_at or None)
        and stored[1].get("with_thread") == run.with_thread
        and stored[1].get("repo_private") == confidential
    ):
        return "unchanged"  # nothing moved on the code host: no per-item read
    item = gh.fetch_item(hit, runner=github_gh_runner)
    gd = build_item_document(
        item, owner_login=run.login, confidential=confidential, with_thread=run.with_thread
    )
    return _write(conn, gd, run, stored=stored)


def _write(
    conn: psycopg.Connection[Any],
    gd: GithubDoc,
    run: _Run,
    *,
    stored: tuple[str, dict[str, Any]] | None = None,
) -> str:
    """Ingest one document; return ``ingested|updated|refreshed|unchanged``."""
    result = ingest_document(
        conn,
        embedder=run.embedder,
        doc=gd.doc,
        source_kind=gh.SOURCE_KIND,
        source_external_id=gd.external_id,
        source_metadata={"repo": gd.doc.metadata["repo"]},
        tags=run.tags,
        vault_root=run.cfg.vault_path,
        enricher=run.enricher,
        enrich=run.enricher is not None,
        enrich_min_tokens=run.cfg.enrich_min_tokens,
        graph_syncer=run.graph_syncer,
        secret_guard=run.cfg.secret_guard,
        allow_secrets=run.allow_secrets,
        sensitivity=CONFIDENTIAL if gd.confidential else DEFAULT_SENSITIVITY,
    )
    _emit_secret_notice(result.secret_notice)
    _emit_egress_notice(result.egress_notice)
    if result.created or result.body_changed:
        return "ingested" if result.created else "updated"
    assert result.document_id is not None  # every non-replace path returns the row id
    stored_meta = stored[1] if stored is not None else {}
    patch = {
        key: gd.doc.metadata[key]
        for key in (*VOLATILE_KEYS, "with_thread", "repo_private")
        if key in gd.doc.metadata and stored_meta.get(key) != gd.doc.metadata[key]
    }
    if not patch:
        return "unchanged"
    # Metadata only: no new_content and no embedder, so no re-chunk and no embed.
    update_document(
        conn,
        document_id=result.document_id,
        metadata_patch=patch,
        vault_root=run.cfg.vault_path,
        graph_syncer=run.graph_syncer,
    )
    return "refreshed"


def _stored_metadata(
    conn: psycopg.Connection[Any], external_id: str
) -> tuple[str, dict[str, Any]] | None:
    """``(document id, metadata)`` of the stored github document, or ``None``."""
    row = conn.execute(
        "SELECT d.id::text, d.metadata FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE s.kind = %s AND s.external_id = %s",
        (gh.SOURCE_KIND, external_id),
    ).fetchone()
    return None if row is None else (str(row[0]), row[1] or {})


def register(app: typer.Typer) -> None:
    """Attach ``brain ingest-github`` to ``app`` (through ``cli_registry``)."""
    app.command(name="ingest-github")(ingest_github)
