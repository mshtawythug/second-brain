"""The daily brief's two-audience build: the terminal payload and the published page (RM-15).

:func:`build_brief` is ``brain brief``'s whole sequence, moved out of ``cli.py`` so that later
work (RM-16's delivery, RM-19's duplicate section) builds on one function instead of growing
``cli.py``. ``cli.brief`` keeps argument parsing, the call and the printing.

The two audiences are the F6 split. The TERMINAL payload (and ``--json``) is built permissively:
the owner reading their own corpus is inside the trust boundary. The PUBLISHED payload, built
only under ``wiki``, is the ``<vault>/daily/<YYYY>/<date>-brief.md`` page, which carries no
``sensitivity`` frontmatter key, so it is built as a second payload with
``exclude_confidential=True`` rather than a filter of the first (``DocumentRow`` / ``TodoRow`` /
``PinnedDoc`` carry no ``sensitivity``; the fuller note is at ``cli.review_weekly``).

**Watches (RM-15).** Every build checks every enabled watch FIRST, on its own connection and
before either payload is assembled, so a hit found now is in this brief. The check records hits
and marks nothing. The terminal payload lists every hit first seen in the capture window,
notified or not; the published payload lists the UNNOTIFIED ones (at most
:data:`brain.saved_search.PUBLISHED_CAP` per watch, gated). Once the page is written, every
unnotified hit the build read is marked notified: over-cap and confidential ones too, which the
terminal and ``brain watch hits`` still show (RM-15.D3).

The :mod:`brain.brief` functions are looked up on the module at call time, as ``cli.brief`` did
through its function-local import, so a test double on ``brain.brief.suggest_next_steps`` still
reaches this code.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from . import brief as brief_mod
from . import db
from .brief import BriefData
from .config import Config
from .ingest import Embedder
from .saved_search import (
    UnnotifiedView,
    WatchNote,
    mark_notified,
    unnotified_view,
    watch_tables_present,
)
from .saved_search_check import CheckResult, build_matcher, check_enabled, limit_note

#: Opens one database connection from a URL: :func:`brain.db.connect` in production.
Connect = Callable[[str], AbstractContextManager[psycopg.Connection[Any]]]


def build_brief(
    cfg: Config,
    *,
    on_date: date,
    since_hours: int,
    todo_since_days: int,
    enrich: bool,
    wiki: bool,
    connect: Connect = db.connect,
    embedder: Embedder | None = None,
) -> tuple[BriefData, Path | None]:
    """Build the terminal brief and, under ``wiki``, write the published page.

    Returns the terminal (permissive) payload and the written page's path, or ``None`` when
    ``wiki`` is false. The page is written whatever the caller does with the payload, so
    ``brain brief --json --wiki`` still writes it.

    ``embedder`` serves semantic watches; ``None`` builds the configured one, and only if an
    enabled semantic watch is checked.
    """
    view: UnnotifiedView | None = None
    with connect(cfg.database_url) as conn:
        # Autocommit, so each watch check is its own committed transaction and
        # its row lock is released before the brief's reads (RM-15 plan, D5).
        conn.autocommit = True
        watches_on = watch_tables_present(conn)
        checks = (
            check_enabled(
                conn,
                build_matcher(cfg, embedder=None if embedder is None else lambda: embedder),
            )
            if watches_on
            else []
        )
        data = brief_mod.assemble_brief(
            conn,
            cfg,
            since_hours=since_hours,
            todo_since_days=todo_since_days,
            on_date=on_date,
            exclude_confidential=False,
        )
        published = (
            brief_mod.assemble_brief(
                conn,
                cfg,
                since_hours=since_hours,
                todo_since_days=todo_since_days,
                on_date=on_date,
                exclude_confidential=True,
            )
            if wiki
            else None
        )
        if published is not None and watches_on:
            view = unnotified_view(conn)
    data = replace(data, watch_notes=_check_notes(checks, published=False))
    # Compared while BOTH still carry ``suggestions=[]``, so this is a straight
    # question about the assembled rows. It decides only whether the published
    # page needs its OWN LLM round-trip: ``suggest_next_steps`` feeds action-item
    # BODY text into the prompt, so suggestions derived from the permissive
    # payload are confidential-derived and must never be written to the page.
    # When the two payloads are equal there is nothing to re-derive from, and
    # reusing the one call is exact rather than an approximation. Being wrong
    # here can only cost a second Ollama call, never a disclosure.
    # The watch fields are left out: the two payloads list different hit sets
    # by construction, and no hit reaches the suggestion prompt (RM-15 plan, D4).
    withheld = published is not None and _without_watches(published) != _without_watches(
        data
    )
    if enrich:
        suggestions = brief_mod.suggest_next_steps(data, cfg)
        if suggestions:
            data = replace(data, suggestions=suggestions)
        if published is not None:
            published = replace(
                published,
                suggestions=(
                    brief_mod.suggest_next_steps(published, cfg) if withheld else suggestions
                ),
            )
    if published is not None and view is not None:
        published = replace(
            published,
            watch_hits=view.hits,
            watch_notes=[*view.notes, *_check_notes(checks, published=True)],
        )
    written_path = (
        brief_mod.write_brief_to_vault(cfg.vault_path, on_date, published)
        if published is not None
        else None
    )
    if written_path is not None and view is not None and view.pending:
        with connect(cfg.database_url) as conn:
            conn.autocommit = True
            mark_notified(conn, view.pending)
    return data, written_path


def _check_notes(checks: list[CheckResult], *, published: bool) -> list[WatchNote]:
    """One note per watch whose check was skipped or (terminal only) hit the fetch limit.

    The published page never gets the limit line: its total spans both tiers, so even the
    line's presence would say that withheld documents exist (the F6 oracle).
    """
    notes = []
    for check in checks:
        limit = None if published else limit_note(check)
        if check.skipped is not None or limit is not None:
            notes.append(WatchNote(watch=check.watch, skipped=check.skipped, limit=limit))
    return notes


def _without_watches(brief: BriefData) -> BriefData:
    return replace(brief, watch_hits=[], watch_notes=[])
