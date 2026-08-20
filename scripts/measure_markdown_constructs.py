#!/usr/bin/env python3
"""Count which corpus documents use each markdown construct — by TOKENIZER.

Phase 1 needed these figures to size the work and to justify one dependency
waiver. They are re-derivable here rather than quoted from a report, because
five of the six counts that phase produced were wrong at least once, and one
wrong figure survived review by sitting in a column labelled "tokenizer" when
it had come from a regex.

**Why a tokenizer and not a regex.** A regex over raw text cannot see block
context. ``- [ ]`` inside a fenced code block matches but never renders as a
task list; ``~~`` inside a CloudFront signed URL matches but is not
strikethrough; ``[^\\s@]`` inside a code fence looks like a footnote reference
and is a character class. Parsing with the UI's own ``build_renderer()`` makes
those *structurally invisible* — fenced content becomes a ``fence`` token that
never reaches inline rules — which is a stronger property than filtering them
out afterwards, because there is no filter to get wrong.

Measured counts have moved in BOTH directions against the cheap method
(strikethrough 38→31, footnotes 3→0, task lists 145→180, tables 468→466), so
there is no correction factor to apply. Use this.

Read-only: a single SELECT, no writes, no DDL.

**DSN resolution, in order: ``--dsn`` → configured ``DATABASE_URL`` → the
documented default.** Each step announces which corpus it measured on stderr,
credentials stripped.

The announcement is the point, not the strictness. Version 1 hardcoded the DSN
and ignored config — it would silently measure a *different corpus* on any box
pointed elsewhere, wrong in the direction that looks like agreement. Version 2
over-corrected: it required a configured ``DATABASE_URL`` and exited 2 without
one, which broke the bare command **in this very worktree**, where ``.env`` is
gitignored and absent. An artifact that re-derives nothing in the tree where the
work was done is no better than one that re-derives the wrong thing.

So: fall back, but say so. A run cannot be read without knowing which corpus
produced it, which was the actual requirement.

    python scripts/measure_markdown_constructs.py
    python scripts/measure_markdown_constructs.py --dsn "$TEST_DATABASE_URL"
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from typing import Any

from brain.config import Config, ConfigError
from brain.db import connect
from brain.ui.render import build_renderer

#: Used only when nothing is configured. The documented default from
#: ``.env.example`` and the README — not a secret, and not authoritative: any
#: configured ``DATABASE_URL`` wins, and the fallback announces itself.
DEFAULT_DSN = "postgresql://brain:brain@localhost:55432/second_brain"

#: A footnote needs a definition AND a reference; a lone `[^x]` is usually a
#: regex character class. Kept as regexes because markdown-it has no footnote
#: rule enabled — there is no token to look for.
FN_DEF = re.compile(r"^\s*\[\^[^\]]+\]:", re.M)
FN_REF = re.compile(r"\[\^[^\]]+\]")


def _task_items(tokens: list[Any]) -> int:
    """Count task-list items, by the CLASS the tasklists plugin stamps on them.

    Deliberately **not** a `^\\[[ xX]\\]` match on the item's inline text. That
    was this script's first implementation and it returned **0 across the whole
    corpus**, because ``build_renderer()`` now REGISTERS the tasklists plugin —
    the plugin consumes the ``[ ]`` marker and replaces it with an ``html_inline``
    ``<input>`` before anything downstream sees the text.

    The instrument had silently started measuring the wrong thing *because the
    code it measures changed*. A count of "documents needing task-list support",
    taken with a renderer that already supports task lists, is 0 by construction
    — and 0 is a plausible-looking answer, which is what makes it dangerous.

    So this reads the plugin's own output: ``list_item_open`` carrying
    ``class="task-list-item"``. That is the same token the browser gets, and it
    tracks the renderer rather than a syntax assumption about it.
    """
    return sum(
        1
        for tok in tokens
        if tok.type == "list_item_open"
        and "task-list-item" in str(tok.attrs.get("class", ""))
    )


def measure(dsn: str) -> tuple[dict[str, Counter[str]], int]:
    md = build_renderer()
    docs: dict[str, Counter[str]] = {
        k: Counter() for k in ("table", "strikethrough", "fence_lang", "task_list", "footnote")
    }
    task_items = 0

    # `brain.db.connect` rather than a bare `psycopg.connect`: it supplies the
    # `connect_timeout=10` CLAUDE.md requires of every external client, which a
    # bare call silently omits.
    with connect(dsn) as conn:
        rows = conn.execute("SELECT kind, content FROM documents").fetchall()

    for kind, content in rows:
        if not content:
            continue
        tokens = md.parse(content)
        types = {t.type for t in tokens}

        if "table_open" in types:
            docs["table"][kind] += 1
        if "fence" in types and any(
            t.type == "fence" and t.info.strip() for t in tokens
        ):
            docs["fence_lang"][kind] += 1
        # `s_open` is markdown-it's strikethrough token; it appears in the
        # INLINE children, not the top-level stream.
        if any(
            child.type == "s_open"
            for t in tokens
            if t.type == "inline" and t.children
            for child in t.children
        ):
            docs["strikethrough"][kind] += 1
        n = _task_items(tokens)
        if n:
            docs["task_list"][kind] += 1
            task_items += n
        if FN_DEF.search(content) and FN_REF.search(content):
            docs["footnote"][kind] += 1

    return docs, task_items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dsn",
        default=None,
        help="Postgres DSN (read-only). Defaults to this install's DATABASE_URL.",
    )
    args = ap.parse_args()

    dsn, source = args.dsn, "--dsn"
    if dsn is None:
        try:
            dsn, source = Config.load().database_url, "DATABASE_URL"
        except ConfigError:
            dsn, source = DEFAULT_DSN, "BUILT-IN DEFAULT (no DATABASE_URL configured)"
    # Credentials stripped: this line lands in logs and pasted output.
    print(f"corpus: {dsn.rsplit('@', 1)[-1]}  [source: {source}]\n", file=sys.stderr)

    docs, task_items = measure(dsn)

    print(f"{'construct':16s} {'total':>6s}  {'ingested':>9s} {'vault':>6s}")
    print("-" * 42)
    for name, counter in docs.items():
        total = sum(counter.values())
        print(
            f"{name:16s} {total:6d}  {counter.get('ingested', 0):9d} "
            f"{counter.get('vault', 0):6d}"
        )
    print(f"\ntask-list ITEMS across the corpus: {task_items}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
