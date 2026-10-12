/* One fetch-and-cache rule for the per-note blocks after the note.
 *
 * marginalia.js (backlinks), graph.js and related.js each fetch one thing about
 * the open note and draw it after the note in `#inspector`. They used to carry
 * the same twenty lines each — a per-note cache, an in-flight set, a failure
 * sentinel, a stale-response guard, the cache key captured at request time —
 * and this is that logic written once. Each module calls `perNoteFetch` with
 * what genuinely differs between them: the endpoint, how the payload is
 * validated, what a failure caches, and how to redraw.
 *
 * THE RULES, which every caller inherits:
 *
 * - CACHED PER NOTE REVISION. Each render module runs on EVERY dispatch —
 *   toggling the editor, saving, moving — so without a cache each would
 *   re-request the same thing. The key is `noteKey(id)` (store.js), the note's
 *   id plus its revision, so a successful save (which bumps the revision)
 *   drops that note's entries and no other note's.
 *
 * - THE KEY IS CAPTURED WHEN THE REQUEST IS MADE, never recomputed when the
 *   answer lands. A response asked for before a save is filed under the
 *   superseded key and never matches again; recomputing the key in `.then`
 *   would file it under the CURRENT key and repaint the pre-save
 *   neighbourhood. Pinned by test_a_pre_save_response_landing_after_the_save_
 *   never_paints (tests/test_ui_browser_refresh.py).
 *
 * - A FAILURE IS CACHED, as the caller's `failed` sentinel. Retrying on every
 *   later dispatch turns one failing endpoint into a request storm against a
 *   server that is already failing; the cost is that a transient failure stays
 *   blank until the page is reloaded or the note is saved — the right trade for
 *   a block that is, by construction, supplementary to the note. The sentinel
 *   is the caller's because the callers differ: marginalia caches `[]`, the
 *   graph and the related rail cache `null`.
 *
 * - SILENT ON FAILURE. `api()` throws on a non-2xx and does NOT toast; this
 *   declines to as well. The note's own request already reported anything
 *   that stopped the NOTE from loading, and a second error for a block the
 *   reader never asked for is noise about a surface they may not be watching.
 *
 * - NOT AWAITED. The note is already painted; a block's request must never
 *   stand between the reader and the note. A lookup answers synchronously from
 *   the cache, or starts the request and answers "not yet".
 *
 * - STALE-RESPONSE GUARD. When an answer lands, the caller's `rerender` runs
 *   only if the note it was asked for is still the open note; a slow response
 *   for note A must not paint under note B. Redrawing (rather than appending to
 *   something captured at request time) is also what copes with the reader
 *   having moved on: anything captured then may be detached now.
 */

import { api } from "/static/js/api.js";
import { noteKey, state } from "/static/js/store.js";

/* Returns `lookup(note)`: the normalised payload for `note` at its current
 * revision, `failed` if that fetch failed, or `undefined` while it has not
 * arrived yet (in which case the request has been started, once).
 *
 *   endpoint  — the path segment after `/api/notes/{id}/`
 *   normalise — payload -> the value to cache; must not return `undefined`
 *   failed    — what a failed fetch caches
 *   rerender  — called when an answer lands for the still-open note
 */
export function perNoteFetch({ endpoint, normalise, failed, rerender }) {
  const cache = new Map();
  const inFlight = new Set();

  return function lookup(note) {
    /* Captured once, here — see "THE KEY IS CAPTURED" above. */
    const key = noteKey(note.id);
    if (cache.has(key)) return cache.get(key);
    if (inFlight.has(key)) return undefined;
    inFlight.add(key);

    api(`/api/notes/${encodeURIComponent(note.id)}/${endpoint}`).then(
      (payload) => {
        inFlight.delete(key);
        cache.set(key, normalise(payload));
        if (state.note && state.note.id === note.id) rerender();
      },
      () => {
        inFlight.delete(key);
        cache.set(key, failed);
      },
    );
    return undefined;
  };
}
