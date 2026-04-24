"""Unit tests for the paragraph-aware chunker."""
from brain.ingest.chunker import Chunk, chunk_text


def fake_count(text: str) -> int:
    # simple word count for predictable chunking
    return max(1, len(text.split()))


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
