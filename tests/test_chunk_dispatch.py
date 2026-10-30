"""Tests for ``chunk_for`` (RM-42): routing, the metadata hints, and prose unchanged (AC3)."""
import hashlib
import json

import pytest

from brain.ingest.chunk_dispatch import CODE_CONTENT_TYPE, chunk_for
from brain.ingest.chunker import chunk_text
from brain.ingest.code_chunker import NO_PATH


def word_count(text: str) -> int:
    return max(1, len(text.split()))


# Every prose content type an extractor emits, plus stdin's free-form ones.
PROSE_CONTENT_TYPES = [
    "txt",
    "markdown",
    "pdf",
    "docx",
    "note",
    "email",
    "email_thread",
    "krisp_action_items",
    "screenshot",
    "transcript",
    "slack_thread",
    "document",
]

_SENTENCE = "The synthetic widget team reviewed the quarterly plan in detail. "
PROSE_TEXTS = {
    "short": "One short synthetic paragraph.",
    "paragraphs": "\n\n".join(_SENTENCE * 8 for _ in range(12)),
    "oversize-paragraph": _SENTENCE * 300,
    "code-shaped": "def looks_like_code():\n    return 1\n\n\nclass Nope:\n    pass\n",
    "blank": "  \n\n ",
}


def _digest(chunks: list) -> str:
    payload = json.dumps([[c.index, c.content] for c in chunks])
    return hashlib.sha256(payload.encode()).hexdigest()


@pytest.mark.parametrize("content_type", PROSE_CONTENT_TYPES)
@pytest.mark.parametrize("text_id", sorted(PROSE_TEXTS))
def test_prose_content_types_chunk_byte_identically(content_type: str, text_id: str) -> None:
    text = PROSE_TEXTS[text_id]
    metadata = {"path": "pkg/mod.py", "language": "python"}
    assert chunk_for(content_type, text, metadata, word_count) == chunk_text(
        text, count_tokens=word_count
    )


def test_prose_chunker_output_is_pinned() -> None:
    """``chunk_text`` itself is unchanged by RM-42: its output on a fixed text is pinned.

    The digest was recorded on ``864f6eb``, before RM-42 touched anything.
    """
    chunks = chunk_text(PROSE_TEXTS["paragraphs"], count_tokens=word_count)
    assert len(chunks) == 2
    assert _digest(chunks) == _PINNED_DIGEST


_PINNED_DIGEST = "abb3c196e58f0c562e0e29fe04d328281b4e91b1a7ee0bce5274f924ddf9dc3b"


def test_code_routes_to_the_code_chunker_with_metadata_hints() -> None:
    source = "import os\n\n\ndef a():\n    return os.sep\n"
    chunks = chunk_for(CODE_CONTENT_TYPE, source, {"path": "pkg/mod.py"}, word_count)
    assert [c.content.split("\n", 1)[0] for c in chunks] == [
        "pkg/mod.py:1-1 <module>",
        "pkg/mod.py:4-5 a",
    ]


def test_code_language_hint_wins_over_the_path() -> None:
    source = "def a():\n    return 1\n"
    chunks = chunk_for(
        CODE_CONTENT_TYPE, source, {"path": "bin/tool", "language": "python"}, word_count
    )
    assert chunks[0].content.split("\n", 1)[0] == "bin/tool:1-2 a"


@pytest.mark.parametrize(
    "metadata",
    [None, {}, {"path": 7, "language": ["python"]}],
    ids=["none", "empty", "non-string"],
)
def test_code_without_usable_hints_uses_the_placeholder(metadata: dict | None) -> None:
    chunks = chunk_for(CODE_CONTENT_TYPE, "def a():\n    pass\n", metadata, word_count)
    assert chunks[0].content.split("\n", 1)[0] == f"{NO_PATH}:1-2 <module>"
