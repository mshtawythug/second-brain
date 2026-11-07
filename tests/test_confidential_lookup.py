"""``confidential_lookup.confidential_document_ids``: the shared F6 boundary lookup.

The real-database answer is exercised through its three callers (the UI's
snippet redaction, the hosted reranker, the document-vector backfill). Pure here:
an empty input never touches the connection.
"""
from __future__ import annotations

import pytest

from brain.confidential_lookup import confidential_document_ids

pytestmark = pytest.mark.nodb


def test_an_empty_input_short_circuits_without_a_query() -> None:
    assert confidential_document_ids(None, []) == set()  # type: ignore[arg-type]
