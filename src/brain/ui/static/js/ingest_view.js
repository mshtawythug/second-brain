/* One job row in the Ingest tab's panel: built once, then updated in place.
 *
 * IN PLACE, NOT REBUILT, because a poll lands every second: rebuilding the row
 * would destroy the Cancel button under a keyboard user's focus. The row and
 * its head, phase line and Cancel button are stable; only `.job-detail` is
 * redrawn, and it holds nothing focusable but the "Open in Notes" link of a
 * finished job, which no later poll touches.
 *
 * EVERY SERVER STRING IS TEXT (Ingest spec §7.5): titles, paths, finding
 * labels and error messages all go through `el(..., text)`, which sets
 * `textContent`. Nothing ingested is rendered as markup here; opening the
 * document goes through the Notes tab's server-side renderer.
 *
 * A WITHHELD RESULT IS SHOWN AS WITHHELD. While `serve_confidential_titles` is
 * off, the server nulls `title`, `vault_path` and `document_id` and sets
 * `title_withheld` (RM-10's lens). The row then says so, with no link, and never
 * puts back a title from the form: a dedup skip can land on a different,
 * existing confidential document, so the typed title could also misname it.
 * Rows are labelled by kind and submit time for the same reason.
 */

import { el } from "/static/js/dom.js";
import { isTerminal } from "/static/js/jobs.js";

const KIND_LABELS = { "ingest.paste": "Paste", "ingest.file": "File", "ingest.inbox": "Inbox" };

const OUTCOMES = {
  ingested: "Added",
  updated: "Updated",
  skipped: "Already in your brain",
  mirror_repaired: "Already in your brain. Its vault file was missing and has been rewritten.",
  empty: "Nothing to index: no text could be extracted.",
};

const PHASES = {
  queued: "Starting", preparing: "Preparing", extracting: "Reading the file", finishing: "Writing",
};

const SUMMARY = {
  written: "Summary written.",
  skipped: "Summary skipped (short, or turned off).",
  pending: "Summary pending: run brain enrich --backfill.",
};

const GRAPH = {
  disabled: "Graph: disabled.",
  queued: "Graph: queued for the vault watcher.",
  not_synced: "Graph: not synced; run brain graphrag build --backfill.",
};

const GONE = "This job's outcome is unknown: the server no longer has it, perhaps "
  + "because it restarted. Re-submitting is safe; identical content is a no-op.";

function kindLabel(kind) {
  return KIND_LABELS[kind] || kind;
}

/* A job the panel has stopped following: terminal on the server, or lost by it. */
export function isSettled(record) {
  return record.gone || isTerminal(record.state);
}

/* The row's one-line headline once settled, or "" while it runs. */
function headline(record) {
  if (record.gone) return "Outcome unknown";
  if (record.state === "cancelled") return "Cancelled. Nothing was written.";
  if (record.state === "failed") return "Failed";
  if (record.state === "succeeded") {
    const outcome = record.result && record.result.outcome;
    return OUTCOMES[outcome] || "Finished";
  }
  return "";
}

/* One sentence for the live region, or "" when this state says nothing new. */
export function announcement(record) {
  const kind = kindLabel(record.kind);
  if (isSettled(record)) return `${kind} job: ${headline(record)}`;
  if (record.state === "running") return `${kind} job running.`;
  return `${kind} job queued.`;
}

function lastEvent(record) {
  return record.events.reduce((latest, e) => (!latest || e.seq > latest.seq ? e : latest), null);
}

function phaseText(record) {
  if (isSettled(record)) return "";
  if (record.cancelRequested) return "Cancelling…";
  if (record.state === "queued") return "Queued";
  const event = lastEvent(record);
  if (!event) return "Running";
  if (event.phase === "embedding" && event.total) {
    return `Embedding ${event.done} of ${event.total} chunks`;
  }
  return PHASES[event.phase] || event.phase;
}

export function createJobRow(record) {
  const row = el("li", "job");
  row.dataset.jobId = record.id;
  row.tabIndex = -1;          // focus lands here when its Cancel button goes away
  const head = el("div", "job-head");
  head.append(el("span", "job-kind"), el("span", "job-time"), el("span", "job-state"));
  const phase = el("p", "job-phase");
  phase.id = `job-${record.id}-phase`;
  row.append(head, phase, el("div", "job-detail"));
  updateJobRow(row, record);
  return row;
}

function timeLabel(stamp) {
  const when = new Date(stamp);
  return stamp && !Number.isNaN(when.getTime()) ? when.toLocaleTimeString() : "";
}

