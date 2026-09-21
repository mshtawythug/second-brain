"""Propagate a ``documents.sensitivity`` change out to the vault files.

**A tier change that stops at the database has not happened.** Quartz's
``RemoveConfidential`` reads the FILE's frontmatter, and ``render_fenced_section``
gates at RENDER time, so a row flipped to ``confidential`` with no vault work
leaves the document's own page still publishing *and* its title still sitting in
every partner's already-rendered fence. Both of those failures are silent and
both report success.

This module exists because that work was duplicated-then-diverged. ``cli_docs``
(``brain mark-confidential`` / ``mark-normal``) did the mirror write;
``cli_sensitivity`` (``brain backfill scan-secrets --action mark-confidential``)
did not, and nothing made the omission visible — a sweep run after discovering
secrets in a corpus reported ``N written`` while, on the published site, not one
of those documents had become confidential. The seam is here, owning the whole
of "what disk work a tier change implies", so a third caller of
:func:`brain.queries.set_document_sensitivity` inherits correctness instead of
re-deriving it and getting a subset.

It deliberately looks the document's ``kind`` / ``vault_path`` up itself rather
than accepting them. The sweep's ``ScannableDocument`` does not carry either,
so a signature demanding them would have forced that caller to grow its own
query — which is how the two paths diverged the first time.

**Failures are RETURNED, never raised.** The DB change is already committed by
the time this runs: raising would lose a completed tier change to a transient
disk error. Each caller renders the returned failures the way its surface
demands — ``mark-confidential`` warns on stderr, the corpus sweep records
per-document so one unwritable mirror cannot abort the run.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import yaml

from .derived_links.fence import refresh_fences_naming
from .export import regenerate_vault_file
from .frontmatter import rewrite_sensitivity


@dataclass(frozen=True)
class PropagationFailure:
    """One stage of the propagation that did not complete.

    ``stage`` is ``"lookup"``, ``"mirror"`` or ``"fences"``. ``message`` is
    already phrased for a human and carries its own recovery instruction,
    because the caller that renders it has no idea which stage failed or what
    fixes it.
    """

    stage: str
    message: str


def propagate_sensitivity_to_vault(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    level: str,
    vault_root: Path,
) -> list[PropagationFailure]:
    """Make a committed tier change visible on disk. Returns what failed.

    Two stages, in this order and for a reason:

    1. **The document's own file.** A vault-tier note gets one frontmatter field
       rewritten in place — the file is authoritative there, and
       ``sync._sensitivity_from_frontmatter`` reads the tier back on every pass,
       so without this the column flips and then silently REVERTS on the next
       ``brain vault sync``. An ingested-tier mirror is DB-derived and is
       regenerated wholesale instead, which picks up the new tier as a side
       effect of rebuilding the frontmatter. ``regenerate_vault_file`` refuses
       vault-tier rows outright, which is why the branch is on ``kind`` and not
       a try/except.
    2. **Every fence that NAMES it**, plus its own. See
       :func:`~brain.vault.derived_links.fence.refresh_fences_naming`.

    Stage 1 first: it regenerates the marked document's mirror, and doing that
    after stage 2 would re-emit a fence that stage 2 had just stripped.

    A document with no ``vault_path`` has nothing on disk to update; stage 1 is
    skipped and stage 2 still runs, because *other* documents' pages may still
    name it.
    """
    failures: list[PropagationFailure] = []
    label = document_id[:8]

    row = conn.execute(
        "SELECT kind, vault_path FROM documents WHERE id=%s", (document_id,)
    ).fetchone()
    if row is None:
        return [
            PropagationFailure(
                "lookup",
                f"{label} disappeared before its sensitivity change could be "
                f"written to the vault — run `brain vault export` to reconcile.",
            )
        ]
    kind, vault_path_rel = row

    if kind == "vault":
        if vault_path_rel:
            try:
                rewrite_sensitivity(Path(vault_root) / str(vault_path_rel), level)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                failures.append(
                    PropagationFailure(
                        "mirror",
                        f"could not write sensitivity into {vault_path_rel}: "
                        f"{exc}. The next `brain vault sync` will revert "
                        f"{label} to the frontmatter's value — fix the file "
                        f"and re-run.",
                    )
                )
    elif vault_path_rel:
        try:
            regenerate_vault_file(
                conn, document_id, vault_path=Path(vault_root), force=True
            )
        except OSError as exc:
            failures.append(
                PropagationFailure(
                    "mirror",
                    f"vault mirror write failed for {label}: {exc}. The "
                    f"database change succeeded — recover via "
                    f"`brain vault export`.",
                )
            )

    try:
        refresh_fences_naming(conn, document_id, vault_path=Path(vault_root))
    except (OSError, psycopg.Error) as exc:
        failures.append(
            PropagationFailure(
                "fences",
                f"could not refresh derived-link fences after marking {label} "
                f"as {level}: {exc}. Other documents' published pages may "
                f"still name it — run `brain vault relink-derived`.",
            )
        )
    return failures
