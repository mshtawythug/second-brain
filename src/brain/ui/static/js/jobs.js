/* The job client: submit, poll, cancel, and the ids this tab submitted.
 *
 * KIND-AGNOSTIC on purpose (Ingest spec §8): nothing here knows what an ingest
 * is. The Agent tab (RM-18b) reuses it for `ask` jobs as it is.
 *
 * POLLING, NOT A STREAM (D2, §4.3). Each poll is `GET /api/jobs/{id}?after=<seq>`,
 * an in-memory read on the server; the client keeps the last seq it saw, so the
 * server keeps no per-client state. Once a second while a job runs; every three
 * seconds once it is older than thirty, and whenever the page is hidden. It
 * stops at a terminal state.
 *
 * REMEMBERED IDS live in sessionStorage, so a reload (or a server restart behind
 * it) can still say what became of a job. Read and written inside `try`: a
 * private window or blocked storage just means the panel starts empty.
 */

import { api } from "/static/js/api.js";

export const POLL_FAST_MS = 1000;
export const POLL_SLOW_MS = 3000;
export const SLOW_AFTER_MS = 30000;
/* The client renders the 20 most recent jobs (Ingest spec R7). */
export const MAX_REMEMBERED = 20;

const STORE_KEY = "brain-ui-jobs";
const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

export function isTerminal(state) {
  return TERMINAL.has(state);
}

/* How long to wait before the next poll: pure, so a test can execute it. */
export function pollDelay(elapsedMs, hidden) {
  return hidden || elapsedMs > SLOW_AFTER_MS ? POLL_SLOW_MS : POLL_FAST_MS;
}

export function rememberedJobs() {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(STORE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === "string") : [];
  } catch (e) {
    return [];
  }
}

function storeIds(ids) {
  try { sessionStorage.setItem(STORE_KEY, JSON.stringify(ids)); } catch (e) { /* ignore */ }
}

export function remember(id) {
  const ids = [id, ...rememberedJobs().filter((other) => other !== id)];
  storeIds(ids.slice(0, MAX_REMEMBERED));
}

export function forget(id) {
  storeIds(rememberedJobs().filter((other) => other !== id));
}

/* POST a job and remember its id. Resolves to the server's snapshot. */
export async function submitJob(path, body) {
  const snapshot = await api(path, { method: "POST", body });
  remember(snapshot.id);
  return snapshot;
}

/* POST a cancel. `{}` because the server refuses a mutation without a JSON body. */
export function cancelJob(id) {
  return api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} });
}

/* Poll one job until it is terminal or gone. Returns a function that stops it.
 *
 * `onUpdate(snapshot)` gets every answer; its `events` hold only the ones newer
 * than the last seq seen. `onGone(error)` is a 404: the server no longer has the
 * job (evicted, or it restarted), and polling stops. `onUnreachable(error)` is
 * any other failure; polling carries on at the slow rate. */
export function pollJob(id, { onUpdate, onGone, onUnreachable, immediate = false }) {
  const started = Date.now();
  let after = 0;
  let timer = null;
  let stopped = false;

  const schedule = (delay) => {
    if (!stopped) timer = setTimeout(tick, delay);
  };

  async function tick() {
    let snapshot;
    try {
      snapshot = await api(`/api/jobs/${encodeURIComponent(id)}?after=${after}`);
    } catch (error) {
      if (stopped) return;
      if (error.status === 404) {
        stopped = true;
        onGone(error);
        return;
      }
      onUnreachable(error);
      schedule(POLL_SLOW_MS);
      return;
    }
    if (stopped) return;
    for (const event of snapshot.events || []) after = Math.max(after, event.seq);
    onUpdate(snapshot);
    if (isTerminal(snapshot.state)) {
      stopped = true;
      return;
    }
    schedule(pollDelay(Date.now() - started, document.visibilityState === "hidden"));
  }

  schedule(immediate ? 0 : pollDelay(0, document.visibilityState === "hidden"));
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}
