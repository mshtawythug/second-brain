"""EvalQuery dataclass and golden-corpus YAML loader."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import EvalCorpusError

# Path to the bundled 20-query bootstrap corpus, resolved relative to the
# package install root.  From corpus.py: eval/ → brain/ → src/ → repo root,
# then descend into tests/eval/.
_DEFAULT_CORPUS_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tests"
    / "eval"
    / "golden_corpus.yaml"
)

_VALID_CATEGORIES: frozenset[str] = frozenset(
    (
        "people",
        "meeting",
        "email",
        "interview",
        "source-specific",
        "recency",
        "semantic",
    )
)

_REQUIRED_FIELDS: frozenset[str] = frozenset(("query", "expected_doc_ids", "category"))
_ALLOWED_FIELDS: frozenset[str] = frozenset(
    ("query", "expected_doc_ids", "category", "source_filter", "tag_filter", "since_days", "notes")
)
_CORPUS_VERSION = 1


@dataclass(frozen=True)
class EvalQuery:
    """A single query from the golden eval corpus."""

    query: str
    expected_doc_ids: list[str]  # full UUIDs OR 8-char hex prefixes; resolved at run time
    category: str  # one of _VALID_CATEGORIES
    source_filter: str | None = None  # mirrors hybrid_search's source_kind kwarg
    tag_filter: str | None = None  # mirrors hybrid_search's tag kwarg
    since_days: int | None = None  # mirrors hybrid_search's since_days kwarg
    notes: str = ""  # human-readable rationale, not graded


def load_corpus(path: Path | None = None) -> list[EvalQuery]:
    """Load and validate an eval corpus YAML file.

    Args:
        path: Path to the YAML file.  Defaults to ``_DEFAULT_CORPUS_PATH``.

    Returns:
        Parsed list of :class:`EvalQuery` objects.

    Raises:
        EvalCorpusError: On missing/malformed file, version mismatch, unknown
            category, missing required fields, or empty ``expected_doc_ids``.
    """
    if path is None:
        path = _DEFAULT_CORPUS_PATH

    if not path.exists():
        raise EvalCorpusError(f"corpus file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EvalCorpusError(f"corpus YAML parse error in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise EvalCorpusError(
            f"corpus file must be a YAML mapping, got {type(raw).__name__}"
        )

    version = raw.get("version")
    if version != _CORPUS_VERSION:
        raise EvalCorpusError(
            f"corpus version mismatch: expected {_CORPUS_VERSION}, got {version!r}. "
            f"Migrate the corpus file to version {_CORPUS_VERSION}."
        )

    raw_queries = raw.get("queries")
    if not isinstance(raw_queries, list):
        raise EvalCorpusError("corpus file must have a 'queries' list")

    queries: list[EvalQuery] = []
    for i, entry in enumerate(raw_queries):
        if not isinstance(entry, dict):
            raise EvalCorpusError(
                f"corpus query #{i + 1} must be a YAML mapping, "
                f"got {type(entry).__name__}"
            )

        missing = _REQUIRED_FIELDS - entry.keys()
        if missing:
            raise EvalCorpusError(
                f"corpus query #{i + 1} is missing required field(s): "
                f"{', '.join(sorted(missing))}"
            )

        unknown = entry.keys() - _ALLOWED_FIELDS
        if unknown:
            raise EvalCorpusError(
                f"corpus query #{i + 1} has unknown field(s): "
                f"{', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(_ALLOWED_FIELDS))}"
            )

        category = entry["category"]
        if category not in _VALID_CATEGORIES:
            raise EvalCorpusError(
                f"corpus query #{i + 1} has unknown category {category!r}. "
                f"Valid categories: {', '.join(sorted(_VALID_CATEGORIES))}"
            )

        expected_doc_ids = entry["expected_doc_ids"]
        if not isinstance(expected_doc_ids, list):
            raise EvalCorpusError(
                f"corpus query #{i + 1} 'expected_doc_ids' must be a list, "
                f"got {type(expected_doc_ids).__name__}"
            )
        if not expected_doc_ids:
            raise EvalCorpusError(
                f"corpus query #{i + 1} ({entry['query']!r}) has empty "
                f"'expected_doc_ids' — curate the corpus with "
                f"`brain search \"<query>\" --json --limit 20` before running eval"
            )

        since_raw = entry.get("since_days")
        queries.append(
            EvalQuery(
                query=str(entry["query"]),
                expected_doc_ids=[str(doc_id) for doc_id in expected_doc_ids],
                category=category,
                source_filter=entry.get("source_filter") or None,
                tag_filter=entry.get("tag_filter") or None,
                since_days=int(since_raw) if since_raw is not None else None,
                notes=str(entry.get("notes", "")),
            )
        )

    return queries
