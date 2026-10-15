/* The watcher status line's wiring: when to ask `/api/watcher`, and where the
 * answer goes. The words are js/watcher_status.js's; this file never chooses
 * any.
 *
 * WHEN. Once at boot, every POLL_MS while the tab is visible, and again when
 * the reader comes back (the tab turns visible, or the window regains focus) —
 * "I started the watcher in a terminal, now I'm back" is the case that
 * matters. A hidden tab never polls. The server caches its `ps` snapshot for
 * 15 s, so a burst of focus events costs at most one fork there.
 *
 * WHERE. #watcher-status is a polite live region that is in the markup from
 * first paint and is never `hidden`: a region inserted or un-hidden in the
 * same moment its text changes is announced unreliably. It holds two spans —
 * the full sentence, and a short form shown only on a narrow top bar (CSS
 * decides; the short one is aria-hidden, so a screen reader hears the full
 * sentence at every width).
 *
 * A FAILED CHECK CHANGES NOTHING. "The server is unreachable" is boot's toast
 * and a different problem; it is not evidence about the watcher either way.
 *
 * Spec: docs/specs/2026-10-15-ui-watcher-status-design.md §3.1, §4.
 */

import { api } from "/static/js/api.js";
import { $ } from "/static/js/dom.js";
import { watcherLine } from "/static/js/watcher_status.js";

const POLL_MS = 60_000;

function render(line) {
  const region = $("watcher-status");
  region.dataset.tone = line.tone;
  region.querySelector(".watcher-full").textContent = line.text;
  region.querySelector(".watcher-short").textContent = line.short;
}

/* Focus and visibilitychange often fire together, so two checks can overlap.
   They are not sequenced: both reach the server's 15 s snapshot cache and get
   the same answer, so their arrival order cannot change what is painted. */
async function check() {
  let payload;
  try {
    payload = await api("/api/watcher");
  } catch (e) {
    return;
  }
  render(watcherLine(payload));
}

function checkIfVisible() {
  if (!document.hidden) check();
}

export function wireWatcherStatus() {
  check();
  setInterval(checkIfVisible, POLL_MS);
  document.addEventListener("visibilitychange", checkIfVisible);
  window.addEventListener("focus", checkIfVisible);
}
