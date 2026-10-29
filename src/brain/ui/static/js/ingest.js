/* The Ingest tab, paste mode (RM-11, Ingest spec §8).
 *
 * LAZY. Nothing here runs at boot except `wireIngest()` attaching listeners. The
 * first time the tab is shown it loads `GET /api/ingest/config` — the tab
 * hard-codes no limit and no list — and resumes the jobs this browser tab
 * submitted before a reload. The form's fieldset ships DISABLED in index.html
 * and only a config that says ingest is allowed enables it, so a read-only or
 * LAN-bound server shows its reason instead of a form that can only fail.
 *
 * ORDER-FREE in boot(): it owns #tab-ingest, which no renderer touches, and
 * subscribes to nothing.
 *
 * The job row is drawn by ingest_view.js; polling and storage are jobs.js.
 * Every server string reaches the page as text.
 */

import { $, el, toast } from "/static/js/dom.js";
import { api } from "/static/js/api.js";
import { openNote } from "/static/js/inspector.js";
import {
  MAX_REMEMBERED, cancelJob, forget, markSettled, pollJob, rememberedJobs, submitJob,
  wasSettled,
} from "/static/js/jobs.js";
import {
  announcement, createJobRow, isSettled, updateJobRow,
} from "/static/js/ingest_view.js";

const UNAVAILABLE = {
  read_only: "This server is read-only.",
  requires_loopback: "Ingest is available only when brain ui is bound to this machine. "
    + "From a terminal, use brain ingest.",
  jobs_unavailable: "The job runner is not available on this server.",
};

/* id -> { record, row, stop } for every job the panel shows. */
const jobs = new Map();
let config = null;
let loading = null;

function bytes(text) {
  return new TextEncoder().encode(text).length;
}

function formatBytes(n) {
  return n >= 1024 * 1024 ? `${(n / (1024 * 1024)).toFixed(1)} MiB` : `${n} bytes`;
}

/* Clear, then set on the next frame: a screen reader may not re-read a live
   region whose new text equals its old text ("Paste job: Added" twice). */
function announce(text) {
  if (!text) return;
  const region = $("ingest-announce");
  region.textContent = "";
  requestAnimationFrame(() => { region.textContent = text; });
}

function showError(message) {
  const node = $("ingest-error");
  node.textContent = message || "";
  node.hidden = !message;
}

function syncEmpty() {
  $("ingest-jobs-empty").hidden = jobs.size > 0;
}

function fillControls(cfg) {
  const select = $("ingest-content-type");
  select.replaceChildren(...cfg.content_types.map((type) => new Option(type, type)));
  select.value = cfg.default_content_type;
  const group = $("ingest-sensitivity");
  for (const old of group.querySelectorAll(".ingest-radio")) old.remove();
  for (const level of cfg.sensitivity_levels.slice().sort(
    (a, b) => (a === cfg.default_sensitivity ? -1 : b === cfg.default_sensitivity ? 1 : 0),
  )) {
    const wrap = el("div", "ingest-radio");
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "ingest-sensitivity";
    input.id = `ingest-sensitivity-${level}`;
    input.value = level;
    input.checked = level === cfg.default_sensitivity;
    const label = el("label", "", level);
    label.htmlFor = input.id;
    wrap.append(input, label);
    group.append(wrap);
  }
  $("ingest-body-hint").textContent =
    `Up to ${formatBytes(cfg.limits.paste_body_bytes)}. Credential-like strings are `
    + "checked by the secret guard before anything is saved.";
}

async function loadConfig() {
  const notice = $("ingest-unavailable");
  try {
    config = await api("/api/ingest/config");
  } catch (error) {
    notice.textContent = "Could not load the ingest settings. Reload the page to retry.";
    notice.hidden = false;
    return;
  }
  fillControls(config);
  if (!config.enabled) {
    notice.textContent = UNAVAILABLE[config.reason] || "Ingest is not available on this server.";
    notice.hidden = false;
    return;
  }
  notice.hidden = true;
  $("ingest-fieldset").disabled = false;
}

function track(record, { immediate }) {
  const row = createJobRow(record);
  $("ingest-jobs").prepend(row);
  const entry = { record, row, stop: () => {} };
  jobs.set(record.id, entry);
  while (jobs.size > MAX_REMEMBERED) {
    const [oldest] = jobs.keys();
    drop(oldest);
  }
  syncEmpty();
  if (!isSettled(record)) {
    entry.stop = pollJob(record.id, {
      immediate,
      onUpdate: (snapshot) => apply(record.id, fromSnapshot(snapshot)),
      onGone: () => {
        /* Seen finished before: an ordinary eviction (1 h, or past 50 jobs), so
           there is nothing to report. Never seen finished: the outcome is lost. */
        const evicted = wasSettled(record.id);
        forget(record.id);
        if (evicted) {
          drop(record.id);
          syncEmpty();
        } else {
          apply(record.id, { gone: true });
        }
      },
      onUnreachable: () => apply(record.id, { unreachable: true }),
    });
  }
}

