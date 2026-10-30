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
) -> tuple[BriefData, Path | None]:
    """Build the terminal brief and, under ``wiki``, write the published page.

    Returns the terminal (permissive) payload and the written page's path, or ``None`` when
    ``wiki`` is false. The page is written whatever the caller does with the payload, so
    ``brain brief --json --wiki`` still writes it.
    """
    with connect(cfg.database_url) as conn:
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
    # Compared while BOTH still carry ``suggestions=[]``, so this is a straight
    # question about the assembled rows. It decides only whether the published
    # page needs its OWN LLM round-trip: ``suggest_next_steps`` feeds action-item
    # BODY text into the prompt, so suggestions derived from the permissive
    # payload are confidential-derived and must never be written to the page.
    # When the two payloads are equal there is nothing to re-derive from, and
    # reusing the one call is exact rather than an approximation. Being wrong
    # here can only cost a second Ollama call, never a disclosure.
    withheld = published is not None and published != data
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
    written_path = (
        brief_mod.write_brief_to_vault(cfg.vault_path, on_date, published)
        if published is not None
        else None
    )
    return data, written_path