export function updateJobRow(row, record) {
  const settled = isSettled(record);
  /* Redrawn each time: a job resumed after a reload learns its kind and its
     submit time from its first poll. */
  row.querySelector(".job-kind").textContent = kindLabel(record.kind);
  row.querySelector(".job-time").textContent = timeLabel(record.createdAt);
  const state = row.querySelector(".job-state");
  state.textContent = record.gone ? "unknown" : record.state;
  state.dataset.state = record.gone ? "unknown" : record.state;
  const phase = row.querySelector(".job-phase");
  phase.textContent = phaseText(record);
  syncProgress(row, record, settled);
  syncCancel(row, record, settled);
  const detail = row.querySelector(".job-detail");
  detail.replaceChildren(...detailNodes(record));
  detail.dataset.terminal = String(settled);
}

function syncProgress(row, record, settled) {
  let bar = row.querySelector("progress");
  const event = lastEvent(record);
  const busy = !settled && record.state === "running";
  if (!busy) {
    if (bar) bar.remove();
    return;
  }
  if (!bar) {
    bar = document.createElement("progress");
    bar.className = "job-progress";
    bar.setAttribute("aria-labelledby", `job-${record.id}-phase`);
    row.querySelector(".job-phase").after(bar);
  }
  if (event && event.phase === "embedding" && event.total) {
    bar.max = event.total;
    bar.value = event.done || 0;
  } else {
    bar.removeAttribute("value");   // indeterminate: preparing, writing
  }
}

function syncCancel(row, record, settled) {
  let button = row.querySelector("button.job-cancel");
  const offered = !settled && record.cancellable && !record.cancelRequested;
  if (!offered) {
    if (button) {
      const hadFocus = document.activeElement === button;
      button.remove();
      if (hadFocus) row.focus();
    }
    return;
  }
  if (!button) {
    button = el("button", "ghost-btn job-cancel", "Cancel");
    button.type = "button";
    button.dataset.action = "cancel";
    button.setAttribute("aria-describedby", `job-${record.id}-phase`);
    row.querySelector(".job-head").append(button);
  }
}

function detailNodes(record) {
  const nodes = [];
  const text = headline(record);
  if (text) nodes.push(el("p", "job-outcome", text));
  if (record.gone) nodes.push(el("p", "job-message", GONE));
  if (record.state === "failed" && record.error) {
    const message = el("p", "job-message", record.error.message);
    message.append(" ", el("code", "job-code", record.error.code));
    nodes.push(message);
  }
  if (record.state === "succeeded" && record.result) nodes.push(...resultNodes(record));
  if (record.unreachable && !isSettled(record)) {
    nodes.push(el("p", "job-note", "Cannot reach the server; still trying."));
  }
  if (record.cancelNote) nodes.push(el("p", "job-note", record.cancelNote));
  if (!isSettled(record) && record.state === "running") {
    nodes.push(el("p", "job-hint", "Ingesting: search may be slower until this finishes."));
  }
  return nodes;
}

function resultNodes(record) {
  const result = record.result;
  const nodes = [];
  if (result.title_withheld) {
    nodes.push(el("p", "job-withheld",
      "Saved. Its title is withheld on this server (confidential titles are hidden)."));
  } else if (result.document_id) {
    const doc = el("p", "job-doc");
    doc.append(el("span", "job-title", result.title || "(untitled)"), " ");
    const open = el("a", "job-open", "Open in Notes");
    open.href = `/?id=${encodeURIComponent(result.document_id)}`;
    open.dataset.action = "open";
    open.dataset.id = result.document_id;
    doc.append(open);
    nodes.push(doc);
  }
  if (result.original_filename) {
    nodes.push(el("p", "job-file", `From ${result.original_filename}`));
  }
  if (result.vault_path) nodes.push(el("code", "job-path", result.vault_path));
  if (result.outcome !== "empty") nodes.push(factsList(result));
  nodes.push(...findingNodes(result));
  if (result.egress_notice) {
    nodes.push(el("p", "job-note", "Not embedded: findable by full-text search only. "
      + "Confidential text is never sent to a hosted embedder."));
  }
  if (record.events.some((e) => e.code === "cancel_too_late")) {
    nodes.push(el("p", "job-note", "It finished before it could stop."));
  }
  return nodes;
}

function factsList(result) {
  const list = el("ul", "job-facts");
  const chunks = result.chunk_count === 1 ? "1 chunk." : `${result.chunk_count} chunks.`;
  for (const fact of [chunks, SUMMARY[result.summary_status], GRAPH[result.graph]]) {
    if (fact) list.append(el("li", "", fact));
  }
  return list;
}

function findingNodes(result) {
  const findings = result.secret_findings || [];
  if (findings.length === 0) return [];
  const nodes = [el("p", "job-note",
    `Credential-like text found (secret guard: ${result.secret_guard}).`)];
  const list = el("ul", "job-findings");
  for (const f of findings) {
    list.append(el("li", "",
      `${f.label} (${f.kind}) at line ${f.line}, column ${f.col_start}: ${f.preview}`));
  }
  nodes.push(list);
  if (result.secret_guard === "redact") {
    nodes.push(el("p", "job-note", "Stored with the matches redacted."));
  }
  return nodes;
}
