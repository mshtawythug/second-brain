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
 * A FAILED CHECK CHANGES NOTHING once there is an answer on screen. "The
 * server is unreachable" is boot's toast and a different problem; it is not
 * evidence about the watcher either way. Before the first answer it says
 * "unknown", because an empty line would claim the watcher is running.
 *
 * Spec: docs/specs/2026-10-15-ui-watcher-status-design.md §3.1, §4.
 */

import { api } from "/static/js/api.js";
import { $ } from "/static/js/dom.js";
import { watcherLine } from "/static/js/watcher_status.js";

const POLL_MS = 60_000;

/* Overlapping checks are SEQUENCED. Focus and visibilitychange often fire
   together, and across the server's 15 s cache boundary two overlapping
   requests can get different answers, in either arrival order. Each check
   takes a ticket; an answer older than the last one painted is dropped. */
let issued = 0;
let painted = 0;

/* Writes ONLY on a change of state. Rewriting identical text into a live
   region can re-announce it, so a watcher that stays down would be read out
   again every minute. The state painted is kept on the element. */
function render(line) {
  const region = $("watcher-status");
  if (region.dataset.state === line.state) return;
  region.dataset.state = line.state;
  region.dataset.tone = line.tone;
  region.querySelector(".watcher-full").textContent = line.text;
  region.querySelector(".watcher-short").textContent = line.short;
}

async function check() {
  const ticket = ++issued;
  let line;
  try {
    line = watcherLine(await api("/api/watcher"));
  } catch (e) {
    /* A failure keeps the last answer. But with NO answer yet, keeping it
       means staying silent, and silence is exactly what RUNNING looks like,
       so it shows "unknown" instead — as a PLACEHOLDER, not an answer. It
       does not advance `painted`, so an older check that is still in flight
       (boot's, say) still paints when it lands instead of being dropped as
       stale behind a failure. */
    if (painted === 0) render(watcherLine(null));
    return;
  }
  if (ticket < painted) return;
  painted = ticket;
  render(line);
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
