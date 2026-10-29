"""``ProgressEmbedder``: progress, cancel points and the C5 flag forwards (Ingest spec §5.3).

Pure tier: a recording handle and a fake inner embedder, no database.
"""
from __future__ import annotations

from typing import Any

import pytest

from brain.embeddings import is_hosted_embedder
from brain.ui.ingest_progress import ProgressEmbedder
from brain.ui.jobs import JobCancelled

pytestmark = pytest.mark.nodb


class _Handle:
    def __init__(self, *, cancel_at_check: int | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._checks = 0
        self._cancel_at_check = cancel_at_check
        self.cancel_now = False

    def progress(self, phase: str, *, done: int | None = None, total: int | None = None) -> None:
        self.calls.append(("progress", phase, done, total))

    def note(self, code: str) -> None:
        self.calls.append(("note", code))

    def check_cancelled(self) -> None:
        self._checks += 1
        self.calls.append(("check",))
        if self.cancel_now or self._checks == self._cancel_at_check:
            raise JobCancelled("x")

    def enter_commit(self) -> None:
        self.calls.append(("commit",))
        if self.cancel_now:
            raise JobCancelled("x")


class _Inner:
    dim = 3

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        self.batches.append(list(texts))
        return [[float(len(t)), 0.0, 1.0] for t in texts]

    def count_tokens(self, text: str) -> int:
        return len(text)


def test_dim_and_count_tokens_delegate() -> None:
    wrapped = ProgressEmbedder(_Inner(), _Handle())
    assert wrapped.dim == 3
    assert wrapped.count_tokens("abcd") == 4


def test_embed_slices_and_returns_the_same_vectors() -> None:
    inner = _Inner()
    handle = _Handle()
    texts = [f"t{i}" * (i + 1) for i in range(5)]

    out = ProgressEmbedder(inner, handle, batch_size=2).embed(texts, input_type="document")

    assert out == _Inner().embed(texts)
    assert inner.batches == [texts[0:2], texts[2:4], texts[4:5]]
    assert handle.calls == [
        ("progress", "embedding", 0, 5),
        ("check",), ("progress", "embedding", 2, 5),
        ("check",), ("progress", "embedding", 4, 5),
        ("check",), ("progress", "embedding", 5, 5),
        ("commit",), ("progress", "finishing", None, None),
    ]


def test_a_cancel_before_a_batch_stops_before_that_batch() -> None:
    inner = _Inner()
    handle = _Handle(cancel_at_check=2)

    with pytest.raises(JobCancelled):
        ProgressEmbedder(inner, handle, batch_size=2).embed(["a", "b", "c"])

    assert inner.batches == [["a", "b"]]
    assert ("commit",) not in handle.calls


def test_enter_commit_runs_after_the_last_batch_and_can_refuse() -> None:
    handle = _Handle()

    class _LateCancel(_Inner):
        def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
            handle.cancel_now = True
            return super().embed(texts, input_type=input_type)

    with pytest.raises(JobCancelled):
        ProgressEmbedder(_LateCancel(), handle, batch_size=16).embed(["only"])
    assert handle.calls[-1] == ("commit",)


def test_an_empty_embed_still_closes_the_window() -> None:
    handle = _Handle()
    assert ProgressEmbedder(_Inner(), handle).embed([]) == []
    assert handle.calls[-2:] == [("commit",), ("progress", "finishing", None, None)]


def test_input_type_is_forwarded() -> None:
    seen: list[str] = []

    class _Recording(_Inner):
        def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
            seen.append(input_type)
            return super().embed(texts, input_type=input_type)

    ProgressEmbedder(_Recording(), _Handle()).embed(["x"], input_type="query")
    assert seen == ["query"]


@pytest.mark.parametrize("value", [True, False])
def test_hosted_egress_is_forwarded(value: bool) -> None:
    """C5: dropping this forward would silently disable the F6 egress veto."""
    inner = _Inner()
    inner.hosted_egress = value  # type: ignore[attr-defined]

    wrapped = ProgressEmbedder(inner, _Handle())

    assert is_hosted_embedder(wrapped) is value


@pytest.mark.parametrize("value", [True, False])
def test_produces_embeddings_is_forwarded(value: bool) -> None:
    inner = _Inner()
    inner.produces_embeddings = value  # type: ignore[attr-defined]

    wrapped = ProgressEmbedder(inner, _Handle())

    assert getattr(wrapped, "produces_embeddings", True) is value


def test_absent_flags_take_the_readers_defaults() -> None:
    wrapped = ProgressEmbedder(_Inner(), _Handle())
    assert is_hosted_embedder(wrapped) is False
    assert getattr(wrapped, "produces_embeddings", True) is True


def test_batch_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ProgressEmbedder(_Inner(), _Handle(), batch_size=0)
