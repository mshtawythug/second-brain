"""Unit tests for brain.eval.corpus — YAML loader + EvalQuery validation."""

import textwrap
from pathlib import Path

import pytest

from brain.eval.corpus import _DEFAULT_CORPUS_PATH, EvalQuery, load_corpus
from brain.eval.errors import EvalCorpusError


def _write_corpus(tmp_path: Path, content: str) -> Path:
    """Write a YAML corpus file to tmp_path and return its path."""
    p = tmp_path / "corpus.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


_MINIMAL_VALID_CORPUS = """\
    version: 1
    queries:
      - query: "test query"
        category: semantic
        expected_doc_ids: [abcd1234]
        notes: "a test"
"""


def test_load_corpus_happy_path(tmp_path: Path) -> None:
    """A minimal valid corpus parses to a list of EvalQuery objects."""
    path = _write_corpus(tmp_path, _MINIMAL_VALID_CORPUS)
    result = load_corpus(path)
    assert len(result) == 1
    q = result[0]
    assert isinstance(q, EvalQuery)
    assert q.query == "test query"
    assert q.category == "semantic"
    assert q.expected_doc_ids == ["abcd1234"]
    assert q.notes == "a test"
    assert q.source_filter is None
    assert q.tag_filter is None
    assert q.since_days is None


def test_load_corpus_all_optional_fields(tmp_path: Path) -> None:
    """Optional fields (source_filter, tag_filter, since_days) are parsed."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - query: "krisp meetings"
            category: meeting
            expected_doc_ids: [deadbeef]
            source_filter: krisp
            tag_filter: "standup"
            since_days: 7
        """,
    )
    (q,) = load_corpus(path)
    assert q.source_filter == "krisp"
    assert q.tag_filter == "standup"
    assert q.since_days == 7


def test_load_corpus_unknown_category_raises(tmp_path: Path) -> None:
    """An unknown category value raises EvalCorpusError."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - query: "x"
            category: unknown_category
            expected_doc_ids: [abcd1234]
        """,
    )
    with pytest.raises(EvalCorpusError, match="unknown category"):
        load_corpus(path)


def test_load_corpus_missing_query_field_raises(tmp_path: Path) -> None:
    """A query entry missing the 'query' field raises EvalCorpusError."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - category: semantic
            expected_doc_ids: [abcd1234]
        """,
    )
    with pytest.raises(EvalCorpusError, match="missing required field"):
        load_corpus(path)


def test_load_corpus_missing_category_raises(tmp_path: Path) -> None:
    """A query entry missing 'category' raises EvalCorpusError."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - query: "x"
            expected_doc_ids: [abcd1234]
        """,
    )
    with pytest.raises(EvalCorpusError, match="missing required field"):
        load_corpus(path)


def test_load_corpus_missing_expected_doc_ids_field_raises(tmp_path: Path) -> None:
    """A query entry missing 'expected_doc_ids' raises EvalCorpusError."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - query: "x"
            category: semantic
        """,
    )
    with pytest.raises(EvalCorpusError, match="missing required field"):
        load_corpus(path)


