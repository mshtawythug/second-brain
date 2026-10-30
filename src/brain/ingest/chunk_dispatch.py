"""Chunker choice by ``content_type``: ``code`` to the code chunker, the rest to prose (RM-42).

Named apart from :mod:`.chunker`, which stays the prose chunker and is not
changed by this module: every content type other than ``code`` reaches
:func:`.chunker.chunk_text` with exactly the arguments it got before RM-42.
"""
from collections.abc import Callable, Mapping
from typing import Any

from .chunker import Chunk, chunk_text
from .code_chunker import chunk_code

# ``Chunk`` is re-exported so ``ingest/__init__.py`` keeps one import line (net zero).
__all__ = ["CODE_CONTENT_TYPE", "Chunk", "chunk_for"]

#: The content type RM-43's code-repository ingest stores.
CODE_CONTENT_TYPE = "code"


def chunk_for(
    content_type: str,
    content: str,
    metadata: Mapping[str, Any] | None,
    count_tokens: Callable[[str], int],
    /,
) -> list[Chunk]:
    """Chunk ``content`` with the chunker its ``content_type`` calls for.

    Example::

        chunk_for("code", source, {"path": "pkg/mod.py"}, embedder.count_tokens)

    For ``code``, two optional string keys of ``metadata`` are hints:
    ``path`` (shown in every chunk's ``path:start-end symbol`` header) and
    ``language`` (wins over ``path``'s extension). A missing or non-string
    value is ignored. Metadata carries them because it is what both ingest call
    sites hold: the in-place update has the stored row, not a source path.

    Positional-only, a deliberate deviation from the spec's keyword
    ``count_tokens``: it keeps both call sites in ``ingest/__init__.py`` (over
    the size ceiling) to one line each.
    """
    if content_type != CODE_CONTENT_TYPE:
        return chunk_text(content, count_tokens=count_tokens)
    hints = metadata or {}
    return chunk_code(
        content,
        count_tokens=count_tokens,
        path=_hint(hints, "path"),
        language=_hint(hints, "language"),
    )


def _hint(metadata: Mapping[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None
