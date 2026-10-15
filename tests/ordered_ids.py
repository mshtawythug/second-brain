"""Chunk ids that sort in insertion order, for fixtures that tie candidates on cosine.

The related-docs vector leg breaks cosine ties by chunk id, like the search leg
(design D, ``docs/specs/2026-10-15-related-connect-vector-leg-design.md``).
Several ranking fixtures give two or more candidates the SAME vector on purpose.
Rank fusion cannot be neutral about a tie: whichever tied chunk the leg lists
first gets the better reciprocal rank. With the default random UUID that order
is a coin flip per run. Before the tie-break existed it was physical row order,
which is insertion order, and those fixtures were written against it without
saying so. Ids from :func:`next_chunk_id` make that order deliberate and
deterministic: the earlier-inserted chunk always sorts first.
"""
from __future__ import annotations

import itertools
import uuid

_counter = itertools.count(1)


def next_chunk_id() -> str:
    """A UUID greater than every one this session has issued before."""
    return str(uuid.UUID(int=next(_counter)))
