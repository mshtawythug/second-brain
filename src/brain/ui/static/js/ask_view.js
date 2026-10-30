/* One turn in the Agent tab: built once, then updated in place (RM-18b).
 *
 * IN PLACE, NOT REBUILT, for the reason ingest_view.js gives: a poll lands every
 * second, and rebuilding would destroy the Cancel button under a keyboard
 * user's focus. The row, its head, step list, phase line and Cancel button are
 * stable; only `.turn-detail` is redrawn, and only while the turn runs or once
 * it settles.
 *
 * THE HARDENING RULES (ticket RM-18b, F14 §1.1). The answer and the sub-queries
 * are written by a model that read the user's documents, so a note can steer
 * them. Therefore:
 *   1. Every string from the server becomes a TEXT node: `el(..., text)` sets
 *      `textContent`, and `answerNodes` appends `createTextNode`. This file and
 *      ask.js contain no HTML sink at all; tests/test_ui_static_behaviour.py
 *      fails the build if one appears.
 *   2. Citation markers are parsed here, from the answer STRING. `[n]` becomes a
 *      link only when `n` is a `ref` in the server's citations, that citation is
 *      not withheld, and its id is a canonical UUID. The server already drops
 *      out-of-range markers; this does not rely on it.
 *   3. No model string becomes an attribute. The one attribute set from result
 *      data is the UUID-checked `href` (the click handler reads the id back from
 *      it, so there is no `data-id`).
 *   4. The page's CSP has no `unsafe-inline`, so even a missed sink could not
 *      run inline script; tests/test_ui_security.py pins it.
 *
 * A WITHHELD ANSWER IS SHOWN AS WITHHELD (RM-18a.D1). Unless the server serves
 * confidential bodies, a citation of a non-normal note arrives with no id, title
 * or snippet, and the answer and sub-queries arrive as null because they could
 * paraphrase it. The turn says so and offers no "Open in Notes" link.
 */

import { el } from "/static/js/dom.js";
import { isTerminal } from "/static/js/jobs.js";

/* Canonical, as Postgres prints a uuid. Anchored: nothing else may ride along. */
export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/* The marker the ticket fixes: one or two digits in square brackets. */
const MARKER_RE = /\[(\d{1,2})\]/g;

const STEP_TEXT = {
  plan: () => "Planning the search",
  retrieve: (round) => `Retrieving notes${round}`,
  reflect: (round) => `Checking whether that is enough${round}`,
  synthesize: () => "Writing the answer",
};

const WAITING_ON_INGEST = "Waiting for the ingest job to finish";
const QUEUED = "Queued: waiting for the job runner";

const GONE = "This answer is no longer on the server. Asks are kept for ten minutes, "
  + "and a restart clears them. Ask again to get a fresh answer.";

const WITHHELD = "The answer is withheld on this server: it draws on a confidential "
  + "note. Ask from a terminal with brain ask to read it.";

export function isSettled(record) {
  return record.gone || isTerminal(record.state);
}

/* The id a citation may link to, or null. Rule 2's id half. */
export function linkableId(citation) {
  if (!citation || citation.withheld) return null;
  const id = citation.document_id;
  return typeof id === "string" && UUID_RE.test(id) ? id : null;
}

function noteLink(id, text, className) {
  const link = el("a", className, text);
  link.href = `/?id=${encodeURIComponent(id)}`;
  link.dataset.action = "open";
  return link;
}

/* The answer as nodes: text, with each valid `[n]` marker made a link. Rule 2. */
export function answerNodes(answer, citations) {
  const byRef = new Map();
  for (const citation of citations || []) {
    if (Number.isInteger(citation.ref)) byRef.set(citation.ref, citation);
  }
  const text = String(answer);
  const nodes = [];
  let last = 0;
  for (const match of text.matchAll(MARKER_RE)) {
    const id = linkableId(byRef.get(Number(match[1])));
    if (!id) continue;
    if (match.index > last) nodes.push(document.createTextNode(text.slice(last, match.index)));
    nodes.push(noteLink(id, match[0], "turn-cite"));
    last = match.index + match[0].length;
  }
  if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
  return nodes;
}

