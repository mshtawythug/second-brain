"""Unit tests for the deterministic, Ollama-free :class:`DemoEmbedder`."""
import math

from brain.demo.embedder import DEFAULT_DEMO_DIM, DemoEmbedder


def test_default_dim_is_1024() -> None:
    assert DemoEmbedder().dim == DEFAULT_DEMO_DIM == 1024


def test_custom_dim_is_honored() -> None:
    embedder = DemoEmbedder(dim=256)
    vectors = embedder.embed(["hello world"])
    assert embedder.dim == 256
    assert len(vectors[0]) == 256


def test_embed_is_deterministic() -> None:
    """The same text embeds to byte-identical vectors across instances."""
    text = "compliance horror stories"
    a = DemoEmbedder().embed([text])[0]
    b = DemoEmbedder().embed([text])[0]
    assert a == b


def test_distinct_texts_embed_differently() -> None:
    embedder = DemoEmbedder()
    a = embedder.embed(["soc 2 readiness"])[0]
    b = embedder.embed(["pci scope creep"])[0]
    assert a != b


def test_vectors_are_unit_norm() -> None:
    embedder = DemoEmbedder()
    for text in ("", "a", "the quarterly access review evidence backlog"):
        vec = embedder.embed([text])[0]
        norm = math.sqrt(sum(x * x for x in vec))
        assert math.isclose(norm, 1.0, rel_tol=1e-9, abs_tol=1e-9), (
            f"vector for {text!r} has norm {norm}, expected 1.0"
        )


def test_input_type_changes_the_vector() -> None:
    """Query-side and document-side embeddings differ (prompt asymmetry)."""
    embedder = DemoEmbedder()
    text = "vendor risk committee"
    as_doc = embedder.embed([text], input_type="document")[0]
    as_query = embedder.embed([text], input_type="query")[0]
    assert as_doc != as_query


def test_embed_empty_list_returns_empty() -> None:
    assert DemoEmbedder().embed([]) == []


def test_embed_preserves_input_order() -> None:
    embedder = DemoEmbedder()
    texts = ["alpha", "beta", "gamma"]
    batched = embedder.embed(texts)
    individually = [embedder.embed([t])[0] for t in texts]
    assert batched == individually


def test_count_tokens_matches_tiktoken() -> None:
    embedder = DemoEmbedder()
    assert embedder.count_tokens("") == 0
    assert embedder.count_tokens("hello world") > 0


def test_advertises_producing_embeddings() -> None:
    assert DemoEmbedder().produces_embeddings is True
