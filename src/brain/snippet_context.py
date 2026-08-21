"""Snippet-context expansion: stitch neighboring chunks around the best match.

Extracted verbatim from :mod:`brain.search` (Wave 4). ``search.py`` was already
over the repo's 800-line file ceiling before this wave, so the snippet-expansion
helper moved out rather than growing in place; ``search.py`` is now 793. This is
the :mod:`brain.format_search` precedent exactly — that module exists because
``format.py`` hit 783/800. The ceiling, the files already over it, and the rule
that a file over it may grow only with a written reason are in CLAUDE.md under
"File-size ceiling"; ``search.py`` is not one of them.

The move was behaviour-preserving: the outward-walk algorithm below is
byte-for-byte the one that shipped in ``search.py``. The only substantive
change is that the hard character cap, previously the inlined expression
``4 * brain.search.SNIPPET_LENGTH``, is now the ``max_chars`` parameter —
numerically identical by default, and configurable via
``BRAIN_SNIPPET_MAX_CHARS``. That knob is not decoration; see below.

Do not add adaptive neighbour selection here — it was built and measured
------------------------------------------------------------------------

Wave 4 of the agentic-token-reduction plan proposed replacing this fixed token
budget with a parameter-free Otsu cut over each neighbour's own ``ts_rank``,
on the strength of a live measurement in which **3 of 5** search results were
pinned to exactly the 1,600-char cap — "the truncation point is chosen by a
constant rather than by the content". The mechanism was implemented in full,
tested, and measured against the live corpus (11 seeded queries x 5 results =
55 results, 2026-08-13). It was then **removed**, because the measurement did
not support it. The artifact is
``docs/audits/2026-08-13-adaptive-snippet-engagement.json``.

What the measurement found:

* The Otsu path **engaged on 41 of 55 results (74.5%)** — the neighbours really
  do differ in lexical relevance most of the time. Engagement was never the
  problem, and the plan's "<20% engagement means revert" trigger did not fire.
* It nonetheless changed **zero bytes** of the delivered snippet on **55 of
  55** results, confirmed two independent ways (token counts, and a direct
  byte comparison of the two arms).
* Cause: only **3 of 55** results admitted *any* neighbour at all. The live
  median chunk is ~2,281 chars / ~570 tokens against a default
  ``snippet_context_tokens`` budget of **200** — **676 of 13,114 chunks
  (5.2%)** in the corpus were small enough to fit when measured (2026-08-13,
  live corpus; a corpus-wide CHUNK statistic, coincidentally close to but
  DISTINCT from the 3/55 = 5.45% of *results* above — do not conflate them).
  Unlike every other figure in this note, no committed artifact carries the
  676/13,114; it survives only here and in the uncommitted Wave-4 report, so
  re-derive it (``SELECT count(*) FILTER (WHERE …)`` over chunk token counts)
  before relying on it. There is usually no neighbour set to select from.
* And on **47 of 55** results (85.5%) the **matched chunk alone** already
  exceeded ``max_chars``, so the cap truncates the matched chunk itself,
  before neighbour admission is ever consulted. Across all 55, the expansion
  contributed **nothing** to the delivered token count.

  (Two distinct numbers, stated separately on purpose. An earlier draft of this
  note collapsed them into "55 of 55 fill the cap", which is not what was
  measured — 55/55 is "the expansion added no delivered tokens", 47/55 is
  "the matched chunk alone is at or over the cap".)

The structural lesson, which is why this note is here rather than in a
gitignored plan: **neighbour SELECTION is not the binding constraint on
snippet size.** The two things that bind are the ``max_chars`` cap (against a
single chunk) and the ``budget_tokens`` walk budget (against the chunker's
chunk size). Any future attempt to make snippet truncation content-aware has
to move one of those, or it will be — as this one was — correct, well-tested,
frequently invoked, and incapable of affecting the number it was adopted to
move. Raising the budget 7.5x to 1,500 tokens was tried: the cut then did bite
(87 -> 57 neighbours admitted, -16,714 tokens *before* the cap) and the
delivered payload still did not shrink, moving **+34** tokens, because both
arms deliver a 1,600-char prefix either way.

Where the headroom actually is, if someone wants it: at the shipped settings
the corpus produces **30,727** tokens of expanded snippet across those 55
results and delivers **19,213** — the cap discards **11,514 tokens (37.5%)**
unread. That waste is upstream of any neighbour-selection question.

``scripts/token_payload_report.py --snippet-constraints`` re-measures the two
constraints named above on demand.
"""
import psycopg

