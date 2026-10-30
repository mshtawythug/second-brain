/* The Agent tab: the question form and its turns (RM-18b; roadmap spec RM-18,
 * decisions A1–A10).
 *
 * LAZY, like ingest.js. Nothing runs at boot except `wireAsk()` attaching
 * listeners. The first time the tab is shown it loads `GET /api/ask/config` and
 * resumes the asks this browser tab submitted before a reload. The fieldset
 * ships DISABLED in index.html and only a config that says asking is allowed
 * enables it, so a read-only or LAN-bound server shows its reason (A6).
 *
 * ORDER-FREE in boot(): it owns #tab-agent, which no renderer touches, and
 * subscribes to nothing.
 *
 * Polling, cancel and the remembered ids are jobs.js, in the "ask" scope so an
 * ask never turns up in the Ingest tab (plan doc D2). A turn is drawn by
 * ask_view.js, which holds the hardening rules: every server string, the model's
 * answer above all, reaches the page as text. `sessionStorage` holds ask ids
 * only, never a question or an answer (plan doc D3).
 *
 * WAITING FOR AN INGEST (A1, plan doc D4). One worker runs ingest and ask jobs,
 * from a FIFO queue. A snapshot carries no queue position, so while an ask is
 * queued the tab reads `GET /api/jobs`, which lists ingest jobs and never asks
 * (A10), and says "waiting for the ingest job" only when one is AHEAD: running,
 * or queued with an earlier `created_at` than the ask's own. An ingest queued
 * after the ask runs after it.
 */

import { $, toast } from "/static/js/dom.js";
import { api } from "/static/js/api.js";
import { openNote } from "/static/js/inspector.js";
import {
  MAX_REMEMBERED, cancelJob, forget, markSettled, pollJob, rememberedJobs, submitJob,
  wasSettled,
} from "/static/js/jobs.js";
import {
  UUID_RE, announcement, createTurn, isSettled, updateTurn,
} from "/static/js/ask_view.js";

const SCOPE = "ask";

const UNAVAILABLE = {
  read_only: "This server is read-only, so it cannot run the agent.",
  requires_loopback: "The agent is available only when brain ui is bound to this machine. "
    + "From a terminal, use brain ask.",
  jobs_unavailable: "The job runner is not available on this server.",
};

/* id -> { record, row, stop } for every turn the tab shows. */
const turns = new Map();
let config = null;
let loading = null;

function announce(text) {
  if (!text) return;
  const region = $("agent-announce");
  region.textContent = "";
  requestAnimationFrame(() => { region.textContent = text; });
}

function showError(message) {
  const node = $("agent-error");
  node.textContent = message || "";
  node.hidden = !message;
}

function syncEmpty() {
  $("agent-empty").hidden = turns.size > 0;
}

/* One ask at a time from this tab (plan doc D5): the server has one worker. */
function syncSubmit() {
  const busy = [...turns.values()].some((entry) => !isSettled(entry.record));
  $("agent-submit").disabled = busy;
  $("agent-busy").hidden = !busy;
}

async function loadConfig() {
  const notice = $("agent-unavailable");
  try {
    config = await api("/api/ask/config");
  } catch (error) {
    notice.textContent = "Could not load the agent settings. Reload the page to retry.";
    notice.hidden = false;
    return;
  }
  $("agent-question-hint").textContent =
    `Up to ${config.max_question_chars} characters. Answers are plain text; `
    + "a number in brackets opens the note it cites.";
  if (!config.enabled) {
    notice.textContent = UNAVAILABLE[config.reason] || "The agent is not available on this server.";
    notice.hidden = false;
    return;
  }
  notice.hidden = true;
  $("agent-fieldset").disabled = false;
}

function fromSnapshot(snapshot) {
  return {
    createdAt: snapshot.created_at,
    state: snapshot.state,
    cancellable: snapshot.cancellable,
    cancelRequested: snapshot.cancel_requested,
    result: snapshot.result,
    error: snapshot.error,
    newEvents: snapshot.events || [],
    unreachable: false,
  };
}

function newRecord(snapshot, question) {
  return {
    id: snapshot.id,
    question,
    createdAt: snapshot.created_at,
    state: snapshot.state,
    cancellable: snapshot.cancellable,
    cancelRequested: snapshot.cancel_requested,
    events: snapshot.events || [],
    result: snapshot.result,
    error: snapshot.error,
    gone: false,
    unreachable: false,
    waitingOnIngest: false,
    cancelNote: "",
  };
}

function apply(id, change) {
  const entry = turns.get(id);
  if (!entry) return;
  const before = entry.record;
  /* A SETTLED TURN IS FINAL, for the reason ingest.js gives: an answer already
     in flight when it settled is older than what it shows. */
  if (isSettled(before)) return;
  const { newEvents = [], ...fields } = change;
  const seen = new Set(before.events.map((e) => e.seq));
  const record = {
    ...before,
    ...fields,
    cancelRequested: before.cancelRequested || Boolean(fields.cancelRequested),
    quiet: before.quiet && !("state" in fields),
    events: [...before.events, ...newEvents.filter((e) => !seen.has(e.seq))],
  };
  entry.record = record;
  updateTurn(entry.row, record);
  if (isSettled(record)) {
    entry.stop();
    if (!record.gone) markSettled(id, SCOPE);
  }
  syncSubmit();
  const moved = before.state !== record.state || before.gone !== record.gone;
  if (moved && !before.quiet) announce(announcement(record));
  if (record.state === "queued" && "state" in fields) checkIngestAhead(id);
}

