"""Synthetic documents for the near-duplicate tests (RM-19).

Every body is built from a fixed word list with a seeded generator, so no test carries real
content. ``near_copy`` changes one ten-word sentence, which keeps a 400-word body well above the
0.85 cut; ``unrelated`` draws from a different seed.
"""
from __future__ import annotations

import hashlib
import random
from typing import Any

import psycopg

VOCAB = (
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india",
    "juliet", "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo",
    "sierra", "tango", "uniform", "victor", "whiskey", "xray", "yankee", "zulu",
    "amber", "basil", "cedar", "dune", "ember", "fjord", "grove", "harbor", "islet",
    "jade", "knoll", "lagoon", "meadow", "nectar", "orchid", "pebble", "quartz",
)

_REPLACEMENT = [
    "this", "sentence", "was", "rewritten", "entirely", "by", "the", "second", "sender",
]


def body(words: int = 400, seed: int = 1) -> str:
    """A synthetic body of ``words`` words."""
    rng = random.Random(seed)
    return " ".join(rng.choice(VOCAB) for _ in range(words))


def near_copy(text: str) -> str:
    """``text`` with one ten-word sentence in the middle replaced."""
    tokens = text.split()
    middle = len(tokens) // 2
    return " ".join(tokens[:middle] + _REPLACEMENT + tokens[middle + len(_REPLACEMENT) :])


def insert_doc(
    conn: psycopg.Connection[Any],
    content: str,
    *,
    title: str = "Synthetic note",
    thread_id: str | None = None,
    sensitivity: str = "normal",
    draft: bool = False,
    vault_path: str | None = None,
) -> str:
    """Insert one synthetic document and return its id."""
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, thread_id,"
        " sensitivity, draft, vault_path)"
        " VALUES (%s, %s, %s, 'note', %s, %s, %s, %s) RETURNING id::text",
        (
            title,
            content,
            hashlib.sha256(content.encode()).hexdigest(),
            thread_id,
            sensitivity,
            draft,
            vault_path,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def edit_doc(conn: psycopg.Connection[Any], document_id: str, content: str) -> None:
    """Replace a document's body and its content hash, as an edit does."""
    conn.execute(
        "UPDATE documents SET content = %s, content_hash = %s WHERE id = %s",
        (content, hashlib.sha256(content.encode()).hexdigest(), document_id),
    )


def pairs(conn: psycopg.Connection[Any]) -> list[tuple[str, str, str]]:
    """Every recorded pair as ``(doc_a, doc_b, status)``, in key order."""
    return [
        (str(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT doc_a, doc_b, status FROM near_duplicates ORDER BY doc_a, doc_b"
        ).fetchall()
    ]
