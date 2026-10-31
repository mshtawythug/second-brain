"""`brain ingest-code` — the owner's own files from one local git work tree (RM-43).

Registered through :data:`brain.cli_registry.EXTENSION_REGISTRARS` (RM-06), so
``cli.py`` is not touched. :mod:`brain.ingest.code_repo` asks ``git`` which
tracked files the owner touched, :mod:`brain.ingest.code_files` excludes and
reads them, and this module runs the ingest loop and reports.

**Scope (protocol point 5).** ``--author-email`` (repeatable) and ``--since`` are
required; only files a commit by one of those addresses touched since that day
are read. ``--ext`` narrows further, and a ``<repo-dir>`` below the top level
narrows to the files under it.

**Each file** is one document, ``content_type="code"`` (chunked at definition
boundaries by ``chunk_dispatch.chunk_for``), keyed by its absolute
``source_path``, so a re-run updates it in place. **The secret guard always runs
in ``reject`` mode here** (RM-43.D1), whatever ``BRAIN_SECRET_GUARD`` says, and
there is no ``--allow-secrets``: a key committed to a repository is the classic
leak. A refused file is printed with its finding kinds and line numbers (never
the match) and summarized on its own line. The default sensitivity is
``confidential`` (an employer's code); summaries are off unless ``--enrich``,
and ``code`` skips concept extraction by policy (``graph_rag.extract_policy``).

**The cap (protocol point 9).** ``--max-files`` (default :data:`DEFAULT_MAX_FILES`)
counts files *written*; an unchanged file is free, so re-runs drain a large
repository. Past the cap each remaining file is compared with its stored row
(exact: reject mode never redacts) and the number still to write is printed.
With summaries and concepts skipped, a capped run costs only embeddings.

**Exit codes.** 0 when the run completes, refusals and per-file failures
included (they are counted, as ``ingest-notion`` and ``ingest-dir`` do); 1 when
``git``, the repository or the database fails before the loop, or ``--sensitivity``
is not a level (the shared check ``ingest-dir`` uses); 2 for the other usage errors.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import psycopg
import typer

from .cli_ingest import (
    _SENSITIVITY_HELP,
    _analyze_after_bulk_write,
    _build_embedder,
    _build_enricher,
    _build_graph_syncer,
    _emit_egress_notice,
    _validate_sensitivity_or_exit,
)
from .config import Config
from .db import connect_raw
from .errors import BrainError, SecretGuardError, describe_failure
from .ingest import Embedder, ExtractedDoc, ingest_document
from .ingest import code_files as cf
from .ingest import code_repo as cr
from .ingest.guard import scan_secrets
from .ingest.text_safety import one_line
from .tags import normalize_tags

if TYPE_CHECKING:
    from .enrichment import OllamaEnricher
    from .graph_rag.sync import GraphSyncer

USAGE = "ingest-code"
SOURCE_KIND = "code"
DEFAULT_MAX_FILES = 2000
DEFAULT_CODE_SENSITIVITY = "confidential"
_SCOPE_REFUSAL = (
    f"{USAGE}: --author-email and --since are required "
    "(only files you committed since that day are ingested)"
)


def ingest_code(
    repo_dir: Path = typer.Argument(..., help="A git work tree (or a directory inside one)."),
    author_email: list[str] = typer.Option(
        [], "--author-email", help="Your commit address; repeat for each one you use."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Files you committed on or after this day (YYYY-MM-DD)."
    ),
    ext: list[str] = typer.Option([], "--ext", help="Only these extensions (py, .ts); repeat."),
    max_files: int = typer.Option(
        DEFAULT_MAX_FILES, "--max-files", min=1, help="Write at most this many files this run."
    ),
    sensitivity: str = typer.Option(
        DEFAULT_CODE_SENSITIVITY, "--sensitivity", help=_SENSITIVITY_HELP
    ),
    tag: list[str] = typer.Option([], "--tag", "-t", help="Apply tag(s) to each document."),
    enrich: bool = typer.Option(
        False, "--enrich/--no-enrich", help="Add the Ollama auto-summary (off by default)."
    ),
) -> None:
    """Ingest the files you committed in one git repository (code, chunked by definition).

    Git-ignored files, vendored and build directories, lockfiles, minified and
    binary files, and files over 200 KB are always skipped. A file holding a
    credential-shaped string is refused. Re-running updates files in place; at
    most --max-files files are written a run.
    """
    emails, first_day, exts = _scope_or_exit(author_email, since, ext)
    level = _validate_sensitivity_or_exit(sensitivity)
    try:
        tree = cr.find_work_tree(repo_dir, runner=_runner(repo_dir))
        runner = _runner(tree.root)
        owned = cr.owned_files(tree, runner=runner, emails=emails, since=first_day)
    except cr.CodeRepoError as exc:
        typer.secho(f"{USAGE}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None
    candidates, skipped = cf.select_candidates(owned, exts=exts)
    typer.echo(f"found {len(owned)} file(s) you committed since {first_day.isoformat()}")
    run = _Run.start(
        tags=normalize_tags(list(tag)), level=level, enrich=enrich, max_files=max_files
    )
    run.counts.update(skipped)
    _ingest_all(tree, candidates, run)


def _runner(directory: Path) -> cr.GitRunner:
    return functools.partial(cr.git_runner, repo=directory)


def _refuse(message: str) -> NoReturn:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code=2)


def _scope_or_exit(
    emails: list[str], since: str | None, exts: list[str]
) -> tuple[tuple[str, ...], date, frozenset[str] | None]:
    if not emails or since is None:
        _refuse(_SCOPE_REFUSAL)
    bad = [e for e in emails if not cr.is_valid_author_email(e)]
    if bad:
        _refuse(f"{USAGE}: --author-email takes one address such as you@example.com")
    try:
        first_day = date.fromisoformat(since)
    except ValueError:
        _refuse(f"{USAGE}: --since must be YYYY-MM-DD")
    try:
        normalized = cf.normalize_exts(exts)
    except ValueError as exc:
        _refuse(f"{USAGE}: {exc}")
    return tuple(dict.fromkeys(emails)), first_day, normalized


@dataclass
class _Run:
    """What every file of one run is ingested with, and the run's counts."""

    cfg: Config
    tags: list[str]
    level: str
    embedder: Embedder
    enricher: OllamaEnricher | None
    graph_syncer: GraphSyncer
    max_files: int
    counts: Counter[str] = field(default_factory=Counter)

    @classmethod
    def start(cls, *, enrich: bool, **kwargs: Any) -> _Run:
        cfg = Config.load()
        return cls(
            cfg=cfg,
            embedder=_build_embedder(cfg),
            enricher=_build_enricher(cfg) if enrich else None,
            graph_syncer=_build_graph_syncer(cfg),
            **kwargs,
        )

    @property
    def written(self) -> int:
        return self.counts["ingested"] + self.counts["updated"]