/* Own keys only: `in` would also accept "toString" or "constructor" from
   Object.prototype, and then call it as a step's text. */
function isStep(phase) {
  return typeof phase === "string" && Object.hasOwn(STEP_TEXT, phase);
}

function lastStep(record) {
  return record.events
    .filter((e) => isStep(e.phase))
    .reduce((latest, e) => (!latest || e.seq > latest.seq ? e : latest), null);
}

export function stepText(event) {
  const multi = event.total > 1 && event.done;
  const round = multi ? ` (round ${event.done} of ${event.total})` : "";
  return STEP_TEXT[event.phase](round);
}

function phaseText(record) {
  if (isSettled(record)) return "";
  if (record.cancelRequested) return "Cancelling…";
  if (record.state === "queued") return record.waitingOnIngest ? WAITING_ON_INGEST : QUEUED;
  const step = lastStep(record);
  return step ? stepText(step) : "Starting";
}

function headline(record) {
  if (record.gone) return "Answer unavailable";
  if (record.state === "cancelled") return "Cancelled.";
  if (record.state === "failed") return "Failed";
  return "";
}

/* One sentence for the live region per state change. */
export function announcement(record) {
  if (record.gone) return "Agent: the answer is no longer available.";
  if (record.state === "succeeded") return "Agent: answer ready.";
  if (record.state === "failed") return "Agent: the question failed.";
  if (record.state === "cancelled") return "Agent: cancelled.";
  if (record.state === "running") return "Agent: working on it.";
  return "Agent: question queued.";
}

function timeLabel(stamp) {
  const when = new Date(stamp);
  return stamp && !Number.isNaN(when.getTime()) ? when.toLocaleTimeString() : "";
}

export function createTurn(record) {
  const row = el("li", "turn");
  row.dataset.jobId = record.id;
  row.tabIndex = -1;          // focus lands here when its Cancel button goes away
  const head = el("div", "turn-head");
  head.append(el("p", "turn-question"), el("span", "turn-state"));
  const phase = el("p", "turn-phase");
  phase.id = `turn-${record.id}-phase`;
  row.append(head, el("ol", "turn-steps"), phase, el("div", "turn-detail"));
  updateTurn(row, record);
  return row;
}

export function updateTurn(row, record) {
  const settled = isSettled(record);
  /* The question is known only in the page that asked it; a turn resumed after
     a reload is labelled by its submit time (it is not on the wire). */
  row.querySelector(".turn-question").textContent = record.question
    || `Asked at ${timeLabel(record.createdAt) || "an earlier visit"}`;
  const state = row.querySelector(".turn-state");
  state.textContent = record.gone ? "unknown" : record.state;
  state.dataset.state = record.gone ? "unknown" : record.state;
  syncSteps(row, record);
  row.querySelector(".turn-phase").textContent = phaseText(record);
  syncProgress(row, record, settled);
  syncCancel(row, record, settled);
  const detail = row.querySelector(".turn-detail");
  detail.replaceChildren(...detailNodes(record));
  detail.dataset.terminal = String(settled);
}

/* One line per step event, appended once: the progress the goal asks to watch. */
function syncSteps(row, record) {
  const list = row.querySelector(".turn-steps");
  const shown = new Set([...list.children].map((li) => li.dataset.seq));
  for (const event of record.events) {
    if (!isStep(event.phase) || shown.has(String(event.seq))) continue;
    const item = el("li", "turn-step", stepText(event));
    item.dataset.seq = String(event.seq);
    list.append(item);
  }
}

