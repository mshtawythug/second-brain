/* What the top bar says about the vault-sync watcher (A9's UI half).
 *
 * PURE, for the reason ledger_status.js gives: no DOM, no imports, no state, so
 * tests/test_ui_watcher_status.py can run it under node instead of grepping
 * it. js/watcher.js owns the element, the polling and the error handling; this
 * file only chooses words.
 *
 * Silent while the watcher runs. A line that says "watcher running" forever is
 * chrome the reader learns to ignore, which defeats it on the day it matters.
 * Spec: docs/specs/2026-10-15-ui-watcher-status-design.md §4.
 */

/* Mirrors `brain.doctor_watcher.WATCHER_STATES`. The test compares the two by
   value after running this module, so a rename on either side is a red test. */
export const RUNNING = "running";
export const NOT_RUNNING = "not_running";
export const UNAVAILABLE = "unavailable";
export const STATES = [RUNNING, NOT_RUNNING, UNAVAILABLE];

const LINES = {
  [RUNNING]: { text: "", short: "", tone: "" },
  [NOT_RUNNING]: {
    text: "Watcher not running — external edits will not appear in search",
    short: "Watcher off",
    tone: "warn",
  },
  /* Shown, but muted. Hiding it would claim a health the server could not
     check; a warning would cry wolf on a platform where it is permanent. */
  [UNAVAILABLE]: {
    text: "Watcher status unknown — external edits may not appear in search",
    short: "Watcher unknown",
    tone: "muted",
  },
};

/* `{state, text, short, tone}` for a `/api/watcher` payload. Anything the line
   cannot read is UNAVAILABLE, never the silent RUNNING: an unreadable answer is
   not evidence of health. `hasOwn` keeps a state such as "constructor" from
   resolving to a prototype member. */
export function watcherLine(payload) {
  const raw = payload ? payload.state : undefined;
  const state = Object.hasOwn(LINES, raw) ? raw : UNAVAILABLE;
  return { state, ...LINES[state] };
}
