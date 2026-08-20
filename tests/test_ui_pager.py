"""The pagination control's decisions, EXECUTED rather than grepped.

THE DEFECT. ``GET /api/search`` has accepted an ``offset`` since T6 and the
front end never sent one — re-derived at the time this module was written by
grepping ``static/`` for ``offset``, which matched three stylesheets'
``outline-offset`` and one comment in ``inspector.js`` and nothing else. So the
ledger printed a total ("544 notes"), rendered the first ``limit`` rows, and
every row past them was unreachable from the UI. #27 then added
``ranking.status``, which explains why a page ended: an explanation of a
boundary on a page nobody could navigate to.

``static/js/pager.js`` is the decision half of the control, split out of
``results.js`` for the reason ``tests/test_ui_static_behaviour.py`` states at
length — this repository has shipped roughly a dozen source-shaped guards that
could not fail, and ``results.js`` cannot be executed here because it touches
``document`` immediately. Node is used exactly as
``tests/test_ui_ledger_status.py`` and ``tests/test_ui_tree_nav.py`` use it: the
source is copied into a tmp ``.mjs`` beside a harness, so no repo
``package.json`` ``type`` setting can change how the module is parsed.

NO BOUND IS WRITTEN DOWN IN THIS FILE either — no ``100``, no ``50``, no
``MAX_OFFSET`` restated as a literal. The single place the ceiling arithmetic is
touched is :func:`test_a_more_status_can_never_advertise_an_offset_the_server_
would_reject`, and it derives every quantity from ``brain.ui.schemas``.

No PII: every payload below is integers and status strings.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from brain.ui import schemas
from brain.ui.app import static_dir

#: Opens NO database connection — copies a JS file to a tmp dir and runs node.
#: Keeps this file off the MACHINE-WIDE advisory lock and the schema reset.
pytestmark = pytest.mark.nodb

PAGER = Path(str(static_dir())) / "js" / "pager.js"

HARNESS = """\
import { pagerModel, MORE, EXHAUSTED, CEILING, UNKNOWN } from "./pager.mjs";

