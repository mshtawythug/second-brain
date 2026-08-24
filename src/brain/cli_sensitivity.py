"""``brain backfill scan-secrets`` — the F4/F6 corpus secret sweep."""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any

import psycopg
import typer

from .config import Config
from .db import connect
from .ingest import Embedder, update_document
from .ingest.guard import SecretFinding, redact_secrets, scan_secrets
from .queries import (
    ScannableDocument,
    iter_documents_for_secret_scan,
    set_document_sensitivity,
)
from .sensitivity import CONFIDENTIAL
from .vault.derived_links.fence import refresh_fences_naming

# ---------------------------------------------------------------------------
# Why this lives in its own module rather than in cli.py.
#
# ``cli.py`` is ~9,700 lines and is the file every feature in this release
# would otherwise collide in. The Wave-0 seam refactor exists precisely so a
# feature owns a whole module; this one registers a single command onto the
# EXISTING ``backfill`` sub-app, so the only thing ``cli.py`` needs is one
# import and one call.
#
# WHAT THIS COMMAND IS FOR. The F4 guard runs at INGEST time, so it protects
# everything ingested after it shipped and nothing ingested before. This sweep
# is the retroactive half: it finds credential-shaped strings already sitting in
# the corpus. It cannot un-send anything already POSTed to a hosted embedder,
# and it does not rewrite git history or prior wiki builds.
# ---------------------------------------------------------------------------

#: The three things ``--apply`` can do. ``report`` is the default and cannot
#: write even WITH ``--apply`` — the read-only default has to be un-bypassable
#: by a single flag, because the alternative failure (a user exploring the
#: command and silently rewriting 1,376 documents) is unrecoverable without a
#: backup.
_ACTIONS = ("report", "mark-confidential", "redact")


