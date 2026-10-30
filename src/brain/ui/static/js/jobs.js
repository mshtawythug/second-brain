/* The job client: submit, poll, cancel, and the ids this tab submitted.
 *
 * KIND-AGNOSTIC on purpose (Ingest spec §8): nothing here knows what an ingest
 * is. The Agent tab (RM-18b) reuses it for `ask` jobs, in its own scope.
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
 *
 * SCOPED, so two tabs' ids never mix (RM-18b). Every store function takes an
 * optional `scope`: the Ingest tab passes none and keeps the original keys; the
 * Agent tab passes "ask". Without it the Ingest tab, which resumes every id it
 * finds, drew a reloaded ask as an ingest row.
 *
 * SETTLED IDS are remembered too, separately. The server evicts a finished job
 * after an hour or past fifty, and a 404 for a job this tab already SAW finish is
 * that ordinary eviction, not a lost outcome: the caller drops it silently. Only
 * a 404 for a job never seen settled means "the server restarted under it".
 */

import { api } from "/static/js/api.js";

export const POLL_FAST_MS = 1000;
export const POLL_SLOW_MS = 3000;
export const SLOW_AFTER_MS = 30000;
/* The client renders the 20 most recent jobs (Ingest spec R7). */
export const MAX_REMEMBERED = 20;

const STORE_KEY = "brain-ui-jobs";
const SETTLED_KEY = "brain-ui-jobs-settled";

/* The sessionStorage key for `base` in `scope`; no scope is the original key. */
function scoped(base, scope) {
  return scope ? `${base}:${scope}` : base;
}
const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

export function isTerminal(state) {
  return TERMINAL.has(state);
}

/* How long to wait before the next poll: pure, so a test can execute it. */
export function pollDelay(elapsedMs, hidden) {
  return hidden || elapsedMs > SLOW_AFTER_MS ? POLL_SLOW_MS : POLL_FAST_MS;
}

function readIds(key) {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(key) || "[]");
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === "string") : [];
  } catch (e) {
    return [];
  }
}

function storeIds(key, ids) {
  try { sessionStorage.setItem(key, JSON.stringify(ids)); } catch (e) { /* ignore */ }
}

export function rememberedJobs(scope) {
  return readIds(scoped(STORE_KEY, scope));
}

function remember(id, scope) {
  const ids = [id, ...rememberedJobs(scope).filter((other) => other !== id)];
  storeIds(scoped(STORE_KEY, scope), ids.slice(0, MAX_REMEMBERED));
}

export function forget(id, scope) {
  storeIds(scoped(STORE_KEY, scope), rememberedJobs(scope).filter((other) => other !== id));
  const settled = scoped(SETTLED_KEY, scope);
  storeIds(settled, readIds(settled).filter((other) => other !== id));
}

/* Record that this tab saw job `id` reach a terminal state. */
export function markSettled(id, scope) {
  const key = scoped(SETTLED_KEY, scope);
  const ids = readIds(key);
  if (!ids.includes(id)) storeIds(key, [id, ...ids].slice(0, MAX_REMEMBERED));
}

export function wasSettled(id, scope) {
  return readIds(scoped(SETTLED_KEY, scope)).includes(id);
}

/* POST a job and remember its id in `scope`. Resolves to the server's snapshot. */
export async function submitJob(path, body, { scope } = {}) {
  const snapshot = await api(path, { method: "POST", body });
  remember(snapshot.id, scope);
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
