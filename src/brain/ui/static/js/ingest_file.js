/* The Ingest tab, file mode (RM-13, Ingest spec §5.2, §8).
 *
 * TWO REQUESTS, AS THE SERVER WANTS THEM. A file is STAGED the moment it is
 * picked or dropped (`POST /api/ingest/uploads`, raw bytes via upload.js), so a
 * refused type or a mismatched file is reported before the user fills in tags.
 * The Ingest button then SUBMITS it (`POST /api/ingest/file`, JSON) as a job.
 * Remove, or choosing another file, discards the staged copy
 * (`DELETE /api/ingest/uploads/{id}`). A staged file the server has expired
 * (15 minutes) answers 404 at submit, and the user is asked to choose it again.
 *
 * CHECKED HERE FIRST, AGAIN THERE. The extension and size limits come from
 * `GET /api/ingest/config`, so the client refuses an obvious mistake without a
 * round trip; the server repeats every check, and sniffs the bytes, which the
 * client cannot.
 *
 * NEVER A DRAG-ONLY AFFORDANCE. The drop zone is a real <button> that opens the
 * file input, so a keyboard user reaches the same path. One file per job.
 *
 * A FILE DROPPED BESIDE THE ZONE IS NOT LOST. The browser's default for a file
 * dropped on the page is to open it, replacing the app (and any unsent form).
 * While file mode is shown, window-level listeners cancel that default for a
 * drag that carries files, and a drop anywhere on the page is treated as a drop
 * on the zone. In paste mode the window listeners do nothing.
 *
 * A STALE ANSWER IS DISCARDED. Each selection bumps `generation`; an upload that
 * lands after the user removed it, or chose another file, is deleted on the
 * server rather than shown.
 *
 * The file's name is the user's own; it is shown, as text, and never logged.
 */

import { $ } from "/static/js/dom.js";
import { submitJob } from "/static/js/jobs.js";
import { discardUpload, uploadFile } from "/static/js/upload.js";

let staged = null;        // { name, size, status: "uploading" | "staged", uploadId }
let generation = 0;
let hooks = null;         // { config, showError, announce }

function extensionOf(name) {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot).toLowerCase() : "";
}

