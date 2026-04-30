"""Unit tests for the paragraph-aware chunker."""
from pathlib import Path

from brain.ingest.chunker import Chunk, chunk_text


def fake_count(text: str) -> int:
    # simple word count for predictable chunking
    return max(1, len(text.split()))


def char_count(text: str) -> int:
    return max(1, len(text))


def test_short_text_yields_single_chunk() -> None:
    chunks = chunk_text(
        "hello world", target_tokens=600, overlap_tokens=100, count_tokens=fake_count
    )
    assert len(chunks) == 1
    assert chunks[0].content == "hello world"
    assert chunks[0].index == 0


def test_paragraphs_are_grouped_under_target() -> None:
    text = "para one.\n\npara two.\n\npara three."
    chunks = chunk_text(text, target_tokens=10, overlap_tokens=0, count_tokens=fake_count)
    assert len(chunks) == 1


def test_paragraphs_split_when_target_exceeded() -> None:
    paragraphs = ["word " * 50] * 4  # 200 words total, target 100
    text = "\n\n".join(p.strip() for p in paragraphs)
    chunks = chunk_text(text, target_tokens=100, overlap_tokens=0, count_tokens=fake_count)
    assert len(chunks) >= 2


def test_overlap_repeats_tail_of_previous_chunk() -> None:
    text = "alpha\n\nbeta\n\ngamma\n\ndelta"
    chunks = chunk_text(text, target_tokens=2, overlap_tokens=1, count_tokens=fake_count)
    # second chunk should start with the last paragraph of the first chunk
    assert len(chunks) >= 2
    assert chunks[1].content.startswith("beta") or "beta" in chunks[1].content


def test_oversized_paragraph_split_on_sentences() -> None:
    long_para = ". ".join(f"sentence{i}" for i in range(20)) + "."
    chunks = chunk_text(long_para, target_tokens=5, overlap_tokens=0, count_tokens=fake_count)
    assert len(chunks) >= 2
    for c in chunks:
        # no chunk should be empty
        assert c.content.strip()


def test_chunks_are_indexed_sequentially() -> None:
    text = "a\n\nb\n\nc\n\nd\n\ne"
    chunks = chunk_text(text, target_tokens=2, overlap_tokens=0, count_tokens=fake_count)
    for i, c in enumerate(chunks):
        assert c.index == i


def test_returns_chunk_dataclass() -> None:
    chunks = chunk_text("hello", target_tokens=10, overlap_tokens=0, count_tokens=fake_count)
    assert isinstance(chunks[0], Chunk)


def test_empty_text_returns_no_chunks() -> None:
    assert chunk_text("", target_tokens=10, overlap_tokens=0, count_tokens=fake_count) == []
    blank = "   \n\n  "
    assert chunk_text(blank, target_tokens=10, overlap_tokens=0, count_tokens=fake_count) == []


# ---------------------------------------------------------------------------
# Hard-ceiling regression tests
# ---------------------------------------------------------------------------


def test_paragraph_with_no_sentence_terminators_respects_budget() -> None:
    """A 10K-token paragraph with zero `.!?` must split below ceiling."""
    text = ("word " * 10000).strip()
    target = 600
    overlap = 100
    ceiling = target + overlap
    chunks = chunk_text(
        text, target_tokens=target, overlap_tokens=overlap, count_tokens=fake_count
    )
    assert len(chunks) > 1
    for c in chunks:
        assert fake_count(c.content) <= ceiling, (
            f"chunk {c.index} has {fake_count(c.content)} tokens > ceiling={ceiling}"
        )


def test_giant_single_sentence_respects_budget() -> None:
    """A single sentence of 10K tokens (one `.` at the end) must split below ceiling."""
    text = ("word " * 9999).strip() + "."
    target = 600
    overlap = 100
    ceiling = target + overlap
    chunks = chunk_text(
        text, target_tokens=target, overlap_tokens=overlap, count_tokens=fake_count
    )
    assert len(chunks) > 1
    for c in chunks:
        assert fake_count(c.content) <= ceiling


def test_paragraph_with_only_newlines_respects_budget() -> None:
    """DOM-dump style: 10K tokens of `- foo\\n  - bar\\n` no blank lines."""
    text = ("- foo\n  - bar\n" * 5000).strip()
    target = 600
    overlap = 100
    ceiling = target + overlap
    chunks = chunk_text(
        text, target_tokens=target, overlap_tokens=overlap, count_tokens=fake_count
    )
    assert len(chunks) > 1
    for c in chunks:
        assert fake_count(c.content) <= ceiling


def test_word_longer_than_budget_respects_budget() -> None:
    """A single whitespace-free blob (e.g., base64) must char-split below ceiling."""
    target = 500
    overlap = 100
    ceiling = target + overlap
    text = "x" * (target + 50_000)  # one giant "word", no whitespace
    chunks = chunk_text(
        text, target_tokens=target, overlap_tokens=overlap, count_tokens=char_count
    )
    assert len(chunks) > 1
    for c in chunks:
        assert char_count(c.content) <= ceiling


def test_char_split_shrinks_when_token_estimate_overshoots() -> None:
    """When tokens-per-char is higher than the average, the slice-shrink loop fires.

    Uses a count_tokens that returns 2× the character count, so the initial
    char-budget slice overshoots the target and must be iteratively shrunk.
    """
    def double_count(t: str) -> int:
        return max(1, 2 * len(t))

    target = 500
    overlap = 100
    ceiling = target + overlap
    text = "x" * 5000  # one giant "word" → 10000 tokens under double_count
    chunks = chunk_text(
        text, target_tokens=target, overlap_tokens=overlap, count_tokens=double_count
    )
    assert len(chunks) > 1
    for c in chunks:
        assert double_count(c.content) <= ceiling


def test_overlap_does_not_exceed_ceiling() -> None:
    """Many small paragraphs with a large overlap budget must respect ceiling."""
    text = "\n\n".join(f"word{i}" for i in range(20))
    target = 2
    overlap = 100
    ceiling = target + overlap
    chunks = chunk_text(
        text, target_tokens=target, overlap_tokens=overlap, count_tokens=fake_count
    )
    assert len(chunks) > 1
    for c in chunks:
        assert fake_count(c.content) <= ceiling


def test_playwright_tree_fixture_respects_budget() -> None:
    """Regression: Playwright accessibility-tree-style fixture must split below ceiling.

    Models the original failure mode (`raw-nfpa25-ch11.md`): one paragraph,
    thousands of lines, zero `.!?` terminators.
    """
    fixture = Path(__file__).parent / "fixtures" / "playwright_tree.txt"
    text = fixture.read_text()
    target = 600
    overlap = 100
    ceiling = target + overlap
    chunks = chunk_text(
        text, target_tokens=target, overlap_tokens=overlap, count_tokens=fake_count
    )
    assert len(chunks) > 1
    for c in chunks:
        assert fake_count(c.content) <= ceiling, (
            f"chunk {c.index} has {fake_count(c.content)} tokens > ceiling={ceiling}"
        )