@dataclass
class _SweepResult:
    """Accumulated outcome of one sweep. Mutable — it is a running tally."""

    scanned: int = 0
    flagged: int = 0
    written: int = 0
    documents: list[dict[str, Any]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def _finding_json(finding: SecretFinding) -> dict[str, Any]:
    """Project a finding for ``--json``.

    ``preview`` is the masked form built by the guard — never the raw match.
    That is the one invariant this projection must not break, and it is
    asserted per-pattern in ``tests/test_ingest_guard.py``.
    """
    return {
        "kind": finding.kind,
        "line": finding.line,
        "col_start": finding.col_start,
        "col_end": finding.col_end,
        "preview": finding.preview,
    }


def _apply_mark_confidential(
    conn: Any, doc: ScannableDocument, result: _SweepResult, *, cfg: Config
) -> None:
    """Flip a hit document to ``confidential``; count only real changes.

    ``set_document_sensitivity`` returns ``False`` when the row was already at
    the target level, so re-running the sweep over a corpus it has already
    marked reports ``0 written`` rather than re-counting every previous hit.

    The fence refresh is inside that ``if`` for the same reason the counter is:
    on a re-run over an already-marked corpus nothing changed, so there is
    nothing to propagate and no reason to rewrite N files. It takes ``cfg`` for
    the vault path — the second of the two ``set_document_sensitivity`` callers,
    fixed alongside ``mark-confidential`` rather than after it, because "the
    other caller" is precisely how a gate ends up true of one surface and
    silently not of another.
    """
    if set_document_sensitivity(conn, document_id=doc.id, level=CONFIDENTIAL):
        result.written += 1
        try:
            refresh_fences_naming(conn, doc.id, vault_path=cfg.vault_path)
        except (OSError, psycopg.Error) as exc:
            # Recorded per-document rather than raised: one unwritable mirror
            # must not abort a sweep over the whole corpus, exactly as
            # ``_apply_redact`` treats its own per-document failures.
            result.errors.append((doc.id, f"fence refresh failed: {exc}"))


def _apply_redact(
    conn: Any, doc: ScannableDocument, result: _SweepResult, *, cfg: Config
) -> None:
    """Rewrite a hit document's body with the secrets replaced.

    Routed through ``update_document`` rather than a raw ``UPDATE`` — spec Q2,
    and deliberately NOT an optimization target. It is the only path that
    re-chunks, re-embeds, re-hashes, and regenerates the vault mirror
    consistently. A raw SQL body write would leave the chunks (and therefore
    every search result) still containing the secret, which is the exact
    opposite of what the command claims to do.

    A ``ValueError`` here is almost always the documented hash collision: the
    redacted body now matches another document's ``content_hash``. It is caught
    PER DOCUMENT so one poisoned row cannot abort a sweep over the whole corpus
    — the failure is recorded and reported in the final tally instead.
    """
    redacted, _ = redact_secrets(doc.content)
    if redacted == doc.content:
        return
    try:
        update_document(
            conn,
            document_id=doc.id,
            new_content=redacted,
            embedder=_build_embedder(cfg),
            vault_root=cfg.vault_path,
        )
    except ValueError as exc:
        result.errors.append((doc.id, str(exc)))
        return
    result.written += 1


def _build_embedder(cfg: Config) -> Embedder:
    """Resolve the active embedder lazily, via the ``brain.cli`` patch point.

    Deferred to call time (and only reached on the ``redact`` path) so the
    read-only default never constructs an embedder — which under
    ``BRAIN_EMBEDDER=arctic`` would require Ollama to be running just to print
    a report that touches nothing.

    Same shim shape as ``cli_search._build_embedder``, including the
    ``attr-defined`` ignore: ``cli.py`` re-exports the helper for the ~20 test
    modules that patch ``brain.cli._build_embedder``, and routing through it
    keeps that patch point effective here too.
    """
    from . import cli as _cli

    return _cli._build_embedder(cfg)  # type: ignore[attr-defined]


def scan_secrets_cmd(
    apply: bool = typer.Option(
        False, "--apply", help="Actually apply the action (default: read-only report)."
    ),
    action: str = typer.Option(
        "report",
        "--action",
        help=(
            "What --apply does to each hit: report|mark-confidential|redact. "
            "'report' never writes."
        ),
    ),
    limit: int = typer.Option(
        0, "--limit", min=0, help="Stop after N documents scanned (0 = no limit)."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of the table."
    ),
) -> None:
    """Scan every stored document for credential-shaped strings.

    READ-ONLY BY DEFAULT. ``--apply`` is required to write anything, and the
    default ``--action report`` cannot write even with it. Both gates exist
    because the destructive action here rewrites document bodies across the
    whole corpus.

    The F4 guard protects documents from the moment it shipped; this is the
    retroactive half for everything ingested before. It finds what is already
    stored — it cannot un-send anything already transmitted to a hosted
    embedder.

    ``--action redact`` re-chunks, re-embeds, re-hashes, and regenerates the
    vault mirror for each rewritten document, so it is the slow option.
    """
    if action not in _ACTIONS:
        raise typer.BadParameter(
            f"--action must be one of {'/'.join(_ACTIONS)} (got {action!r})"
        )

    cfg = Config.load()
    result = _SweepResult()
    writing = apply and action != "report"

    with connect(cfg.database_url) as conn:
        conn.autocommit = True
        if not json_output:
            mode = (
                f"applying --action {action}"
                if writing
                else "READ ONLY (pass --apply with --action to act)"
            )
            typer.echo(f"scanning documents — {mode}")

        for doc in iter_documents_for_secret_scan(conn):
            if limit and result.scanned >= limit:
                break
            result.scanned += 1
            findings = scan_secrets(doc.content)
            if not findings:
                continue
            result.flagged += 1
            result.documents.append(
                {
                    "id": doc.id,
                    "title": doc.title,
                    "source_kind": doc.source_kind,
                    "findings": [_finding_json(f) for f in findings],
                }
            )
            if not json_output:
                kinds = ", ".join(sorted({f.kind for f in findings}))
                typer.echo(
                    f"  {doc.id[:8]}  {(doc.source_kind or 'manual'):<8} "
                    f"{doc.title[:40]:<40} {len(findings)}  {kinds}"
                )
            if not writing:
                continue
            if action == "mark-confidential":
                _apply_mark_confidential(conn, doc, result, cfg=cfg)
            else:
                _apply_redact(conn, doc, result, cfg=cfg)

    if json_output:
        typer.echo(
            _json.dumps(
                {
                    "scanned": result.scanned,
                    "flagged": result.flagged,
                    "written": result.written,
                    "action": action,
                    "applied": bool(apply),
                    "documents": result.documents,
                    "errors": [
                        {"id": doc_id, "error": msg} for doc_id, msg in result.errors
                    ],
                },
                indent=2,
            )
        )
        return

    typer.echo(
        f"\n{result.flagged} document(s) with findings / {result.scanned} scanned "
        f"/ {result.written} written"
    )
    if result.errors:
        # Surfaced rather than swallowed: a redaction that could not be applied
        # leaves the secret in place, which the user must know about.
        typer.secho(f"{len(result.errors)} error(s):", fg="red", err=True)
        for doc_id, msg in result.errors:
            typer.secho(f"  {doc_id[:8]}  {msg}", fg="red", err=True)
    if result.flagged and not writing:
        typer.echo(
            "next: brain backfill scan-secrets --apply --action mark-confidential"
        )


def register_backfill(backfill_app: typer.Typer) -> None:
    """Attach ``scan-secrets`` to the existing ``brain backfill`` sub-app.

    Takes ``backfill_app`` rather than the root ``app`` — which is why this
    registrar is deliberately NOT in ``cli_registry.REGISTRARS``, whose entries
    all take the root app. See the note there.
    """
    backfill_app.command("scan-secrets")(scan_secrets_cmd)
