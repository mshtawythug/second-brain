/* Email-thread reading mode: uniform sections, and "only my replies".
 *
 * WHAT THE SERVER ALREADY DID. `brain.ingest.gmail` assembles a thread with
 * every message except the most recent wrapped in `<details><summary>`, and
 * `brain.ui.render`'s thread rule re-emits those structurally (T18, option a).
 * So collapsed-by-default is already true when this module runs; nothing here
 * needs to collapse anything.
 *
 * WHAT IS LEFT IS THE ASYMMETRY. The newest message is a plain `## H2`
 * (gmail.py's `collapsed=(idx != last_idx)` over an ascending sort, so it is the
 * LAST section, not the first). It therefore has no summary bar, no twisty, and
 * cannot be collapsed, while every older message can. This module wraps it in a
 * synthetic `<details open>` so all messages share one shape and one hook.
 *
 * The Quartz overlay's `emailThread.js` used to call that H2 "leading" in its
 * header comment; that has been corrected there, and the ordering fact is held
 * by `tests/test_gmail_thread.py::test_most_recent_message_not_collapsed`
 * (`last_h2 > last_details`) rather than by either comment.
 *
 * THE WRAP HAPPENS IN THE DOM, NEVER IN render.py, AND THAT IS LOAD-BEARING.
 * `extract_headings` walks the MARKDOWN server-side to build the TOC and mint
 * anchors (T5). Promoting the H2 to a `<details>` server-side would delete a
 * heading from that walk and leave the marginalia rail pointing at an id the
 * page does not contain — defect S4 all over again. Working on rendered DOM
 * leaves the server's heading extraction untouched by construction.
 *
 * AND THE `<h2>` ELEMENT ITSELF IS MOVED, NOT REPLACED. It carries the anchor
 * id the TOC links to, so it is relocated INTO the `<summary>` rather than
 * having its text copied out. `<summary>`'s content model permits one heading
 * element, so this is valid HTML, the id still resolves, and clicking the TOC
 * entry still scrolls to it. Copying the text and dropping the element would
 * break every TOC link into the newest message — silently, because the link
 * would still exist.
 */

import { $, el } from "/static/js/dom.js";
import { state, subscribe } from "/static/js/store.js";

let wired = false;

/* `YYYY-MM-DD HH:MM — sender`. The gate that stops an ordinary `## Conclusion`
   on an unrelated note being mistaken for a thread message — the same guard the
   overlay applies, and the reason this module does nothing at all on the vast
   majority of documents. */
const THREAD_HEADING_RE = /^\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+—\s+\S/;

/* Idempotent: the browser harness mounts this itself, and boot() will too. */
export function wireThread() {
  if (wired) return;
  wired = true;
  subscribe(renderThread);
  renderThread();
}

export function renderThread() {
  const body = document.querySelector(".note-body");
  if (!body || body.dataset.threadReady === "1") return;

  const sections = [...body.querySelectorAll("details.thread-message")];
  const newest = findNewestHeading(body);
  /* Neither a rendered thread section nor a thread-shaped heading: this is an
     ordinary note and the module leaves it completely alone. */
  if (!sections.length && !newest) return;

  if (newest) wrapNewest(body, newest);
  body.dataset.threadReady = "1";
  mountFilter(body);
}

function findNewestHeading(body) {
  for (const heading of body.querySelectorAll("h2")) {
    /* Skip a heading already inside a section — only a TOP-LEVEL h2 is the
       un-wrapped newest message. */
    if (heading.closest("details.thread-message")) continue;
    if (THREAD_HEADING_RE.test(heading.textContent || "")) return heading;
  }
  return null;
}

/* Wrap the newest message — its heading plus every sibling after it, up to the
   next thread section or the end — in an OPEN details. */
function wrapNewest(body, heading) {
  const section = el("details", "thread-message thread-newest");
  section.open = true;
  const summary = el("summary");

  const trailing = [];
  for (let node = heading.nextElementSibling; node; node = node.nextElementSibling) {
    if (node.matches("details.thread-message")) break;
    trailing.push(node);
  }

  heading.replaceWith(section);
  /* The heading element itself moves into the summary — see the header note:
     it carries the TOC's anchor id. */
  summary.appendChild(heading);
  section.appendChild(summary);
  for (const node of trailing) section.appendChild(node);
}

/* ------------------------------------------------------ only my replies --- */

/* The address the server reported for THIS request (`/api/health`), not a value
   baked in at build time. Changing BRAIN_USER_EMAIL takes effect on the next
   page load; the wiki needs a full rebuild for the same change. */
function ownerEmail() {
  const health = state.health;
  const value = health && typeof health.user_email === "string" ? health.user_email : "";
  return value.trim().toLowerCase();
}

function mountFilter(body) {
  const owner = ownerEmail();
  /* No configured address means the question "which of these are mine?" has no
     answer, so the control is not offered. A checkbox that filtered to nothing
     would look like "you wrote none of these" rather than "unconfigured". */
  if (!owner) return;
  if (body.parentElement.querySelector(".thread-filter")) return;

  const label = el("label", "thread-filter");
  const box = el("input");
  box.type = "checkbox";
  box.addEventListener("change", () => applyFilter(body, box.checked));
  label.appendChild(box);
  label.appendChild(el("span", null, "Only my replies"));
  body.parentElement.insertBefore(label, body);
}

/* Matched on the SUMMARY text, which is where the address is.
   `_format_thread_section` escapes the summary heading precisely so the
   `Name <addr>` form survives as readable text — its comment names this filter
   as the reason. Substring rather than parse: the header form varies
   (`Name <addr>`, bare `addr`, quoted display names), and the address itself is
   the only part that is reliably present and unambiguous. */
function applyFilter(body, only) {
  const owner = ownerEmail();
  for (const section of body.querySelectorAll("details.thread-message")) {
    const summary = section.querySelector("summary");
    const text = (summary ? summary.textContent : "").toLowerCase();
    section.hidden = only && !text.includes(owner);
  }
}