function drop(id) {
  const entry = jobs.get(id);
  entry.stop();
  entry.row.remove();
  jobs.delete(id);
}

/* The record fields a snapshot carries. Events are merged by seq, not replaced:
   a poll returns only the ones newer than the last seen. */
function fromSnapshot(snapshot) {
  return {
    kind: snapshot.kind,
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

function apply(id, change) {
  const entry = jobs.get(id);
  if (!entry) return;
  const before = entry.record;
  /* A SETTLED ROW IS FINAL. A poll or a cancel answer that was already in flight
     when the row settled is older than what the row shows; letting it through
     would put a finished job back to "running" and offer Cancel again. */
  if (isSettled(before)) return;
  const { newEvents = [], ...fields } = change;
  const seen = new Set(before.events.map((e) => e.seq));
  const record = {
    ...before,
    ...fields,
    /* Sticky for the same reason: a poll sent before the cancel still says false. */
    cancelRequested: before.cancelRequested || Boolean(fields.cancelRequested),
    quiet: false,
    events: [...before.events, ...newEvents.filter((e) => !seen.has(e.seq))],
  };
  entry.record = record;
  updateJobRow(entry.row, record);
  if (isSettled(record)) {
    entry.stop();
    if (!record.gone) markSettled(id);
  }
  /* A job resumed after a reload is not news: its first answer only restores
     the row, so it is drawn and not announced. */
  const moved = before.state !== record.state || before.gone !== record.gone;
  if (moved && !before.quiet) announce(announcement(record));
}

function newRecord(snapshot) {
  return {
    id: snapshot.id,
    kind: snapshot.kind,
    createdAt: snapshot.created_at,
    state: snapshot.state,
    cancellable: snapshot.cancellable,
    cancelRequested: snapshot.cancel_requested,
    events: snapshot.events || [],
    result: snapshot.result,
    error: snapshot.error,
    gone: false,
    unreachable: false,
    cancelNote: "",
  };
}

function pendingRecord(id) {
  return { ...newRecord({ id, kind: "ingest.paste", state: "queued", events: [] }),
           cancellable: false, quiet: true };
}

function resumeRemembered() {
  for (const id of rememberedJobs().reverse()) {
    if (!jobs.has(id)) track(pendingRecord(id), { immediate: true });
  }
}

function readForm() {
  const checked = $("ingest-sensitivity").querySelector("input:checked");
  return {
    title: $("ingest-title").value.trim(),
    body: $("ingest-body").value,
    tags: $("ingest-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
    content_type: $("ingest-content-type").value,
    sensitivity: checked ? checked.value : config.default_sensitivity,
    enrich: !$("ingest-no-enrich").checked,
  };
}

/* The checks the server repeats; here they save a round trip and keep the form. */
function problem(paste) {
  if (!paste.title) return "Give the paste a title.";
  if (!paste.body.trim()) return "Paste some text to ingest.";
  const size = bytes(paste.body);
  const limit = config.limits.paste_body_bytes;
  if (size > limit) return `The text is ${formatBytes(size)}; the limit is ${formatBytes(limit)}.`;
  return "";
}

async function onSubmit(event) {
  event.preventDefault();
  if (!config || !config.enabled) return;
  const paste = readForm();
  const message = problem(paste);
  showError(message);
  if (message) return;
  const submit = $("ingest-submit");
  submit.disabled = true;
  try {
    const snapshot = await submitJob("/api/ingest/paste", paste);
    track(newRecord(snapshot), { immediate: false });
    announce(announcement(jobs.get(snapshot.id).record));
    $("ingest-title").value = "";
    $("ingest-body").value = "";
    $("ingest-tags").value = "";
  } catch (error) {
    showError(error.message);
  } finally {
    submit.disabled = false;
  }
}

async function onCancel(row, button) {
  const id = row.dataset.jobId;
  button.disabled = true;
  try {
    apply(id, fromSnapshot(await cancelJob(id)));
  } catch (error) {
    if (error.status === 409) {
      apply(id, { cancellable: false, cancelNote: error.message });
    } else {
      button.disabled = false;
      toast(error.message, "error");
    }
  }
}

function onJobsClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const row = target.closest(".job");
  if (target.dataset.action === "cancel") {
    onCancel(row, target);
    return;
  }
  /* "Open in Notes" is a real link, so a modified click opens a new tab. */
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  document.querySelector('.tab[data-tab="notes"]').click();
  openNote(target.dataset.id);
}

function onTabShown() {
  if (!loading) loading = loadConfig().then(resumeRemembered);
}

export function wireIngest() {
  document.querySelector('.tab[data-tab="ingest"]').addEventListener("click", onTabShown);
  $("ingest-form").addEventListener("submit", onSubmit);
  $("ingest-jobs").addEventListener("click", onJobsClick);
}