const failures = [];
function eq(actual, expected, what) {
  if (actual !== expected) {
    failures.push(`${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}
function has(actual, needle, what) {
  if (typeof actual !== "string" || !actual.includes(needle)) {
    failures.push(
      `${what}: expected ${JSON.stringify(actual)} to contain ${JSON.stringify(needle)}`);
  }
}

/* A page of a ranking, with only the keys the model reads. `limit` is 25 and
   `returned` is a full page unless a case says otherwise — every number here is
   chosen by this harness, none of them is a bound copied from the server. */
function page(status, { offset = 0, limit = 25, returned = 25, ranked = 50 } = {}) {
  return {
    offset, limit, returned,
    total_documents: 544,
    ranking: { status, ranked_documents: ranked, max_ranked_documents: 100 },
  };
}

/* --- can the reader move at all -------------------------------------- */
/* THE DEFECT, as one assertion: a search whose ranker has more to give must
   offer a way to reach it. */
eq(pagerModel(page(MORE)).canNext, true,
   "a page the ranker did not finish must offer a next page");
eq(pagerModel(page(MORE)).visible, true,
   "a pager with a live Next must be on screen");
eq(pagerModel(page(MORE)).nextOffset, 25,
   "Next asks for the row after the last one shown");
eq(pagerModel(page(MORE)).canPrev, false,
   "page one has nothing before it");

const second = pagerModel(page(MORE, { offset: 25 }));
eq(second.canPrev, true, "a page past the first can go back");
eq(second.prevOffset, 0, "Previous from page two lands on page one");
eq(second.nextOffset, 50, "Next from page two lands on page three");

/* --- one short page is not a pager ------------------------------------ */
/* Both buttons dead is furniture: it says only "this is all of it", which the
   ledger's own count already says. */
eq(pagerModel(page(EXHAUSTED, { returned: 3, ranked: 3 })).visible, false,
   "a single exhausted page must not grow a dead control");
eq(pagerModel(page(CEILING, { returned: 3, ranked: 3 })).visible, false,
   "nor does a single page that the ranker cut short");
/* ...but the SAME ending on a later page keeps the pager, because Previous is
   still live and the reader has to be able to get back. */
eq(pagerModel(page(EXHAUSTED, { offset: 25, returned: 3 })).visible, true,
   "the last page of a multi-page search must still offer Previous");

/* --- the boundary must be legible, and the two endings distinct -------- */
/* THE REQUIREMENT. At the ceiling the reader has NOT seen everything that
   matched; at exhaustion they have. A control that spelled those the same way
   would be a lie told by the navigation. */
const atCeiling = pagerModel(page(CEILING, { offset: 75, returned: 12 }));
const atEnd = pagerModel(page(EXHAUSTED, { offset: 75, returned: 12 }));
eq(atCeiling.canNext, false, "the ceiling is the end of what Next can reach");
eq(atEnd.canNext, false, "so is exhaustion");
if (atCeiling.boundary === atEnd.boundary) {
  failures.push("the ceiling and the end of the results read identically");
}
if (!atCeiling.boundary) failures.push("the ceiling boundary says nothing at all");
if (!atEnd.boundary) failures.push("the exhausted boundary says nothing at all");
has(atCeiling.boundary, "rank", "the ceiling names RANKING as what ran out");
/* The exhausted wording must not claim a ranking limit, and the ceiling wording
   must not claim the results ended. Asserted as the two directions rather than
   as one inequality, which `!==` above already covers. */
if (/rank/i.test(atEnd.boundary)) {
  failures.push("exhaustion was described as a ranking limit");
}

const undetermined = pagerModel(page(UNKNOWN, { offset: 25, returned: 4 }));
if (!undetermined.boundary) failures.push("an undetermined ending says nothing");
/* `total_documents` was unavailable, so the server could not tell the two
   endings apart. Claiming the reader has seen everything is the one direction
   that misleads. */
eq(undetermined.boundary, pagerModel(page("a-status-from-the-future",
   { offset: 25, returned: 4 })).boundary,
   "an unrecognised status must fall back to the non-committal wording");
if (undetermined.boundary === atEnd.boundary) {
  failures.push("an undetermined ending was reported as the end of the results");
}

/* While Next is live there is no ending to explain yet. */
eq(pagerModel(page(MORE)).boundary, "", "a page with more to come explains nothing");

/* --- the range the reader is looking at ------------------------------- */
eq(pagerModel(page(MORE)).rangeLabel, "1–25", "page one is rows 1 to 25");
eq(pagerModel(page(MORE, { offset: 25 })).rangeLabel, "26–50", "page two continues");
/* Over the rows ACTUALLY returned, not over `limit`: the last page is short and
   "76–100" on a page showing twelve rows describes a page that does not exist. */
eq(pagerModel(page(EXHAUSTED, { offset: 75, returned: 12 })).rangeLabel, "76–87",
   "a short last page reports the rows it actually has");
eq(pagerModel(page(EXHAUSTED, { offset: 25, returned: 0 })).rangeLabel, "",
   "an empty page has no range, and '26-25' is not one");

/* --- payloads that must not throw ------------------------------------- */
/* A server older than #27 sends no `ranking` key, and it also predates any way
   to know whether a next page exists. The ledger keeps the shape it had. */
eq(pagerModel({ offset: 0, limit: 25, returned: 25 }).visible, false,
   "no `ranking` key -> no pager, rather than a Next that may lead nowhere");
eq(pagerModel(null).visible, false, "a null payload must not throw");
eq(pagerModel({}).visible, false, "an empty payload must not throw");
eq(pagerModel(undefined).visible, false, "an absent payload must not throw");

/* A zero limit would make Next advertise the offset it is already on — a live
   control that does nothing. */
eq(pagerModel(page(MORE, { limit: 0 })).visible, false, "a zero limit is no pager");

/* Nothing the model computes may reach a URL as NaN or as a negative. */
const junk = pagerModel({ offset: "nonsense", limit: 25, returned: 25,
                          ranking: { status: MORE, ranked_documents: 50,
                                     max_ranked_documents: 100 } });
eq(junk.nextOffset, 25, "an unparseable offset is read as page one, never NaN");
const negative = pagerModel({ offset: -5, limit: 25, returned: 25,
                              ranking: { status: MORE, ranked_documents: 50,
                                         max_ranked_documents: 100 } });
eq(negative.prevOffset, 0, "Previous can never ask for a negative offset");
eq(negative.nextOffset, 25, "a negative offset is read as page one");

/* The model is a FRESH object per call — a caller that writes to one must not
   change what the next render sees.
   ASSERTED ON THE NO-PAGER MODEL, and the first version of this check was on
   the visible one, which could not fail: the visible branch builds a new object
   literal every call, so there is nothing there to share. The hazard is real on
   this branch alone, because "nothing to navigate" is the one answer a module
   is tempted to return from a single hoisted constant. Caught by the mutation
   run — the shared-object mutation left the harness GREEN until this moved. */
const blank = pagerModel(null);
blank.visible = true;
blank.canNext = true;
eq(pagerModel(null).visible, false,
   "the empty model is shared between calls, so one caller's write leaks into the next");
eq(pagerModel({}).canNext, false, "and the same object is handed to every caller");

if (failures.length) {
  console.error(failures.join("\\n"));
  process.exit(1);
}
console.log("ok");
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover — environment-dependent
        pytest.skip("node is required for the pager.js runtime harness")
    return node


def _run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pager.mjs").write_text(source, encoding="utf-8")
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    return subprocess.run(
        [_node(), str(harness)], capture_output=True, text=True, check=False
    )


def test_the_pager_can_reach_every_page_the_ranker_produced(tmp_path: Path) -> None:
    """Run every payload shape against the real module."""
    result = _run(tmp_path, PAGER.read_text(encoding="utf-8"))
    assert result.returncode == 0, result.stderr or result.stdout


def test_the_javascript_status_names_match_the_python_ones() -> None:
    """The duplicated fact, across a language boundary, asserted.

    ``pager.js`` re-declares all four of ``schemas``' status strings because
    four literals do not justify a generated constants file — the same trade
    ``ledger_status.js`` records for the two it needs. A rename on the Python
    side that did not reach the JS would leave the control failing to recognise
    an ending: silently, and in the direction that offers a Next button on a
    page that has none.
    """
    source = PAGER.read_text(encoding="utf-8")
    for constant in (
        schemas.RANKING_MORE,
        schemas.RANKING_EXHAUSTED,
        schemas.RANKING_CEILING,
        schemas.RANKING_UNKNOWN,
    ):
        assert f'"{constant}"' in source, (
            f"schemas defines the status {constant!r} but pager.js does not "
            "mention it — the control cannot reason about an ending it cannot name"
        )


def test_a_more_status_can_never_advertise_an_offset_the_server_would_reject() -> None:
    """The implication ``pager.js`` rests on, checked where the constants live.

    ``pager.js`` decides "is there a next page?" as ``status === MORE`` and
    computes ``nextOffset = offset + limit`` with NO bound of its own — that
    absence is the design, because a bound in JavaScript is a second copy of
    ``MAX_OFFSET`` to keep true. What makes the absence safe is an implication
    about the two together, and an implication nobody checks is an assumption:

    * ``ranking_status`` returns ``more`` only when ``ranked >= fetch_limit``,
      and ``fetch_limit`` IS the ``offset + limit`` the pager would ask for next;
    * the ranker cannot return more than ``MAX_RANKED_DOCUMENTS`` rows, because
      both legs bound their candidate pools at ``CANDIDATE_LIMIT``;
    * ``MAX_OFFSET`` is ``MAX_RANKED_DOCUMENTS``.

    So every ``more`` is reported at an ``offset + limit`` that ``_parse_offset``
    accepts. Break the last link — set ``MAX_OFFSET`` below
    ``MAX_RANKED_DOCUMENTS`` — and this test goes red while every JS assertion
    above stays green, which is exactly the division of labour intended: the
    harness holds the control's behaviour, this holds the premise it relies on.

    Every quantity is READ from :mod:`brain.ui.schemas`; nothing is restated.
    """
    ceiling = schemas.MAX_RANKED_DOCUMENTS

    # THE LINK BETWEEN THE TWO CONSTANTS, asserted rather than assumed. The
    # loop below sweeps `ranked` only as far as MAX_RANKED_DOCUMENTS, which is
    # the whole range a ranker can produce; that sweep says nothing about
    # MAX_OFFSET unless the two are the same quantity. Separate them and this
    # line fails first, naming the cause, instead of the loop failing on an
    # arbitrary-looking fetch_limit.
    assert ceiling == schemas.MAX_OFFSET

    for fetch_limit in range(1, ceiling + 2):
        for ranked in range(0, ceiling + 1):
            if schemas.ranking_status(
                ranked=ranked, fetch_limit=fetch_limit, total_documents=None
            ) != schemas.RANKING_MORE:
                continue
            assert fetch_limit <= schemas.MAX_OFFSET, (
                f"ranking_status reports 'more' at fetch_limit={fetch_limit} "
                f"(ranked={ranked}), so the pager would request "
                f"offset={fetch_limit} — past MAX_OFFSET={schemas.MAX_OFFSET}, "
                "which _parse_offset answers with a 400. pager.js relies on "
                "this never happening and carries no bound of its own."
            )


@pytest.mark.parametrize(
    ("anchor", "replacement", "what"),
    [
        (
            "  const canNext = ranking.status === MORE;",
            "  const canNext = false;",
            "Next being offered when the ranker has more to give",
        ),
        (
            "  const canPrev = offset > 0;",
            "  const canPrev = false;",
            "Previous being offered on a page past the first",
        ),
        (
            "  if (!canPrev && !canNext) return noPager();",
            "  if (false) return noPager();",
            "a single short page being left without a dead control",
        ),
        (
            "    boundary: canNext ? \"\" : (BOUNDARY[ranking.status] "
            "|| BOUNDARY[UNKNOWN]),",
            "    boundary: canNext ? \"\" : BOUNDARY[EXHAUSTED],",
            "the ceiling reading differently from the end of the results",
        ),
        (
            "  [UNKNOWN]: \"End of the ranked results\",",
            "  [UNKNOWN]: \"End of results\",",
            "an undetermined ending not being reported as exhaustion",
        ),
        (
            "    nextOffset: offset + limit,",
            "    nextOffset: offset + limit + 1,",
            "Next landing on the row after the last one shown",
        ),
        (
            "    prevOffset: Math.max(0, offset - limit),",
            "    prevOffset: offset - limit,",
            "Previous being clamped away from a negative offset",
        ),
        (
            "    rangeLabel: returned > 0 ? `${offset + 1}–${offset + returned}` : \"\",",
            "    rangeLabel: `${offset + 1}–${offset + limit}`,",
            "the range describing the rows actually returned",
        ),
        (
            "  if (!ranking) return noPager();",
            "  if (false) return noPager();",
            "a payload with no `ranking` key not throwing",
        ),
        (
            "  if (limit <= 0) return noPager();",
            "  if (false) return noPager();",
            "a zero limit not producing a control that cannot move",
        ),
        (
            "  if (!Number.isFinite(number) || number < 0) return 0;",
            "  if (false) return 0;",
            "an unparseable or negative offset never reaching a URL",
        ),
        (
            "function noPager() {\n  return {",
            "const _SHARED = {\n  visible: false, canPrev: false, canNext: false,\n"
            "  prevOffset: 0, nextOffset: 0, rangeLabel: \"\", boundary: \"\",\n"
            "};\nfunction noPager() {\n  return _SHARED;\n}\nfunction _unused() {\n"
            "  return {",
            "each call getting a model of its own",
        ),
    ],
)
def test_the_harness_can_actually_fail(
    tmp_path: Path, anchor: str, replacement: str, what: str
) -> None:
    """Guard the guard: break each behaviour and prove the harness goes red.

    Clause order is copied from ``tests/test_ui_ledger_status.py``, which copied
    it from ``tests/test_ui_tree_nav.py``: the anchor must exist, the
    substitution must have applied, and only THEN is a red result evidence of
    anything. A mutation whose anchor silently missed produces a guard that
    certifies nothing while looking green.

    The last entry is the odd one and says so: the shared-object mutation cannot
    be expressed as a one-line replacement, so it rewrites ``noPager`` to return
    one module-level object and leaves the original literal behind in a dead
    function purely to keep the file parseable.
    """
    source = PAGER.read_text(encoding="utf-8")
    assert anchor in source, f"the mutation anchor for {what} no longer exists"
    mutated = source.replace(anchor, replacement, 1)
    assert mutated != source, f"the {what} mutation did not apply"

    result = _run(tmp_path, mutated)
    assert result.returncode != 0, (
        f"breaking {what} left the harness GREEN — it is not testing that "
        "behaviour"
    )
