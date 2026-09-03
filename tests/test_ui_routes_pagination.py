"""``GET /api/search`` paging — T6, the over-fetch-and-slice ruling.

``hybrid_search`` has no ``offset``/``cursor`` parameter, and adding one would
move an **eval-gated** module. The ruling was therefore option (b): ask the
ranker for ``offset + limit`` rows and slice locally.

That is not an approximation, and this module is where the claim is checked
rather than asserted. The two facts it rests on (both re-derived, not
inherited):

* ``search.py:155 CANDIDATE_LIMIT = 50`` bounds **both** ranking legs'
  candidate pools. Neither leg's ``LIMIT`` mentions the caller's ``limit``.
* ``search.py:764 return results[:effective_limit]`` is the only place
  ``limit`` is applied — a truncation of the fully-sorted list, last.
  (Re-derived 2026-08-20: this said 761, which was three lines stale. The
  citation is kept because the exact statement is the claim; it is checked by
  re-deriving it, never by inheriting it.)

So ``search(limit=o+n)[o:]`` returns the same rows, in the same order, that a
real ``OFFSET o LIMIT n`` would. The assertions below are relative comparisons
between three live responses, so they hold that property to account instead of
trusting the reasoning above.

**Why there is no "page 2 returns 25 rows" assertion.** It survives every
off-by-one this module exists to catch: ``results[offset - 1:]`` and
``results[offset + 1:]`` both still return 25 rows on an over-fetch of 50. The
load-bearing assertions are *disjointness* (no id on both pages) and
*coverage* (the two pages concatenate to the unpaginated top-50) — an
off-by-one breaks the first, a skipped row breaks the second.

No PII: the corpus is 60 synthetic notes about a nonsense term.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.config import Config
from brain.search import CANDIDATE_LIMIT, hybrid_search
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.vault import init_vault

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"

#: A lexeme that cannot collide with anything else the suite seeds. Stemming is
#: irrelevant to it, so ``plainto_tsquery`` round-trips it unchanged.
TERM = "zorblat"

#: Enough documents that the ranked set overflows a single page *and* the FTS
#: candidate pool, so page 2 is a real second page rather than a short tail.
SEEDED_DOCS = 60
PAGE = 25


@pytest.fixture
def ui_cfg(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    return Config(
        database_url="postgresql://unused/in/these/tests",
        vault_path=vault,
        embedder="none",
    )


@pytest.fixture
def client(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any
) -> TestClient:
    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    context = UiContext(
        cfg=ui_cfg,
        conn_factory=conn_factory,
        embedder=fake_embedder,
        search_fn=hybrid_search,
        allowed_origin=ORIGIN,
        logging_enabled=False,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


@pytest.fixture
def ranked_corpus(seed_doc: Any) -> list[str]:
    """60 documents that ``TERM`` matches at 60 **distinct** ``ts_rank`` values.

    The frequency ladder is not decoration. ``ORDER BY score DESC`` is not a
    total order over tied rows, so a corpus of equally-ranked documents would
    let Postgres return a different permutation per call — and then a *stable*
    implementation would fail the union assertion while a broken one might
    pass. Giving document *i* the term *i* times makes the ranking total, so
    any difference between the three responses is the slicing, which is the
    thing under test.
    """
    return [
        seed_doc(
            title=f"Synthetic note {index:03d}",
            content=" ".join([TERM] * index),
        )
        for index in range(1, SEEDED_DOCS + 1)
    ]


def _page(client: TestClient, *, limit: int, offset: int | None = None) -> Any:
    """One ``/api/search`` response body."""
    url = f"/api/search?q={TERM}&fts_only=1&limit={limit}"
    if offset is not None:
        url += f"&offset={offset}"
    response = client.get(url)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(client: TestClient, *, limit: int, offset: int | None = None) -> list[str]:
    """Ordered document ids from one ``/api/search`` response."""
    return [row["id"] for row in _page(client, limit=limit, offset=offset)["results"]]


def test_page_two_is_the_second_slice_of_the_unpaginated_ranking(
    client: TestClient, ranked_corpus: list[str]
) -> None:
    """Disjoint pages whose concatenation is the unpaginated top-50.

    Three independent claims, each broken by a different defect:

    1. no id appears on both pages — an offset that starts too early;
    2. the two pages concatenate to the unpaginated ranking — an offset that
       starts too late, or an over-fetch that never happened;
    3. page 2 **is** rows 26-50 of it, in order — a re-rank between requests.

    **Assertion order is load-bearing, and was corrected by a mutation run.**
    The anti-vacuity guard used to be ``len(unpaginated) == 50`` — but
    ``unpaginated`` is itself fetched at ``offset=0`` and therefore flows
    through the very slice under test, so BOTH off-by-one mutations corrupted
    the baseline and tripped that guard *before* reaching claims 1 and 2. The
    test went red, which looks like a pass for the mutation exercise, while the
    two load-bearing assertions were never actually shown to fire. The guard
    below reads ``total_documents`` instead — computed by a separate
    ``count(DISTINCT document_id)`` query that no slicing can touch — and the
    ordering puts each claim ahead of anything that could mask it.
    """
    first = _page(client, limit=PAGE, offset=0)
    # Anti-vacuity, and deliberately NOT derived from a sliced result list: this
    # is the lexical match total, so it stays honest under any paging defect and
    # only fails when the SEED is wrong.
    assert first["total_documents"] >= 2 * PAGE, (
        f"only {first['total_documents']} documents match {TERM!r}; there is no "
        "second page to test and every assertion below would be vacuous"
    )

    page_one = [row["id"] for row in first["results"]]
    page_two = _ids(client, limit=PAGE, offset=PAGE)

    # (1) Off-by-one too early → the boundary row lands on both pages.
    assert not set(page_one) & set(page_two), (
        "a document appears on BOTH pages — the offset slice starts at least "
        f"one row too early: overlap={sorted(set(page_one) & set(page_two))}"
    )

    unpaginated = _ids(client, limit=2 * PAGE)

    # (2) Off-by-one too late, or no over-fetch at all → a row is skipped.
    assert page_one + page_two == unpaginated, (
        "the two pages do not reconstruct the unpaginated ranking — paging "
        f"skipped a row or reordered one ({len(page_one)} + {len(page_two)} "
        f"vs {len(unpaginated)})"
    )
    # (3) The positional claim, stated separately from (2) because a re-rank
    # between the two requests breaks this while (2) could still hold.
    assert page_two == unpaginated[PAGE : 2 * PAGE]
    assert len(set(unpaginated)) == len(unpaginated), (
        "the unpaginated ranking repeats a document; paging cannot be "
        "meaningfully compared against it"
    )


def test_omitting_offset_is_page_one(
    client: TestClient, ranked_corpus: list[str]
) -> None:
    """``offset`` is optional and defaults to 0.

    The ledger shipped before paging existed and its requests carry no
    ``offset``; a default that was not 0 would silently move every existing
    caller's first page.
    """
    assert _ids(client, limit=PAGE) == _ids(client, limit=PAGE, offset=0)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("-1", "invalid_offset"),
        ("abc", "invalid_offset"),
        ("2.5", "invalid_offset"),
        (str(2 * CANDIDATE_LIMIT + 1), "invalid_offset"),
    ],
)
def test_a_bad_offset_is_a_400_not_a_silent_clamp(
    client: TestClient, raw: str, code: str
) -> None:
    """Fail-closed, per the module contract in ``ui/schemas.py``.

    A clamped offset is indistinguishable from a working one, so a client
    paging past the end would be handed page 1 forever and read it as "no more
    results".
    """
    response = client.get(f"/api/search?q={TERM}&fts_only=1&offset={raw}")
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code


# ``MAX_OFFSET == 2 * CANDIDATE_LIMIT`` is NOT asserted here, deliberately.
# ``tests/test_ui_search_ceiling.py`` already pins it — as two named steps
# (``MAX_RANKED_DOCUMENTS == 2 * CANDIDATE_LIMIT`` and
# ``MAX_OFFSET == MAX_RANKED_DOCUMENTS``), which says more than the collapsed
# form did. The copy that stood here took the DB-backed ``client`` fixture and
# never used it, so a pure-arithmetic check over three integers pulled the
# MACHINE-WIDE advisory lock and the schema reset in behind it. The ceiling
# module is ``nodb``-marked and needs neither.


def test_an_empty_page_past_the_ranked_ceiling_reports_ceiling_not_exhaustion(
    client: TestClient, ranked_corpus: list[str]
) -> None:
    """Defect #27, end to end against a real ranking.

    ``SEEDED_DOCS`` documents match ``TERM`` and the FTS leg's candidate pool is
    bounded at ``CANDIDATE_LIMIT`` chunks, so the ranked set is strictly smaller
    than the match total. A page starting past the ranked set is empty — and
    before this key, an empty page carrying ``total_documents`` in the sixties
    was the ONLY thing a client got. The ledger rendered it as "No notes
    matched", which is false in the one direction that matters: it tells a
    reader whose query matched everything to go and broaden it.

    Run ``fts_only`` so the assertion is about the ceiling and not about
    whichever near-neighbours a vector leg happened to add.
    """
    deep = _page(client, limit=PAGE, offset=2 * PAGE)

    # Anti-vacuity, in the order the claims depend on each other. Without the
    # first two, "status == ceiling" could be true of a corpus that never
    # overflowed anything.
    assert deep["total_documents"] == SEEDED_DOCS, (
        f"the seed changed: {deep['total_documents']} documents match {TERM!r}, "
        f"expected {SEEDED_DOCS}"
    )
    ranked = deep["ranking"]["ranked_documents"]
    assert ranked < deep["total_documents"], (
        f"the ranked set ({ranked}) covers every match ({deep['total_documents']}), "
        "so there is no ceiling here and this test proves nothing — seed more "
        "documents than CANDIDATE_LIMIT can rank"
    )
    assert deep["results"] == [], (
        f"expected an empty page at offset {2 * PAGE} over a ranked set of "
        f"{ranked}; got {len(deep['results'])} rows"
    )

    assert deep["ranking"]["status"] == "ceiling", (
        "an empty page past the ranked ceiling reported "
        f"{deep['ranking']['status']!r} — 'exhausted' here tells the reader "
        f"they have seen all {deep['total_documents']} matches when they have "
        f"seen at most {ranked}"
    )
    assert deep["ranking"]["max_ranked_documents"] == 2 * CANDIDATE_LIMIT

    # ``ranked_documents`` IS A FACT ABOUT THE RANKER, NOT ABOUT THIS SLICE, and
    # this assertion is here because a mutation proved the rest of the test
    # could not tell the difference. Wiring it to ``len(page)`` instead of
    # ``len(results)`` left every assertion above green: on this page the slice
    # is empty, and 0 is still "fewer than the ranked set" and still "fewer than
    # the total", so the status came out ``ceiling`` for the wrong reason.
    # The page is EMPTY and the ranked count must still be positive.
    assert ranked > 0, (
        f"the page is empty yet `ranked_documents` is {ranked} — the count is "
        "reporting the size of this slice rather than the size of the ranking, "
        "so the ledger would tell the reader 0 of "
        f"{deep['total_documents']} were ranked"
    )


def test_a_middle_page_with_ranked_rows_after_it_reports_more(
    client: TestClient, ranked_corpus: list[str]
) -> None:
    """The third state, and the second half of the mutation that got away.

    Page 2 of a ranking that runs past it is ``more`` — there are further ranked
    rows, the ceiling has nothing to do with this page. Reading the count off
    the SLICE rather than off the ranking flips this to ``ceiling``, because a
    25-row slice is smaller than a 50-row over-fetch, and the ledger would then
    print an end-of-results note in the middle of a result set.
    """
    middle = _page(client, limit=PAGE, offset=PAGE)

    assert len(middle["results"]) == PAGE, (
        "page 2 did not fill; there is no 'middle' here and the claim below is "
        "not the one being tested"
    )
    assert middle["ranking"]["status"] == "more", (
        f"page 2 reported {middle['ranking']['status']!r} while returning a "
        "full page of rows"
    )
    assert middle["ranking"]["ranked_documents"] == 2 * PAGE, (
        "the over-fetch for page 2 is offset+limit rows and it filled, so the "
        "ranked count is that whole over-fetch — not the "
        f"{len(middle['results'])} rows this page shows"
    )


def test_a_first_page_that_fills_its_over_fetch_reports_more(
    client: TestClient, ranked_corpus: list[str]
) -> None:
    """The other side of the gate, so 'ceiling' is not simply what this route
    always says.

    A boolean that only ever takes one value is indistinguishable from a
    constant, and a constant explains nothing.
    """
    first = _page(client, limit=PAGE, offset=0)

    assert len(first["results"]) == PAGE, (
        "the first page did not fill, so 'more' is not the state under test"
    )
    assert first["ranking"]["status"] == "more"
    assert first["ranking"]["ranked_documents"] == PAGE
