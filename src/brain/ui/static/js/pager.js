/* Which page the ledger can move to, and whether it can move at all.
 *
 * PURE, AND THAT IS WHY IT IS ITS OWN FILE — the same split, for the same
 * reason, as `ledger_status.js` out of `results.js` and `tree_nav.js` out of
 * `tree.js`. No DOM, no imports, no state: it maps a `/api/search` payload to
 * the state of two buttons and one line of text. `results.js` cannot be
 * executed outside a browser (it reaches for `document` in its first renderer),
 * so a decision left inside it can only ever be grepped;
 * `tests/test_ui_pager.py` runs this one under node.
 *
 * THE DEFECT THIS EXISTS FOR. `GET /api/search` has accepted an `offset` since
 * T6 and `js/` never sent one — grep the bundle and the only `offset` outside
 * this file is `outline-offset` in three stylesheets. So the ledger printed a
 * total ("544 notes"), rendered the first `limit` rows, and every row after
 * them was unreachable from the UI: paging existed on the wire and nowhere a
 * user could touch. #27 then added `ranking.status`, which explains WHY a page
 * ended — an explanation of a boundary on a page nobody could navigate to.
 *
 * THE CEILING IS NOT RE-DERIVED HERE, and that is the whole design constraint.
 * Both of `hybrid_search`'s legs bound their candidate pools at
 * `CANDIDATE_LIMIT` independently of the caller's `limit`, so at most
 * `2 * CANDIDATE_LIMIT` documents are ever ranked; `brain.ui.schemas` owns that
 * arithmetic (`MAX_RANKED_DOCUMENTS`, and `MAX_OFFSET` derived from it) and
 * reports its conclusion as `ranking.status`. A second implementation of that
 * rule in JavaScript is a second thing to keep true, which is exactly what
 * `ledger_status.js` refuses to do and what this file refuses with it.
 *
 * SO THERE IS NO BOUND IN THIS FILE AT ALL — not a literal, not a derived one.
 * "Is there a next page?" is `status === MORE` and nothing else. That is not a
 * shortcut, it is the stronger property: `more` means the ranker returned a
 * full over-fetch, `ranked >= offset + limit`, and the ranker cannot return
 * more than `MAX_RANKED_DOCUMENTS` rows — so `offset + limit` is already at or
 * below `MAX_OFFSET` whenever `more` is reported, and the control cannot ask
 * for an offset the server would reject with a 400. That implication is the
 * load-bearing one, so it is asserted where the constants live rather than
 * assumed here: `tests/test_ui_pager.py::test_a_more_status_can_never_advertise
 * _an_offset_the_server_would_reject`.
 *
 * The one-page overshoot is inherited deliberately. `schemas.ranking_status`
 * documents that a ranked set of exactly `fetch_limit` reports `more` and the
 * NEXT request reports the real ending; so Next can occasionally lead to one
 * empty page that then explains itself. Suppressing that here would mean
 * predicting the ranker's next answer — the re-derivation this file exists to
 * avoid — and `emptyLedgerMessage` already renders that page correctly.
 */

/* Mirrors `brain.ui.schemas.RANKING_*`. Duplicated across the language boundary
   — a real cost — rather than injected, because the alternative is a generated
   constants file for four strings; `ledger_status.js` records the same trade.
   `tests/test_ui_pager.py` asserts all four against the Python constants, so
   drift is a red test rather than a control that silently stops recognising an
   ending. */
export const MORE = "more";
export const EXHAUSTED = "exhausted";
export const CEILING = "ceiling";
export const UNKNOWN = "unknown";

/* WHY THIS PAGE IS THE LAST ONE, in the few words that fit beside a button.
 *
 * The distinction between the first two is the requirement, not a nicety: at
 * the ceiling the reader has NOT seen everything that matched, and a control
 * that said "End of results" there would be a lie told by the navigation while
 * the ledger note beneath it told the truth. `ledger_status.js` carries the
 * long form; this is the label on the boundary itself. */
const BOUNDARY = {
  [CEILING]: "End of what search can rank",
  [EXHAUSTED]: "End of results",
  [UNKNOWN]: "End of the ranked results",
};

/* Nothing to navigate. A FRESH object each call rather than one shared
   constant: callers read it, but a shared frozen-by-convention object is one
   careless `model.canNext = true` away from a bug that appears in an unrelated
   render. */
function noPager() {
  return {
    visible: false, canPrev: false, canNext: false,
    prevOffset: 0, nextOffset: 0, rangeLabel: "", boundary: "",
  };
}

/* Query-string integers arrive as whatever the server echoed. Anything that is
   not a non-negative integer is read as 0 — the pager must not compute an
   offset out of `NaN` and put it in a URL. */
function count(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return 0;
  return Math.trunc(number);
}

export function pagerModel(meta) {
  const ranking = (meta && meta.ranking) || null;
  /* No `ranking` key means a server older than #27, which also predates any
     way to know whether a next page exists. Showing a Next button that might
     lead nowhere is worse than showing none, so the ledger keeps exactly the
     shape it had before this file existed. */
  if (!ranking) return noPager();

  const offset = count(meta.offset);
  const limit = count(meta.limit);
  /* A limit of zero would make every page the same page: Next would advertise
     the offset it is already on and the control would look live while doing
     nothing. */
  if (limit <= 0) return noPager();

  const canPrev = offset > 0;
  const canNext = ranking.status === MORE;
  /* The ordinary short search: one page, nothing before it, nothing after it.
     A pager with both buttons dead is furniture that says only "this is all of
     it", which the ledger's own count already says. */
  if (!canPrev && !canNext) return noPager();

  const returned = count(meta.returned);
  return {
    visible: true,
    canPrev,
    canNext,
    /* Clamped at 0 rather than assumed to land there. The offsets this control
       produces are always multiples of `limit`, but a URL or a future caller
       need not be, and a negative offset is a 400. */
    prevOffset: Math.max(0, offset - limit),
    nextOffset: offset + limit,
    /* 1-based and inclusive, over the rows ACTUALLY returned rather than over
       `limit`: the last page is short, and "526–550" on a page showing twelve
       rows describes a page that does not exist. Empty when the page is empty,
       because "26–25" is not a range. */
    rangeLabel: returned > 0 ? `${offset + 1}–${offset + returned}` : "",
    /* Only when there is no next page — while Next is live, "why did it end"
       has no answer yet. An unrecognised status falls back to the UNKNOWN
       wording, never to EXHAUSTED: a status this file has not been taught is
       precisely the case where claiming the reader has seen everything is the
       one direction that misleads. */
    boundary: canNext ? "" : (BOUNDARY[ranking.status] || BOUNDARY[UNKNOWN]),
  };
}
