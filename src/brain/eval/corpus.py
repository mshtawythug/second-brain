"""EvalQuery dataclass and golden-corpus YAML loader."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .errors import EvalCorpusError

# Path to the local golden-corpus YAML, resolved relative to the package
# install root.  From corpus.py: eval/ → brain/ → src/ → repo root, then
# descend into tests/eval/.  The file is gitignored — each developer
# authors their own set tailored to their brain's seeded documents (real
# corpus = real doc IDs).  ``load_corpus`` raises ``EvalCorpusError`` when
# the default path doesn't exist; callers that want to operate without a
# local corpus should pass ``--corpus`` explicitly.
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
    (
        "query",
        "expected_doc_ids",
        "category",
        "source_filter",
        "tag_filter",
        "since_days",
        "after",
        "before",
        "notes",
    )
)
_CORPUS_VERSION = 1

# Strict ``YYYY-MM-DD``. ``date.fromisoformat`` alone is too lenient on 3.11+:
# it also accepts the ISO basic (``20261015``) and week (``2026-W42-4``) forms,
# which a corpus author would never mean and a reviewer would misread.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class EvalQuery:
    """A single query from the golden eval corpus."""

    query: str
    expected_doc_ids: list[str]  # full UUIDs OR 8-char hex prefixes; resolved at run time
    category: str  # one of _VALID_CATEGORIES
    source_filter: str | None = None  # mirrors hybrid_search's source_kind kwarg
    tag_filter: str | None = None  # mirrors hybrid_search's tag kwarg
    since_days: int | None = None  # mirrors hybrid_search's since_days kwarg
    # Absolute bounds on coalesce(sent_at, ingested_at), mirroring
    # hybrid_search's after/before: inclusive / exclusive, UTC midnight. Unlike
    # ``since_days`` they do not move with the wall clock, which is what makes a
    # ``recency`` query reproducible against a committed baseline (spec
    # docs/specs/2026-10-15-eval-recency-determinism-design.md).
    after: date | None = None
    before: date | None = None
    notes: str = ""  # human-readable rationale, not graded


def _parse_date_bound(value: Any, *, field: str, index: int) -> date | None:
    """Parse an ``after``/``before`` corpus value into a :class:`date`.

    Accepts an unquoted YAML date (``yaml.safe_load`` yields a ``date``) or a
    quoted ``"YYYY-MM-DD"`` string. A ``datetime`` is refused even though it
    subclasses ``date``: a time of day or a timezone is one more thing that can
    differ between two runs, and the CLI's ``--after``/``--before`` are
    day-granular too.

    Raises:
        EvalCorpusError: When *value* is anything other than a calendar date.
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _ISO_DATE_RE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass  # e.g. "2026-02-30" — same error as any other bad value
    raise EvalCorpusError(
        f"corpus query #{index} {field!r} must be a calendar date (YYYY-MM-DD), "
        f"got {value!r}"
    )


def _parse_since_days(value: Any, *, index: int) -> int | None:
    """Return a corpus ``since_days`` value, refusing anything but a positive int.

    Strict on purpose. The previous ``int(value)`` silently coerced ``true`` to
    1, ``40.7`` to 40 and ``'40'`` to 40, so a typo'd corpus ran a window other
    than the one written. ``bool`` is refused explicitly because it subclasses
    ``int``. ``0`` is refused because ``build_predicate`` tests
    ``if since_days:``, so 0 would silently mean *no* window while reading as
    one; negatives are refused by the CLI's ``--since`` already.

    Raises:
        EvalCorpusError: When *value* is not ``None`` and not an ``int >= 1``.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EvalCorpusError(
            f"corpus query #{index} 'since_days' must be a positive integer, got {value!r}"
        )
    return value


def _check_date_bounds(
    *, after: date | None, before: date | None, since_days: int | None, index: int
) -> None:
    """Enforce the cross-field rules that keep a pinned query deterministic.

    Raises:
        EvalCorpusError: On ``after`` without ``before`` (an open upper bound
            still admits every newly ingested document), ``after >= before``
            (the half-open range would match nothing), or ``since_days`` mixed
            with either bound (different columns, and still clock-relative).
    """
    if since_days is not None and (after is not None or before is not None):
        raise EvalCorpusError(
            f"corpus query #{index}: 'since_days' cannot be combined with "
            f"'after'/'before' — since_days filters ingested_at relative to now, "
            f"the bounds filter coalesce(sent_at, ingested_at) absolutely"
        )
    if after is not None and before is None:
        raise EvalCorpusError(
            f"corpus query #{index}: 'after' requires 'before' — an open upper "
            f"bound admits every newly ingested document, so the query still drifts"
        )
    if after is not None and before is not None and after >= before:
        raise EvalCorpusError(
            f"corpus query #{index}: 'after' must be earlier than 'before' "
            f"(got {after.isoformat()} .. {before.isoformat()}; the range is "
            f"half-open, so equal dates match nothing)"
        )


def load_corpus(path: Path | None = None) -> list[EvalQuery]:
    """Load and validate an eval corpus YAML file.

    Args:
        path: Path to the YAML file.  Defaults to ``_DEFAULT_CORPUS_PATH``.

    Returns:
        Parsed list of :class:`EvalQuery` objects.

    Raises:
        EvalCorpusError: On missing/malformed file (including an unquoted
            impossible YAML date), version mismatch, unknown category, missing
            required fields, empty ``expected_doc_ids``, or a ``since_days``
            that is not a positive ``int`` (bools, floats, quoted numbers and
            0 are refused, not coerced); and on the date-bound rules — an
            ``after``/``before`` that is not a ``YYYY-MM-DD`` calendar date,
            ``after`` without ``before``, ``after >= before``, or
            ``since_days`` combined with either bound.
    """
    if path is None:
        path = _DEFAULT_CORPUS_PATH

    if not path.exists():
        raise EvalCorpusError(f"corpus file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, ValueError) as exc:
        # ``ValueError`` too: PyYAML's timestamp constructor builds an unquoted
        # ``before: 2026-02-30`` with ``datetime.date(...)`` INSIDE safe_load,
        # so an impossible date raises a bare ValueError before
        # ``_parse_date_bound`` ever sees the value.
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

        since_days = _parse_since_days(entry.get("since_days"), index=i + 1)
        after = _parse_date_bound(entry.get("after"), field="after", index=i + 1)
        before = _parse_date_bound(entry.get("before"), field="before", index=i + 1)
        _check_date_bounds(after=after, before=before, since_days=since_days, index=i + 1)
        queries.append(
            EvalQuery(
                query=str(entry["query"]),
                expected_doc_ids=[str(doc_id) for doc_id in expected_doc_ids],
                category=category,
                source_filter=entry.get("source_filter") or None,
                tag_filter=entry.get("tag_filter") or None,
                since_days=since_days,
                after=after,
                before=before,
                notes=str(entry.get("notes", "")),
            )
        )

    return queries
