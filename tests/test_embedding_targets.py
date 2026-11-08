"""``embedding_targets``: the allowlist of reconcilable vector columns (RM-20 adds one).

Pure: the allowlist is checked before any SQL is built. All names are the
schema's own.
"""
from __future__ import annotations

import pytest

from brain.db import RELATIONAL_EMBEDDING_TARGETS
from brain.embedding_targets import (
    EMBEDDING_TARGET_ALLOWLIST,
    embedding_index_name,
    validate_embedding_target,
)
from brain.errors import BrainError

pytestmark = pytest.mark.nodb


def test_the_document_vectors_column_is_allowlisted() -> None:
    assert ("document_embeddings", "embedding") in EMBEDDING_TARGET_ALLOWLIST
    validate_embedding_target("document_embeddings", "embedding")


def test_every_relational_target_is_allowlisted() -> None:
    assert RELATIONAL_EMBEDDING_TARGETS == (
        ("chunks", "embedding"),
        ("document_embeddings", "embedding"),
    )
    assert set(RELATIONAL_EMBEDDING_TARGETS) <= EMBEDDING_TARGET_ALLOWLIST


def test_a_name_off_the_allowlist_is_refused() -> None:
    with pytest.raises(BrainError, match="not on the allowlist"):
        validate_embedding_target("document_embeddings", "summary")


def test_the_index_name_follows_the_convention() -> None:
    assert embedding_index_name("document_embeddings", "embedding") == (
        "document_embeddings_embedding_idx"
    )
