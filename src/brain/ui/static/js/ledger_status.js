/* What the ledger says about WHY a page of results ended (#27).
 *
 * PURE, AND THAT IS WHY IT IS ITS OWN FILE. No DOM, no imports, no state: it
 * maps a `/api/search` payload to the sentence the reader sees. `results.js`
 * cannot be executed outside a browser — it reaches for `document` on the first
 * line of its renderer — so a decision left inside it can only ever be grepped.
 * `tree_nav.js` was split out of `tree.js` for exactly this reason and
 * `tests/test_ui_tree_nav.py` runs it under node; `tests/test_ui_ledger_status.py`
 * does the same here.
 *
 * THE DEFECT THIS EXISTS FOR. Both of `hybrid_search`'s ranking legs bound their
 * candidate pools at `CANDIDATE_LIMIT` chunks, independently of the caller's
 * `limit`, so at most `2 * CANDIDATE_LIMIT` documents can ever be ranked no
 * matter how many match. The ledger used to print one hardcoded line —
 * "No notes matched. Try fewer filters." — for every empty list, which is
 * exactly wrong when the list is empty because the ranker stopped looking: it
 * tells a reader whose query matched 544 notes to go and broaden it.
 *
 * The server now says which ending happened, in `payload.ranking.status`. These
 * functions only choose words; they never re-derive the status, because a
 * second implementation of that rule in JavaScript is a second thing to keep
 * true.
 */

/* Mirrors `brain.ui.schemas.RANKING_*`. Duplicated across the language boundary
   — which is a real cost — rather than injected, because the alternative is a
   generated constants file for four strings. `tests/test_ui_ledger_status.py`
   asserts these against the Python constants, so drift is a red test rather
   than a ledger that silently stops recognising a status. */
export const CEILING = "ceiling";
export const UNKNOWN = "unknown";

/* The message for an EMPTY result list. Never returns "", because an empty
   ledger with no explanation is the defect. */
export function emptyLedgerMessage(meta) {
  const ranking = (meta && meta.ranking) || null;
  const status = ranking ? ranking.status : null;

  if (status === CEILING) {
    return (
      `${describeTotal(meta)} matched, but search ranks at most ` +
      `${ranking.max_ranked_documents} — this page is past that limit, not past ` +
      `the last match. Narrow the query to reach the rest.`
    );
  }
  if (status === UNKNOWN) {
    /* The ranker ran dry and the match count is unavailable, so the server
       itself cannot tell exhaustion from the ceiling. Saying "no notes matched"
       would resolve that ambiguity in the one direction that misleads. */
    return "No more ranked results. The match total is unavailable, so there may be more.";
  }
  /* `exhausted`, and also the no-`ranking`-key case: a server older than #27
     must keep the message it always had rather than a new one it cannot
     justify. */
  return "No notes matched. Try fewer filters.";
}

/* A trailing note for a NON-EMPTY page that is nonetheless the end of what the
   ranker will produce. Returns "" when there is nothing to say — the caller
   appends nothing rather than an empty row. */
export function ceilingNote(meta) {
  const ranking = (meta && meta.ranking) || null;
  if (!ranking || ranking.status !== CEILING) return "";
  return (
    `End of the ranked results: ${ranking.ranked_documents} of ` +
    `${describeTotal(meta)}. Search ranks at most ` +
    `${ranking.max_ranked_documents}; narrow the query to reach the rest.`
  );
}

/* `total_documents` is null when the count query failed, and `SearchDiagnostics`
   requires that a caller render it as unknown, NEVER as zero. "0 notes matched"
   under a ceiling would be self-contradicting on its face. */
function describeTotal(meta) {
  const total = meta ? meta.total_documents : null;
  return total == null ? "more notes" : `${total} notes`;
}
