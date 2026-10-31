/* The Ingest tab, inbox mode (RM-40; Ingest spec §5.5).
 *
 * A NAME, NEVER A PATH. `GET /api/ingest/inbox` lists the files already sitting
 * in the inbox folder (a drop folder with `kind = "manual"` and `auto = false`)
 * by name and size; the user picks one and the Ingest button sends
 * `POST /api/ingest/inbox {name, ...}`. The server re-checks the name, refuses a
 * link or anything that is not a plain file, and moves the file into the
 * inbox's `imported/` folder before ingesting it.
 *
 * ONE RADIO PER FILE, so the list is a real form control: arrow keys move the
 * choice, and the label says the name and the size.
 *
 * THE FOLDER'S SENSITIVITY IS THE DEFAULT. An inbox is `confidential` unless its
 * entry says otherwise, so switching to this mode preselects the level the
 * listing names, but only while this mode is still the one shown: a listing that
 * answers after the user has left never touches the radio. The user can still
 * change it.
 *
 * FETCHED ONLY ON DEMAND. The listing names private files, so it is requested
 * when the user picks this mode (or Refresh), never on page load or in the
 * background: a prompted view (#110 review round 1, L5).
 *
 * Every name is the user's own file name: it is shown as text, never logged.
 */

import { $, el } from "/static/js/dom.js";
import { api } from "/static/js/api.js";
import { submitJob } from "/static/js/jobs.js";

const REASONS = {
  inbox_unconfigured: "No inbox folder is set up. Add one to drop-folders.toml "
    + "(kind = \"manual\", auto = false); see the drop-folders guide.",
  inbox_misconfigured: "drop-folders.toml has a mistake. Run brain doctor to see which entry.",
  inbox_unreadable: "The inbox folder could not be read.",
};

let listing = null;       // the last GET answer, or null before the first one
let hooks = null;         // { showError, announce }

function size(n) {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KiB`;
  return n === 1 ? "1 byte" : `${n} bytes`;
}

function radio(file, index) {
  const wrap = el("li", "ingest-radio ingest-inbox-file");
  const input = document.createElement("input");
  input.type = "radio";
  input.name = "ingest-inbox-file";
  input.id = `ingest-inbox-file-${index}`;
  input.value = file.name;
  const label = el("label", "", `${file.name} (${size(file.size)})`);
  label.htmlFor = input.id;
  wrap.append(input, label);
  return wrap;
}

function render() {
  const list = $("ingest-inbox-list");
  const files = (listing && listing.files) || [];
  list.replaceChildren(...files.map(radio));
  const empty = $("ingest-inbox-empty");
  if (!listing || !listing.available) {
    empty.textContent = (listing && REASONS[listing.reason]) || "The inbox is not available.";
  } else if (!(listing.extensions || []).length) {
    /* A `manual` folder with no `extensions` gets the image defaults; files sit
       there, so "empty" would be wrong (#110 review F2). */
    empty.textContent = "The inbox takes no file type this tab can ingest. Add "
      + "\".md\", \".txt\", \".pdf\" or \".docx\" to its extensions in drop-folders.toml.";
  } else if (!files.length) {
    empty.textContent = "The inbox is empty.";
  } else {
    empty.textContent = listing.truncated ? "Showing the newest files only." : "";
  }
  empty.hidden = !empty.textContent;
  const takes = (listing && listing.extensions) || [];
  $("ingest-inbox-hint").textContent = takes.length
    ? `Files of type ${takes.join(", ")}. Each is moved into the inbox's imported folder.`
    : "";
}

function preselectSensitivity() {
  const level = listing && listing.default_sensitivity;
  const input = level && document.getElementById(`ingest-sensitivity-${level}`);
  if (input) input.checked = true;
}

/* Fetch the listing again and redraw it; an error is shown, never thrown. */
export async function refreshInbox() {
  try {
    listing = await api("/api/ingest/inbox");
  } catch (error) {
    listing = null;
    hooks.showError(error.message);
  }
  render();
  return listing;
}

/* Called when the user switches to inbox mode. */
export async function showInbox() {
  await refreshInbox();
  if ($("ingest-mode-inbox").checked) preselectSensitivity();
}

/* The client-side check: a file must be picked. "" when it is. */
export function inboxProblem() {
  return pickedName() ? "" : "Pick a file from the inbox.";
}

function pickedName() {
  const checked = $("ingest-inbox-list").querySelector("input:checked");
  return checked ? checked.value : "";
}

/* Submit the picked file with the shared fields; the row leaves the list. */
export async function submitInboxFile(common) {
  const name = pickedName();
  const title = $("ingest-inbox-title").value.trim();
  let snapshot;
  try {
    snapshot = await submitJob("/api/ingest/inbox", {
      name, ...(title ? { title } : {}), ...common,
    });
  } catch (error) {
    /* A refusal can mean the list is stale (the file moved or changed): redraw it. */
    if (error.status >= 400 && error.status < 500) refreshInbox();
    throw error;
  }
  listing = { ...listing, files: listing.files.filter((f) => f.name !== name) };
  $("ingest-inbox-title").value = "";
  render();
  return snapshot;
}

export function wireInboxMode(given) {
  hooks = given;
  $("ingest-inbox-refresh").addEventListener("click", () => {
    hooks.showError("");
    refreshInbox().then(() => hooks.announce("Inbox list refreshed."));
  });
}