from .ingest import Embedder

# Maximum number of neighbors on each side to fetch per finalist.
NEIGHBOR_WINDOW = 2

#: Hard outer cap on the stitched snippet, in characters, and the default for
#: ``max_chars``.
#:
#: Numerically identical to the ``4 × brain.search.SNIPPET_LENGTH`` expression
#: this module inlined before the extraction. It is a literal here rather than
#: an import because ``search`` imports *this* module — importing back would be
#: a cycle. The equality is therefore not enforced by the type system; it is
#: pinned by ``test_default_snippet_max_chars_equals_four_times_snippet_length``
#: in ``tests/test_snippet_context_extraction.py``, which is the only thing
#: standing between the two constants and silent drift.
DEFAULT_SNIPPET_MAX_CHARS = 1600


def expand_snippet_with_neighbors(
    conn: psycopg.Connection,
    *,
    document_id: str,
    best_chunk_index: int,
    best_content: str,
    embedder: Embedder,
    budget_tokens: int,
    max_chars: int = DEFAULT_SNIPPET_MAX_CHARS,
) -> str:
    """Expand a snippet by stitching neighboring chunks around the best match.

    Fetches up to :data:`NEIGHBOR_WINDOW` chunks on each side of
    ``best_chunk_index`` within the same ``document_id``. Walks outward
    from the matched chunk, prepending the preceding neighbor and appending
    the following neighbor alternately, stopping when adding the next whole
    neighbor would exceed ``base_tokens + budget_tokens``. A neighbor is
    either included in full or not at all (no mid-chunk slicing).

    Returns the stitched string. The caller applies any final display
    truncation (e.g. 120-char table preview). A hard outer cap of
    ``max_chars`` chars guards against a degenerate token-counter — and, on
    the live corpus, is what actually decides the length of most snippets. See
    the module docstring before assuming otherwise.
    """
    lo = max(0, best_chunk_index - NEIGHBOR_WINDOW)
    hi = best_chunk_index + NEIGHBOR_WINDOW
    neighbor_rows = conn.execute(
        """
        SELECT chunk_index, content
        FROM chunks
        WHERE document_id = %s
          AND chunk_index BETWEEN %s AND %s
        ORDER BY chunk_index
        """,
        (document_id, lo, hi),
    ).fetchall()

    # Index the fetched rows by chunk_index for O(1) lookup.
    by_idx: dict[int, str] = {int(r[0]): r[1] for r in neighbor_rows}

    # The matched chunk is always included in full.
    matched = by_idx.get(best_chunk_index, best_content)

    before: list[str] = []  # chunks with index < best, in ascending order
    after: list[str] = []   # chunks with index > best, in ascending order
    budget_used = 0

    # Walk outward alternately, consuming the token budget.
    prev_idx = best_chunk_index - 1
    next_idx = best_chunk_index + 1
    while budget_used < budget_tokens:
        added = False
        if prev_idx >= lo and prev_idx in by_idx:
            chunk = by_idx[prev_idx]
            cost = embedder.count_tokens(chunk)
            if budget_used + cost <= budget_tokens:
                before.insert(0, chunk)
                budget_used += cost
                prev_idx -= 1
                added = True
            else:
                prev_idx = -1  # stop prepending — budget exhausted
        if next_idx <= hi and next_idx in by_idx:
            chunk = by_idx[next_idx]
            cost = embedder.count_tokens(chunk)
            if budget_used + cost <= budget_tokens:
                after.append(chunk)
                budget_used += cost
                next_idx += 1
                added = True
            else:
                next_idx = hi + 1  # stop appending — budget exhausted
        if not added:
            break  # no more neighbors in range or budget fully spent

    parts = before + [matched] + after
    stitched = "\n\n".join(parts)

    # Hard outer cap.
    cap = max_chars
    return stitched[:cap] if len(stitched) > cap else stitched
