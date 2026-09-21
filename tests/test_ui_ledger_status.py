"""Defect #27's ledger half: the sentence a reader gets when a page ends.

The server now reports WHY a search page ended (``payload.ranking.status``;
``brain.ui.schemas.ranking_status``). This module runs the JavaScript that turns
that into words — `static/js/ledger_status.js` — rather than grepping it.

Executed, not inspected, for the reason ``tests/test_ui_static_behaviour.py``
states at length: this repository has shipped roughly a dozen source-shaped
guards that could not fail. ``results.js`` itself cannot be run here (it touches
``document`` immediately), which is precisely why the decision was extracted
into a module with no DOM and no imports — the same split, for the same reason,
as ``tree_nav.js`` out of ``tree.js``.

Node is used the same way ``tests/test_ui_tree_nav.py`` uses it: the source is
copied into a tmp ``.mjs`` beside a harness, so no repo ``package.json`` ``type``
setting can change how the module is parsed.

No PII: the payloads below are integers and one nonsense query.
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

LEDGER_STATUS = Path(str(static_dir())) / "js" / "ledger_status.js"

HARNESS = """\
import { emptyLedgerMessage, ceilingNote, CEILING, UNKNOWN } from "./ledger_status.mjs";

const failures = [];
function has(actual, needle, what) {
  if (typeof actual !== "string" || !actual.includes(needle)) {
    failures.push(
      `${what}: expected ${JSON.stringify(actual)} to contain ` +
      JSON.stringify(needle));
  }
}
function lacks(actual, needle, what) {
  if (typeof actual === "string" && actual.includes(needle)) {
    failures.push(
      `${what}: expected ${JSON.stringify(actual)} NOT to contain ` +
      JSON.stringify(needle));
  }
}
function eq(actual, expected, what) {
  if (actual !== expected) {
    failures.push(`${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const NO_MATCH = "No notes matched";

/* --- the empty page, and the whole point of the defect ------------------ */
/* An empty page because the RANKER STOPPED LOOKING must not be reported the
   same way as an empty page because nothing matched. These two payloads differ
   only in `status`, so any message that ignores it fails one of them. */
const ceiling = {
  total_documents: 544,
  ranking: { status: CEILING, ranked_documents: 0, max_ranked_documents: 100 },
};
const exhausted = {
  total_documents: 0,
  ranking: { status: "exhausted", ranked_documents: 0, max_ranked_documents: 100 },
};

lacks(emptyLedgerMessage(ceiling), NO_MATCH,
  "an empty page past the ceiling must not claim nothing matched");
has(emptyLedgerMessage(ceiling), "544",
  "the reader is told how many notes DID match");
has(emptyLedgerMessage(ceiling), "100",
  "the reader is told what the ranking ceiling is");
has(emptyLedgerMessage(exhausted), NO_MATCH,
  "a genuinely empty result set keeps the message it always had");

/* The distinction itself, stated as one assertion so it cannot be satisfied by
   two messages that happen to differ in punctuation. */
if (emptyLedgerMessage(ceiling) === emptyLedgerMessage(exhausted)) {
  failures.push("ceiling and exhaustion produce the SAME empty-page message");
}

/* --- the count is unavailable ------------------------------------------ */
/* `total_documents` is null when the count query failed. Printing "0 notes
   matched" under a ceiling would contradict itself on its face. */
const noTotal = {
  total_documents: null,
  ranking: { status: CEILING, ranked_documents: 0, max_ranked_documents: 100 },
};
lacks(emptyLedgerMessage(noTotal), "0 notes", "a null total must not render as zero");
lacks(emptyLedgerMessage(noTotal), NO_MATCH, "a null total must not claim nothing matched");
/* ADDED AFTER A MUTATION RUN, and it is the catcher rather than a flourish.
   Deleting the null-guard in `describeTotal` makes the message read "null
   notes"; the two assertions above both still passed, because neither "0 notes"
   nor "No notes matched" appears in it. The counterfactual was run: with this
   line removed and the guard mutated, the harness stayed GREEN. */
lacks(emptyLedgerMessage(noTotal), "null",
  "an absent total must not leak `null` into the sentence");
lacks(emptyLedgerMessage(noTotal), "undefined", "nor `undefined`");

const unknown = {
  total_documents: null,
  ranking: { status: UNKNOWN, ranked_documents: 4, max_ranked_documents: 100 },
};
lacks(emptyLedgerMessage(unknown), NO_MATCH,
  "an undetermined ending must not be reported as 'nothing matched'");

/* --- degrading to a server that predates #27 ---------------------------- */
eq(emptyLedgerMessage({ total_documents: 0 }), "No notes matched. Try fewer filters.",
   "no `ranking` key -> the message the ledger always had");
eq(emptyLedgerMessage(null), "No notes matched. Try fewer filters.",
   "a null payload must not throw");
eq(emptyLedgerMessage({}), "No notes matched. Try fewer filters.",
   "an empty payload must not throw");

/* --- the trailing note on a NON-empty page ------------------------------ */
/* The ceiling can also be hit on a partially-filled last page: 87 ranked, page
   starting at 75, twelve rows and no more to come. */
const partial = {
  total_documents: 544,
  ranking: { status: CEILING, ranked_documents: 87, max_ranked_documents: 100 },
};
has(ceilingNote(partial), "87", "the note reports how many were ranked");
has(ceilingNote(partial), "544", "the note reports how many matched");
has(ceilingNote(partial), "100", "the note reports the ceiling");

eq(ceilingNote({ total_documents: 544,
                 ranking: { status: "more", ranked_documents: 25, max_ranked_documents: 100 } }),
   "", "a page with more to come gets no end-of-ranking note");
eq(ceilingNote({ total_documents: 3,
                 ranking: { status: "exhausted", ranked_documents: 3,
                            max_ranked_documents: 100 } }),
   "", "a genuinely exhausted page gets no ceiling note");
eq(ceilingNote(null), "", "a null payload yields no note");
eq(ceilingNote({}), "", "a payload with no `ranking` key yields no note");

if (failures.length) {
  console.error(failures.join("\\n"));
  process.exit(1);
}
console.log("ok");
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover — environment-dependent
        pytest.skip("node is required for the ledger_status.js runtime harness")
    return node


def _run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "ledger_status.mjs").write_text(source, encoding="utf-8")
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    return subprocess.run(
        [_node(), str(harness)], capture_output=True, text=True, check=False
    )


def test_the_ledger_distinguishes_a_ceiling_from_an_empty_result_set(
    tmp_path: Path,
) -> None:
    """Run every payload shape against the real module."""
    result = _run(tmp_path, LEDGER_STATUS.read_text(encoding="utf-8"))
    assert result.returncode == 0, result.stderr or result.stdout


def test_the_javascript_status_names_match_the_python_ones() -> None:
    """The one duplicated fact, across a language boundary, asserted.

    ``ledger_status.js`` re-declares two of ``schemas``' status strings because
    four literals do not justify a generated constants file. A rename on the
    Python side that did not reach the JS would leave the ledger quietly failing
    to recognise the status — which is this defect again, one layer up.
    """
    source = LEDGER_STATUS.read_text(encoding="utf-8")
    for constant in (schemas.RANKING_CEILING, schemas.RANKING_UNKNOWN):
        assert f'"{constant}"' in source, (
            f"schemas defines the status {constant!r} but ledger_status.js does "
            "not mention it — the ledger cannot render a status it does not name"
        )


@pytest.mark.parametrize(
    ("anchor", "replacement", "what"),
    [
        (
            "  if (status === CEILING) {",
            "  if (false) {",
            "the ceiling branch of the empty-page message",
        ),
        (
            "  if (status === UNKNOWN) {",
            "  if (false) {",
            "the undetermined-ending branch of the empty-page message",
        ),
        (
            '  if (!ranking || ranking.status !== CEILING) return "";',
            "  if (!ranking) return \"\";",
            "ceilingNote refusing to fire on a non-ceiling page",
        ),
        (
            '  return total == null ? "more notes" : `${total} notes`;',
            "  return `${total} notes`;",
            "a null match total being rendered as words rather than as null",
        ),
        (
            "  const ranking = (meta && meta.ranking) || null;\n"
            "  const status = ranking ? ranking.status : null;",
            "  const ranking = meta.ranking;\n  const status = ranking.status;",
            "emptyLedgerMessage surviving a payload with no `ranking` key",
        ),
    ],
)
def test_the_harness_can_actually_fail(
    tmp_path: Path, anchor: str, replacement: str, what: str
) -> None:
    """Guard the guard: break each behaviour and prove the harness goes red.

    Clause order matters and is copied from ``tests/test_ui_tree_nav.py``:
    the anchor must exist, the substitution must have applied, and only THEN is
    a red result evidence of anything. A mutation whose anchor silently missed
    produces a guard that certifies nothing while looking green.
    """
    source = LEDGER_STATUS.read_text(encoding="utf-8")
    assert anchor in source, f"the mutation anchor for {what} no longer exists"
    mutated = source.replace(anchor, replacement, 1)
    assert mutated != source, f"the {what} mutation did not apply"

    result = _run(tmp_path, mutated)
    assert result.returncode != 0, (
        f"breaking {what} left the harness GREEN — it is not testing that "
        "behaviour"
    )