function syncProgress(row, record, settled) {
  let bar = row.querySelector("progress");
  if (settled || record.state !== "running") {
    if (bar) bar.remove();
    return;
  }
  if (!bar) {
    bar = document.createElement("progress");
    bar.className = "turn-progress";
    bar.setAttribute("aria-labelledby", `turn-${record.id}-phase`);
    row.querySelector(".turn-phase").after(bar);
  }
}

function syncCancel(row, record, settled) {
  let button = row.querySelector("button.turn-cancel");
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
    button = el("button", "ghost-btn turn-cancel", "Cancel");
    button.type = "button";
    button.dataset.action = "cancel";
    button.setAttribute("aria-describedby", `turn-${record.id}-phase`);
    row.querySelector(".turn-head").append(button);
  }
}

function detailNodes(record) {
  const nodes = [];
  const text = headline(record);
  if (text) nodes.push(el("p", "turn-outcome", text));
  if (record.gone) nodes.push(el("p", "turn-message", GONE));
  if (record.state === "failed" && record.error) {
    const message = el("p", "turn-message", record.error.message);
    message.append(" ", el("code", "turn-code", record.error.code));
    nodes.push(message);
  }
  if (record.state === "succeeded" && record.result) nodes.push(...resultNodes(record.result));
  if (record.unreachable && !isSettled(record)) {
    nodes.push(el("p", "turn-note", "Cannot reach the server; still trying."));
  }
  if (record.cancelNote) nodes.push(el("p", "turn-note", record.cancelNote));
  if (!isSettled(record)) {
    nodes.push(el("p", "turn-hint",
      "A question can take a minute or more, and an ingest waits behind it."));
  }
  return nodes;
}

function resultNodes(result) {
  const nodes = [];
  if (result.fallback_note) nodes.push(el("p", "turn-notice", result.fallback_note));
  if (typeof result.answer === "string") {
    const answer = el("p", "turn-answer");
    answer.append(...answerNodes(result.answer, result.citations));
    nodes.push(answer);
  } else {
    nodes.push(el("p", "turn-withheld", WITHHELD));
  }
  const citations = result.citations || [];
  if (citations.length) nodes.push(citationList(citations));
  else nodes.push(el("p", "turn-note", "No note was cited."));
  if (Array.isArray(result.sub_queries) && result.sub_queries.length) {
    nodes.push(subQueryList(result.sub_queries));
  }
  nodes.push(el("p", "turn-facts", factsText(result)));
  return nodes;
}

function citationList(citations) {
  const list = el("ol", "turn-citations");
  for (const citation of citations) {
    const item = el("li", "turn-citation");
    item.append(el("span", "turn-ref", `[${citation.ref}]`), " ");
    const id = linkableId(citation);
    if (citation.withheld) {
      item.append(el("span", "turn-cite-withheld",
        "A confidential note (withheld on this server)"));
    } else {
      item.append(el("span", "turn-cite-title", citation.title || "(untitled)"));
      if (citation.source_kind) item.append(" ", el("span", "turn-cite-kind", citation.source_kind));
      if (id) item.append(" ", noteLink(id, "Open in Notes", "turn-open"));
      if (citation.snippet) item.append(el("p", "turn-snippet", citation.snippet));
    }
    list.append(item);
  }
  return list;
}

function subQueryList(subQueries) {
  const wrap = el("div", "turn-subqueries");
  wrap.append(el("p", "turn-label", "Searched for"));
  const list = el("ul", "");
  for (const query of subQueries) list.append(el("li", "", String(query)));
  wrap.append(list);
  return wrap;
}

function factsText(result) {
  const parts = [];
  if (Number.isFinite(result.elapsed_ms)) parts.push(`${(result.elapsed_ms / 1000).toFixed(1)} s`);
  if (Number.isInteger(result.iterations_used)) {
    parts.push(result.iterations_used === 1 ? "1 round" : `${result.iterations_used} rounds`);
  }
  if (result.fallback_used) parts.push("no planning loop");
  return parts.join(" · ");
}