/* True when `stamp` is strictly before `than`. Unparseable is never "before": a
   turn only claims to wait for an ingest it can show is ahead. */
function earlier(stamp, than) {
  const a = Date.parse(stamp);
  const b = Date.parse(than);
  return Number.isFinite(a) && Number.isFinite(b) && a < b;
}

/* D4: is an ingest job ahead of this queued ask? The queue is FIFO
   (`JobRegistry.run_next` pops its left end), so a queued job counts only when it
   was submitted first. Best effort: on any failure the turn keeps the plain
   "queued" line. */
async function checkIngestAhead(id) {
  let listing;
  try {
    listing = await api("/api/jobs");
  } catch (error) {
    return;
  }
  const asked = turns.has(id) ? turns.get(id).record.createdAt : undefined;
  const ahead = (listing.jobs || []).some((job) => job.state === "running"
    || (job.state === "queued" && earlier(job.created_at, asked)));
  const entry = turns.get(id);
  if (entry && entry.record.state === "queued" && entry.record.waitingOnIngest !== ahead) {
    apply(id, { waitingOnIngest: ahead });
  }
}

function track(record, { immediate }) {
  const row = createTurn(record);
  $("agent-turns").prepend(row);
  const entry = { record, row, stop: () => {} };
  turns.set(record.id, entry);
  while (turns.size > MAX_REMEMBERED) {
    const [oldest] = turns.keys();
    drop(oldest);
  }
  syncEmpty();
  syncSubmit();
  if (isSettled(record)) return;
  entry.stop = pollJob(record.id, {
    immediate,
    onUpdate: (snapshot) => apply(record.id, fromSnapshot(snapshot)),
    onGone: () => {
      /* Seen settled before: the ordinary 10-minute eviction (A10), so the turn
         simply goes. Never seen settled: the answer is lost, and the turn says so. */
      const evicted = wasSettled(record.id, SCOPE);
      forget(record.id, SCOPE);
      if (evicted) {
        drop(record.id);
        syncEmpty();
        syncSubmit();
      } else {
        apply(record.id, { gone: true });
      }
    },
    onUnreachable: () => apply(record.id, { unreachable: true }),
  });
  /* A resumed placeholder (`quiet`) is "queued" only until its first poll says
     what it really is; that poll's apply() checks, if it is still queued. */
  if (record.state === "queued" && !record.quiet) checkIngestAhead(record.id);
}

function drop(id) {
  const entry = turns.get(id);
  entry.stop();
  entry.row.remove();
  turns.delete(id);
}

function resumeRemembered() {
  for (const id of rememberedJobs(SCOPE).reverse()) {
    if (turns.has(id)) continue;
    const record = { ...newRecord({ id, state: "queued", events: [] }, ""),
                     cancellable: false, quiet: true };
    track(record, { immediate: true });
  }
}

/* The check the server repeats, in the same unit: Python's len() counts code
   points, and so does spreading a string. */
function problem(question) {
  if (!question) return "Type a question to ask.";
  const length = [...question].length;
  if (length > config.max_question_chars) {
    return `The question is ${length} characters; the limit is ${config.max_question_chars}.`;
  }
  return "";
}

async function onSubmit(event) {
  event.preventDefault();
  if (!config || !config.enabled) return;
  const question = $("agent-question").value.trim();
  const message = problem(question);
  showError(message);
  if (message) return;
  const submit = $("agent-submit");
  submit.disabled = true;
  try {
    const snapshot = await submitJob(
      "/api/ask", { question, no_loop: $("agent-no-loop").checked }, { scope: SCOPE },
    );
    $("agent-question").value = "";
    track(newRecord(snapshot, question), { immediate: false });
    announce(announcement(turns.get(snapshot.id).record));
  } catch (error) {
    showError(error.message);
  } finally {
    syncSubmit();
  }
}

/* aria-disabled while in flight, for the focus reason ingest.js gives. */
async function onCancel(row, button) {
  const id = row.dataset.jobId;
  if (button.getAttribute("aria-disabled") === "true") return;
  button.setAttribute("aria-disabled", "true");
  try {
    apply(id, fromSnapshot(await cancelJob(id)));
  } catch (error) {
    if (error.status === 409) {
      apply(id, { cancellable: false, cancelNote: error.message });
    } else {
      button.removeAttribute("aria-disabled");
      toast(error.message, "error");
    }
  }
}

/* The cited note's id, read back from the link's own UUID-checked href. */
function linkedId(link) {
  const id = new URL(link.href, location.href).searchParams.get("id");
  return id && UUID_RE.test(id) ? id : null;
}

function onTurnsClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  if (target.dataset.action === "cancel") {
    onCancel(target.closest(".turn"), target);
    return;
  }
  /* A citation is a real link, so a modified click opens a new tab. */
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const id = linkedId(target);
  if (!id) return;
  event.preventDefault();
  document.querySelector('.tab[data-tab="notes"]').click();
  openNote(id);
}

function onTabShown() {
  if (!loading) loading = loadConfig().then(resumeRemembered);
}

export function wireAsk() {
  document.querySelector('.tab[data-tab="agent"]').addEventListener("click", onTabShown);
  $("agent-form").addEventListener("submit", onSubmit);
  $("agent-turns").addEventListener("click", onTurnsClick);
}