def _ingest_all(tree: cr.WorkTree, candidates: list[str], run: _Run) -> None:
    try:
        opened = connect_raw(run.cfg.database_url)
    except psycopg.Error as exc:
        typer.secho(f"{USAGE}: database unavailable ({describe_failure(exc)})", fg="red", err=True)
        raise typer.Exit(code=1) from None
    with contextlib.closing(opened) as conn:
        conn.autocommit = True
        try:
            for path in candidates:
                read = cf.read_code_file(tree.root, path, within=tree.scope)
                if isinstance(read, str):
                    run.counts[read] += 1
                    continue
                doc = cf.build_code_doc(repo=tree.name, root=tree.root, file=read)
                if run.written >= run.max_files:
                    run.counts[_past_cap(conn, doc)] += 1
                else:
                    run.counts[_guarded(conn, doc, run)] += 1
        finally:
            _report_run(run)
        if run.written:
            _analyze_after_bulk_write(conn, context=USAGE)


def _guarded(conn: psycopg.Connection[Any], doc: ExtractedDoc, run: _Run) -> str:
    """Ingest one file; a refusal or a failure is counted and printed, never fatal."""
    path = one_line(doc.metadata["path"])
    try:
        result = ingest_document(
            conn,
            embedder=run.embedder,
            doc=doc,
            source_kind=SOURCE_KIND,
            tags=run.tags,
            vault_root=run.cfg.vault_path,
            enricher=run.enricher,
            enrich=run.enricher is not None,
            enrich_min_tokens=run.cfg.enrich_min_tokens,
            graph_syncer=run.graph_syncer,
            # RM-43.D1: always reject, whatever BRAIN_SECRET_GUARD says.
            secret_guard="reject",
            allow_secrets=False,
            sensitivity=run.level,
        )
    except SecretGuardError:
        return _refused(doc)
    except (psycopg.Error, OSError, ValueError, BrainError) as exc:
        typer.secho(f"  failed {path}: {describe_failure(exc)}", fg="red", err=True)
        return "failed"
    _emit_egress_notice(result.egress_notice)
    outcome = "ingested" if result.created else "updated" if result.body_changed else "unchanged"
    if outcome != "unchanged":
        typer.echo(f"  {outcome} {path}")
    return outcome


def _refused(doc: ExtractedDoc) -> str:
    """Print a refusal. The guard's message quotes masked previews; kinds and lines say enough."""
    where = ", ".join(f"{f.kind} at line {f.line}" for f in scan_secrets(doc.content))
    typer.secho(f"  refused {one_line(doc.metadata['path'])}: {where}", fg="red", err=True)
    return "refused"


def _past_cap(conn: psycopg.Connection[Any], doc: ExtractedDoc) -> str:
    """What :func:`_guarded` would report for a file past the cap, without writing it.

    ``unchanged`` when the stored row has this exact body (reject mode never
    redacts, so the stored hash is the body's), ``refused`` when the guard would
    refuse it (so it is never counted as remaining forever), else ``remaining``.
    """
    row = conn.execute(
        "SELECT content_hash FROM documents WHERE source_path = %s AND kind = 'ingested'",
        (doc.source_path,),
    ).fetchone()
    digest = hashlib.sha256(doc.content.encode("utf-8")).hexdigest()
    if row is not None and row[0] == digest:
        return "unchanged"
    return _refused(doc) if scan_secrets(doc.content) else "remaining"


def _report_run(run: _Run) -> None:
    c = run.counts
    typer.echo(
        f"{c['ingested']} ingested, {c['updated']} updated, {c['unchanged']} unchanged, "
        f"{c['refused']} refused, {c['failed']} failed"
    )
    for reason, text in cf.SKIP_TEXT.items():
        if c[reason]:
            typer.echo(f"  {c[reason]} skipped: {text}")
    if c["refused"]:
        typer.secho(
            f"{c['refused']} file(s) NOT ingested: each holds a credential-shaped string, and "
            "ingest-code always rejects those. Remove the credential from the file (and rotate "
            "it if it is real), then run again.",
            fg="red",
            err=True,
        )
    if c["remaining"]:
        typer.secho(
            f"stopped at --max-files {run.max_files}: {c['remaining']} file(s) remain; "
            "run again to continue",
            fg="yellow",
        )


def register(app: typer.Typer) -> None:
    """Attach ``brain ingest-code`` to ``app`` (through ``cli_registry``)."""
    app.command(name="ingest-code")(ingest_code)
