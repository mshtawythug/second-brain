/* The Ingest tab: the form, its Paste / File switch, and the job panel (RM-11, RM-13;
 * Ingest spec §8).
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
 * The job row is drawn by ingest_view.js; polling and storage are jobs.js; file
 * mode's staging and drop zone are ingest_file.js; inbox mode's list is
 * ingest_inbox.js (RM-40). Tags, sensitivity and the summary switch are shared by
 * every mode. Every server string reaches the page as text.
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
import {
  acceptsFiles, describeFileLimits, fileProblem, submitStagedFile, wireFileMode,
} from "/static/js/ingest_file.js";
import {
  inboxProblem, showInbox, submitInboxFile, wireInboxMode,
} from "/static/js/ingest_inbox.js";

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
  describeFileLimits(cfg);
  /* File mode is offered only when this server accepts at least one type. */
  $("ingest-mode-file").disabled = !acceptsFiles(cfg);
}

function mode() {
  if ($("ingest-mode-file").checked) return "file";
  return $("ingest-mode-inbox").checked ? "inbox" : "paste";
}

function onModeChange() {
  const current = mode();
  $("ingest-paste-fields").hidden = current !== "paste";
  $("ingest-file-fields").hidden = current !== "file";
  $("ingest-inbox-fields").hidden = current !== "inbox";
  showError("");
  if (current === "inbox") showInbox();
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
    /* Only a real snapshot restores the row; an "unreachable" note does not. */
    quiet: before.quiet && !("state" in fields),
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

/* The fields both modes send. */
function readCommon() {
  const checked = $("ingest-sensitivity").querySelector("input:checked");
  return {
    tags: $("ingest-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
    sensitivity: checked ? checked.value : config.default_sensitivity,
    enrich: !$("ingest-no-enrich").checked,
  };
}

function readForm() {
  return {
    title: $("ingest-title").value.trim(),
    body: $("ingest-body").value,
    content_type: $("ingest-content-type").value,
    ...readCommon(),
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

async function submitPaste() {
  const paste = readForm();
  const message = problem(paste);
  showError(message);
  if (message) return null;
  const snapshot = await submitJob("/api/ingest/paste", paste);
  $("ingest-title").value = "";
  $("ingest-body").value = "";
  return snapshot;
}

async function submitFile() {
  const message = fileProblem();
  showError(message);
  if (message) return null;
  return submitStagedFile(readCommon());
}

async function submitInbox() {
  const message = inboxProblem();
  showError(message);
  if (message) return null;
  return submitInboxFile(readCommon());
}

const SUBMIT = { paste: submitPaste, file: submitFile, inbox: submitInbox };

async function onSubmit(event) {
  event.preventDefault();
  if (!config || !config.enabled) return;
  const submit = $("ingest-submit");
  submit.disabled = true;
  try {
    const snapshot = await SUBMIT[mode()]();
    if (!snapshot) return;
    track(newRecord(snapshot), { immediate: false });
    announce(announcement(jobs.get(snapshot.id).record));
    $("ingest-tags").value = "";
  } catch (error) {
    showError(error.message);
  } finally {
    submit.disabled = false;
  }
}

/* aria-disabled, NOT `disabled`, while the request is in flight: a disabled
   button drops keyboard focus to <body>, and focus must stay on the button so
   that, once the button goes away, ingest_view.js can hand it to the row. */
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
  $("ingest-mode").addEventListener("change", onModeChange);
  wireFileMode({ config: () => config, showError, announce });
  wireInboxMode({ showError, announce });
}