function mib(n) {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KiB`;
  return n === 1 ? "1 byte" : `${n} bytes`;
}

/* The extensions this server takes, each with its cap. An extension the config
   names without a cap is not offered: the client never guesses a limit. */
function offered(cfg) {
  const caps = (cfg.limits && cfg.limits.upload_bytes) || {};
  return (cfg.extensions || []).filter((ext) => Number.isFinite(caps[ext]))
    .map((ext) => ({ ext, cap: caps[ext] }));
}

/* True when this server accepts at least one file type. */
export function acceptsFiles(cfg) {
  return offered(cfg).length > 0;
}

/* Why the client refuses `file` before sending it, or "". */
function refusal(file, cfg) {
  const types = offered(cfg);
  const ext = extensionOf(file.name);
  const match = types.find((t) => t.ext === ext);
  if (!match) return `Only ${types.map((t) => t.ext).join(", ")} files can be uploaded.`;
  if (file.size > match.cap) {
    return `The file is ${mib(file.size)}; the limit for ${ext} is ${mib(match.cap)}.`;
  }
  return "";
}

function render() {
  const box = $("ingest-staged");
  box.hidden = !staged;
  if (!staged) return;
  $("ingest-staged-name").textContent = staged.name;
  $("ingest-staged-state").textContent = staged.status === "uploading"
    ? "Uploading…" : `Ready (${mib(staged.size)})`;
}

function clear() {
  staged = null;
  $("ingest-file").value = "";
  render();
}

/* Forget the staged file and delete it on the server. A failure is harmless:
   the server expires an unsubmitted upload after 15 minutes. */
function discard() {
  const previous = staged;
  generation += 1;
  clear();
  if (previous && previous.uploadId) discardUpload(previous.uploadId).catch(() => {});
}

async function choose(file) {
  const cfg = hooks.config();
  const message = refusal(file, cfg);
  hooks.showError(message);
  if (message) {
    hooks.announce(message);
    return;
  }
  discard();
  const mine = generation;
  staged = { name: file.name, size: file.size, status: "uploading", uploadId: null };
  render();
  hooks.announce(`Uploading ${file.name}.`);
  let answer;
  try {
    answer = await uploadFile(file);
  } catch (error) {
    if (mine !== generation) return;
    clear();
    hooks.showError(error.message);
    hooks.announce(`Upload refused: ${error.message}`);
    return;
  }
  if (mine !== generation) {
    discardUpload(answer.upload_id).catch(() => {});
    return;
  }
  staged = { ...staged, status: "staged", uploadId: answer.upload_id, size: answer.size };
  render();
  hooks.announce(`${file.name} is ready to ingest.`);
}

function onPicked() {
  const files = $("ingest-file").files;
  if (files && files.length === 1) choose(files[0]);
}

function onDrop(event) {
  event.preventDefault();
  $("ingest-drop").removeAttribute("data-dragging");
  if ($("ingest-drop").disabled) return;
  const files = event.dataTransfer ? event.dataTransfer.files : null;
  if (!files || files.length !== 1) {
    const message = "Drop one file at a time.";
    hooks.showError(message);
    hooks.announce(message);
    return;
  }
  choose(files[0]);
}

function onDragOver(event) {
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  $("ingest-drop").setAttribute("data-dragging", "true");
}

function onDragLeave() {
  $("ingest-drop").removeAttribute("data-dragging");
}

function carriesFiles(event) {
  return Boolean(event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files"));
}

/* Only while file mode is on screen; the zone's own handlers run first. */
function fileModeShown() {
  return !$("ingest-file-fields").hidden && !$("tab-ingest").hidden;
}

function onWindowDragOver(event) {
  if (event.defaultPrevented || !carriesFiles(event) || !fileModeShown()) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
}

function onWindowDrop(event) {
  if (event.defaultPrevented || !carriesFiles(event) || !fileModeShown()) return;
  onDrop(event);
}

/* The hint under the drop zone: the types and sizes this server accepts. */
export function describeFileLimits(cfg) {
  const parts = offered(cfg).map((t) => `${t.ext} up to ${mib(t.cap)}`);
  $("ingest-file-hint").textContent = parts.length
    ? `One file at a time: ${parts.join(", ")}.`
    : "This server accepts no file uploads.";
}

/* Why file mode cannot submit yet, or "". */
export function fileProblem() {
  if (!staged) return "Choose a file to ingest.";
  if (staged.status === "uploading") return "Wait for the upload to finish.";
  return "";
}

/* Submit the staged file as a job. Resolves to the job's snapshot. */
export async function submitStagedFile(fields) {
  const body = { upload_id: staged.uploadId, ...fields };
  const title = $("ingest-file-title").value.trim();
  if (title) body.title = title;
  try {
    const snapshot = await submitJob("/api/ingest/file", body);
    clear();                 // claimed by the job: the server deletes it when it settles
    $("ingest-file-title").value = "";
    return snapshot;
  } catch (error) {
    if (error.status !== 404) throw error;
    clear();
    /* A new error, same shape: the caller's `catch` reports `message`. */
    const expired = new Error("The uploaded file expired before it was ingested. Choose it again.");
    expired.code = error.code;
    expired.status = error.status;
    throw expired;
  }
}

export function wireFileMode(options) {
  hooks = options;
  const drop = $("ingest-drop");
  drop.addEventListener("click", () => $("ingest-file").click());
  drop.addEventListener("dragover", onDragOver);
  drop.addEventListener("dragleave", onDragLeave);
  drop.addEventListener("drop", onDrop);
  window.addEventListener("dragover", onWindowDragOver);
  window.addEventListener("drop", onWindowDrop);
  $("ingest-file").addEventListener("change", onPicked);
  $("ingest-staged-remove").addEventListener("click", () => {
    const name = staged ? staged.name : "";
    discard();
    hooks.showError("");
    if (name) hooks.announce(`${name} removed.`);
    drop.focus();
  });
}