def test_load_corpus_accepts_8_char_prefix_and_full_uuid(tmp_path: Path) -> None:
    """Both 8-char hex prefixes and full UUIDs parse without error.

    Resolution (DB lookup) is the runner's responsibility, not the loader's.
    """
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - query: "test"
            category: semantic
            expected_doc_ids:
              - abcd1234
              - "00000000-0000-0000-0000-000000000001"
        """,
    )
    (q,) = load_corpus(path)
    assert "abcd1234" in q.expected_doc_ids
    assert "00000000-0000-0000-0000-000000000001" in q.expected_doc_ids


def test_load_corpus_rejects_empty_expected_doc_ids(tmp_path: Path) -> None:
    """An empty expected_doc_ids list is a corpus integrity error."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - query: "uncurated"
            category: semantic
            expected_doc_ids: []
        """,
    )
    with pytest.raises(EvalCorpusError, match="empty"):
        load_corpus(path)


def test_load_corpus_version_check_wrong_version(tmp_path: Path) -> None:
    """version != 1 raises EvalCorpusError with a migration hint."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 2
        queries: []
        """,
    )
    with pytest.raises(EvalCorpusError, match="version mismatch"):
        load_corpus(path)


def test_load_corpus_version_check_missing_version(tmp_path: Path) -> None:
    """Missing version key raises EvalCorpusError."""
    path = _write_corpus(
        tmp_path,
        """\
        queries:
          - query: "x"
            category: semantic
            expected_doc_ids: [abcd1234]
        """,
    )
    with pytest.raises(EvalCorpusError, match="version mismatch"):
        load_corpus(path)


def test_load_corpus_file_not_found(tmp_path: Path) -> None:
    """Non-existent path raises EvalCorpusError."""
    missing = tmp_path / "nonexistent.yaml"
    with pytest.raises(EvalCorpusError, match="not found"):
        load_corpus(missing)


def test_load_corpus_multiple_queries(tmp_path: Path) -> None:
    """Multiple queries are all parsed and returned in order."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - query: "query one"
            category: semantic
            expected_doc_ids: [aaa00001]
          - query: "query two"
            category: people
            expected_doc_ids: [bbb00002]
          - query: "query three"
            category: email
            expected_doc_ids: [ccc00003]
        """,
    )
    results = load_corpus(path)
    assert len(results) == 3
    assert [q.query for q in results] == ["query one", "query two", "query three"]
    assert [q.category for q in results] == ["semantic", "people", "email"]


@pytest.mark.skipif(
    not _DEFAULT_CORPUS_PATH.exists(),
    reason=(
        "golden_corpus.yaml is gitignored and must be authored locally — "
        "see tests/eval/.gitignore"
    ),
)
def test_default_corpus_path_points_to_existing_file() -> None:
    """_DEFAULT_CORPUS_PATH resolves to the golden corpus YAML when present."""
    assert _DEFAULT_CORPUS_PATH.exists(), (
        f"_DEFAULT_CORPUS_PATH {_DEFAULT_CORPUS_PATH} does not exist — "
        "check the path computation in corpus.py"
    )


@pytest.mark.skipif(
    not _DEFAULT_CORPUS_PATH.exists(),
    reason=(
        "golden_corpus.yaml is gitignored and must be authored locally — "
        "see tests/eval/.gitignore"
    ),
)
def test_default_corpus_loads_successfully() -> None:
    """The local golden_corpus.yaml loads without errors."""
    queries = load_corpus()
    assert len(queries) > 0
    # All categories should be valid.
    from brain.eval.corpus import _VALID_CATEGORIES
    for q in queries:
        assert q.category in _VALID_CATEGORIES


# ---------------------------------------------------------------------------
# Defensive YAML-structural validation (lift corpus.py coverage to ≥95%)
# ---------------------------------------------------------------------------


def test_load_corpus_yaml_parse_error_raises(tmp_path: Path) -> None:
    """An unparseable YAML file raises EvalCorpusError with 'parse error'."""
    bad = tmp_path / "corpus.yaml"
    bad.write_text("queries:\n  -\tquery: bad\n", encoding="utf-8")
    with pytest.raises(EvalCorpusError, match="parse error"):
        load_corpus(bad)


def test_load_corpus_non_dict_root_raises(tmp_path: Path) -> None:
    """A YAML file whose root is a list (not a mapping) is rejected."""
    path = _write_corpus(tmp_path, "- not_a_mapping\n- still_not\n")
    with pytest.raises(EvalCorpusError, match="must be a YAML mapping"):
        load_corpus(path)


def test_load_corpus_queries_not_a_list_raises(tmp_path: Path) -> None:
    """The 'queries' key must be a list — a scalar value is rejected."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries: "not_a_list"
        """,
    )
    with pytest.raises(EvalCorpusError, match="'queries' list"):
        load_corpus(path)


def test_load_corpus_entry_not_a_mapping_raises(tmp_path: Path) -> None:
    """Each query entry must be a YAML mapping — a scalar in the list is rejected."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - just_a_string
        """,
    )
    with pytest.raises(EvalCorpusError, match="must be a YAML mapping"):
        load_corpus(path)


def test_load_corpus_expected_doc_ids_not_a_list_raises(tmp_path: Path) -> None:
    """`expected_doc_ids` must be a list — a scalar value is rejected."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - query: "x"
            category: semantic
            expected_doc_ids: not_a_list
        """,
    )
    with pytest.raises(EvalCorpusError, match="must be a list"):
        load_corpus(path)


def test_load_corpus_rejects_unknown_fields(tmp_path: Path) -> None:
    """An entry with an unrecognised field raises EvalCorpusError (spec §3.a)."""
    path = _write_corpus(
        tmp_path,
        """\
        version: 1
        queries:
          - query: "test"
            category: semantic
            expected_doc_ids: [abcd1234]
            bogus_field: "this should fail"
        """,
    )
    with pytest.raises(EvalCorpusError, match="unknown field"):
        load_corpus(path)
