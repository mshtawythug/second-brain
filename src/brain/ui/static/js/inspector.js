/* The right column: view/edit toggle, save indicator, frontmatter, modals.
 *
 * Imports `loadTree` from tree.js while tree.js imports `openNote` from here —
 * see the cycle note at the top of tree.js. Nothing here calls an imported
 * binding at module-evaluation time, which is what makes that legal.
 */

import { api } from "/static/js/api.js";
import { $, el, toast } from "/static/js/dom.js";
import { dispatch, state, syncUrl } from "/static/js/store.js";
import { loadTree } from "/static/js/tree.js";

const SAVE_LABELS = {
  saved: "● Saved", dirty: "● Unsaved", saving: "Saving…",
  error: "● Error", conflict: "● Conflict",
};

/* Update the save indicator WITHOUT a dispatch.
 *
 * Every status that leaves `editing` true must come through here. dispatch()
 * notifies renderInspector, which rebuilds the <textarea> and calls focus() on
 * the new one — dropping the caret at the end of the document. That is fine
 * when the editor is going away (a successful save sets editing:false and the
 * preview replaces it), and destructive when it is not: `saving` fires before
 * the request is even sent, and `error` / `conflict` fire while the user is
 * still holding unsaved text.
 *
 * This preserves the CARET on a failed save. It does not, by itself, preserve
 * the EDIT — that is A7, and phase 4 closed it: `confirmDiscardDraft` asks
 * before the reopen that used to destroy the draft, and `overwriteOnDisk` is
 * the exit that keeps it.
 *
 * The claim this comment used to make — "a 409 still discards in-progress
 * work" — was WRONG, and measuring it is what redirected the fix. A 409 never
 * discarded the edit: the text survived the conflict and survived retries. It
 * STRANDED it, unsaveable, and the reopen the toast recommended is what
 * destroyed it. So the defect was an interface advising the one action that
 * lost the work, not an interface throwing it away. */
function setSaveStatus(next) {
  state.saveStatus = next;
  const node = document.querySelector(".save-state");
  if (!node) return;
  node.dataset.state = next;
  node.textContent = SAVE_LABELS[next];
}

export async function openNote(id) {
  /* A7: ask BEFORE the dispatch below, which is what destroys the draft.
     Only when there is something to lose — a guard that prompted on every
     reopen would make the app unusable and would be the over-reaching form of
     this fix. */
  if (!(await confirmDiscardDraft())) return;
  dispatch({ selectedId: id, editing: false, saveStatus: "saved" });
  syncUrl();
  document.body.dataset.view = "note";
  try {
    const query = state.sessionId ? `?session_id=${encodeURIComponent(state.sessionId)}` : "";
    const note = await api(`/api/notes/${encodeURIComponent(id)}${query}`);
    /* `?? ""` because T8 made `body` OMITTED rather than nulled on a read-only
       server — it is the largest thing on the wire and unusable without an
       editor — so `note.body` is `undefined` there and `state.draftBody` is
       declared a string in store.js.
       Nothing is broken today: edit mode is unreachable read-only (`editable`
       is False and keys.js's Cmd+E checks it), so the `undefined` never reaches
       a textarea and hasUnsavedEdit() compares undefined to undefined. That is
       ACCIDENTALLY correct, not intentionally so, and it is the shape that
       breaks when something adjacent moves. See hasUnsavedEdit() for the other
       half — coercing only here would make the comparison `"" !== undefined`,
       i.e. permanently dirty, so the two coercions are one change. */
    dispatch({ note, draftBody: note.body ?? "" });
  } catch (error) {
    dispatch({ note: null });
    toast(error.message, "error");
  }
}

/* THE HOST HAS EXACTLY TWO ROWS. READ THIS BEFORE ADDING ANYTHING TO IT.
 *
 * `.inspector` is `display: grid; grid-template-rows: auto 1fr` (layout.css),
 * so this host takes exactly TWO children on every path: `.note-head`, then the
 * body/editor on the `1fr` track. A third `host.appendChild` inserted between
 * them takes the fill track and pushes the body/editor onto an implicit auto
 * row — where `.inspector > .editor { align-self: stretch }` has nothing to
 * stretch into and `resize: vertical` on the editor stops meaning anything.
 *
 * SO: NEW FURNITURE GOES INSIDE `head`, NOT BESIDE IT. That is what the
 * `.note-head` wrapper is for — "everything above the body" — and it is why
 * the summary lede below appends to `head` even though its two early-return
 * guards live further down.
 *
 * THIS NOTE IS HERE, AT THE SHARED HOST, ON PURPOSE. The constraint is fully
 * documented in layout.css beside the `grid-template-rows` rule, and
 * `marginalia.js` documents its own consequence of it (its block appends
 * LAST). Neither was reachable from this line: a rule about a shared DOM host,
 * written only in one consumer's file or only in the stylesheet, is invisible
 * to the next author editing this function — which is exactly how the lede
 * shipped as a sixth append and was caught by a static count guard rather than
 * by anyone reading. Two guards hold it, and they are NOT redundant:
 *   - `check_resize_is_not_inert` (tests/test_ui_static_behaviour.py) counts
 *     `host.appendChild(` textually and pins it at five;
 *   - `test_the_body_is_the_second_inspector_child_even_with_a_lede`
 *     (tests/test_ui_browser_lede.py) asserts the rendered POSITION.
 * The second exists because raising the first's expected count from 5 to 6 is
 * the tempting fix and the wrong one — it silences the guard while leaving the
 * body off the fill track. The position test stays red through that.
 */
export function renderInspector() {
  const host = $("inspector");
  host.textContent = "";
  const note = state.note;
  if (!note) {
    host.appendChild(el("p", "empty", "Select a note from the vault, or search."));
    return;
  }

  /* One wrapper for everything above the body, so .inspector is exactly TWO
     grid rows — `auto 1fr` — and the body/editor always lands on the 1fr track
     however many header pieces are showing. Without it the fill target's row
     index varies: .back-btn is `display: none` above 780px and generates no
     box at all, so the count changes with the viewport. Grid is what lets a
     user's resize win (an explicit height defeats `stretch`, unlike flex-grow,
     which treats it as a mere base size) — measured, not assumed. */
  const head = el("div", "note-head");

  /* Below 780px the ledger and inspector are a two-view stack, so opening a
     note replaces the result list entirely. Without this the only way back is
     the browser's back button — a dead end on a touch device. CSS shows it
     only at that breakpoint; every shortcut needs a visible equivalent. */
  const back = el("button", "ghost-btn back-btn", "← Results");
  back.type = "button";
  back.addEventListener("click", () => { document.body.dataset.view = "list"; });
  head.appendChild(back);

  head.appendChild(el("h1", "note-title", note.title));

  const bar = el("div", "note-bar");
  const readOnly = state.health && state.health.read_only;

  if (!readOnly && note.editable && !note.withheld) {
    const edit = el("button", "ghost-btn", state.editing ? "Preview" : "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => dispatch({ editing: !state.editing }));
    bar.appendChild(edit);

    if (state.editing) {
      const save = el("button", "accent-btn", "Save");
      save.type = "button";
      save.addEventListener("click", saveNote);
      bar.appendChild(save);
    }
  }

  const status = el("span", "save-state", SAVE_LABELS[state.saveStatus]);
  status.dataset.state = state.saveStatus;
  bar.appendChild(status);

  if (!readOnly) {
    const draft = el("button", "ghost-btn", note.draft ? "○ Draft" : "Published");
    draft.type = "button";
    draft.addEventListener("click", () => toggleDraft(!note.draft));
    bar.appendChild(draft);

    if (note.movable) {
      const move = el("button", "ghost-btn", "⋯ Move");
      move.type = "button";
      move.addEventListener("click", moveNote);
      bar.appendChild(move);
    }

    const del = el("button", "ghost-btn", "⌫ Delete");
    del.type = "button";
    del.addEventListener("click", deleteNote);
    bar.appendChild(del);
  }
  head.appendChild(bar);

  const fm = el("div", "frontmatter");
  fm.appendChild(el("div", null, note.vault_path || "(not exported to the vault)"));
  fm.appendChild(el("div", null,
    `type ${note.content_type} · ${note.tier} · ${note.source_kind || "—"}` +
    (note.sensitivity && note.sensitivity !== "normal" ? ` · ${note.sensitivity}` : "")));
  if (note.tags.length) fm.appendChild(el("div", null, note.tags.map((t) => `[${t}]`).join(" ")));
  head.appendChild(fm);
  host.appendChild(head);

  if (note.withheld) {
    /* `withheld` is present ONLY when the body was withheld — same key and
       same message vocabulary as MCP brain_show, so a client handles one
       spelling across both surfaces. */
    host.appendChild(el("p", "withheld", note.withheld));
    return;
  }

  if (state.editing) {
    const area = el("textarea", "editor");
    area.value = state.draftBody;
    area.setAttribute("aria-label", "Note source");
    /* NO dispatch() here, and that is the whole point — see setSaveStatus. The
       first keystroke after opening a note used to rebuild this very textarea
       and throw away the caret position and the scroll offset. */
    area.addEventListener("input", () => {
      state.draftBody = area.value;
      if (state.saveStatus !== "dirty") setSaveStatus("dirty");
    });
    host.appendChild(area);
    area.focus();
    return;
  }

  /* The summary lede — `documents.summary`, the LLM-generated precis.
   *
   * TRUTHINESS AFTER TRIMMING, not key presence, and that is the whole guard.
   * `notes_service.read_note` omits the key when the column is NULL, so `in`
   * would be right for that case and wrong for the other two the column
   * actually holds: an empty string, and whitespace an enricher left behind.
   * Either one renders an <aside> with a rule and a block of padding above a
   * note that has nothing to say — on a corpus where most documents predate
   * enrichment, that is a band of empty furniture over most of the vault.
   * So: no summary, no element. Not a hidden element, not an empty one.
   *
   * Placed AFTER the withheld early-return above and it must stay there.
   * `summary` is derived FROM the body, so a precis beside a withheld body
   * hands out exactly the content being protected — the server already
   * withholds it for that reason, and this is the client-side half of the same
   * rule rather than a duplicate of it.
   *
   * Read mode only: in the editor the <textarea> IS the document, and a
   * server-generated summary of the pre-edit text sitting above it would
   * describe something the user is in the middle of changing.
   *
   * IT GOES INTO `head`, NOT ONTO `host`, and that is a layout constraint
   * rather than a preference. `.inspector` is `grid-template-rows: auto 1fr`,
   * so the body/editor must be the SECOND host child on every path; a lede
   * appended beside it makes the LEDE the 1fr child and the body stops
   * filling. `check_resize_is_not_inert` in tests/test_ui_static_behaviour.py
   * pins the host-append count at five for exactly this reason, and it is what
   * caught the first version of this line. `head` is already in the DOM by
   * here — appending to a live node is what keeps the guard's count honest
   * while still letting the two early returns above decide whether this runs
   * at all, which is the whole reason the call site is down here. */
  const summary = (note.summary || "").trim();
  if (summary) head.appendChild(el("aside", "lede", summary));

  const body = el("div", "note-body");
  /* The ONLY innerHTML in this file. The value is server-rendered by
     brain.ui.render, which parses with html=False (raw tags are escaped, not
     passed through) and strips any href scheme outside http/https/mailto. The
     default-src 'none' CSP is the second layer behind that. */
  body.innerHTML = note.html;
  host.appendChild(body);
}

export async function saveNote() {
  const note = state.note;
  setSaveStatus("saving");     /* editing stays true — must not rebuild the editor */
  try {
    const result = await api(`/api/notes/${encodeURIComponent(note.id)}`, {
      method: "PUT",
      body: { body_hash: note.body_hash, body: state.draftBody },
    });
    note.body = state.draftBody;
    note.body_hash = result.body_hash;
    note.html = result.html;
    dispatch({ saveStatus: "saved", editing: false });
    toast("Saved");
  } catch (error) {
    /* Both of these leave `editing` true — the user is still in the editor
       holding text that did not save. Dispatching here would rebuild the
       textarea and throw their caret to the end at the worst possible moment. */
    if (error.code === "stale_write") {
      setSaveStatus("conflict");
      /* The old message advised reopening — the ONE action that destroys the
         work, phrased as the safe option. Measured: the edit survives the 409
         and survives retries; the reopen is what kills it. Point at the button
         instead, which is now the only exit that keeps the text. */
      showConflictAction();
      toast(
        "This note changed on disk. Your text is safe here — use “Replace on "
        + "disk” to save it over the other version.",
        "error",
      );
      return;
    }
    setSaveStatus("error");
    toast(error.message, "error");
  }
}

async function toggleDraft(next) {
  try {
    await api(`/api/notes/${encodeURIComponent(state.note.id)}/draft`, {
      method: "POST", body: { draft: next },
    });
    state.note.draft = next;
    dispatch({});
    loadTree();
    toast(next ? "Marked as draft" : "Published");
  } catch (error) { toast(error.message, "error"); }
}

async function moveNote() {
  const folder = prompt("Move to folder (vault-relative, blank for root):", "");
  if (folder === null) return;
  try {
    await api(`/api/notes/${encodeURIComponent(state.note.id)}/move`, {
      method: "POST", body: { confirm: true, new_folder: folder },
    });
    toast("Moved");
    await openNote(state.note.id);
    loadTree();
  } catch (error) { toast(error.message, "error"); }
}

/* Configure the shared confirm dialog COMPLETELY, every time.
 *
 * `#confirm-dialog` is markup shared by every destructive action, and its
 * defaults are delete's: the heading says "Delete note", the button says
 * "Delete", and a typed-title field gates it. Before this helper, `deleteNote`
 * set only two of those and relied on the rest being untouched — which is fine
 * with exactly one caller and silently wrong with two. A second caller that
 * changed the heading would leave delete's dialog reading "Overwrite" forever
 * after, and nothing would fail.
 *
 * So every caller states every field. `hidden` rather than a style property:
 * the CSP is `style-src 'self'` with no `'unsafe-inline'`, and the attribute
 * costs no stylesheet rule.
 *
 * Returns the elements the caller still needs.
 */
function configureConfirm({ title, text, okLabel, gateOnTitle }) {
  const dialog = $("confirm-dialog");
  const input = $("confirm-input");
  const ok = $("confirm-ok");
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  ok.textContent = okLabel;
  input.value = "";
  /* A dismissed dialog must never read as consent. `returnValue` is STICKY —
     it survives a bare `close()` — so today the only thing preventing a
     dismissal from carrying the previous "ok" is Chromium's Escape handling.
     Clearing it here makes that safety ours rather than the browser's. */
  dialog.returnValue = "";
  /* The label element is the input's parent `.field`; hiding the input alone
     would leave its caption floating. */
  const field = input.closest(".field");
  if (field) field.hidden = !gateOnTitle;
  ok.disabled = Boolean(gateOnTitle);
  return { dialog, input, ok };
}

/* True when the editor holds text that differs from what the server last gave
   us. The comparison is against `note.body`, NOT against a dirty flag: a user
   who types and then undoes back to the original has nothing to lose, and
   prompting them would be the over-reaching form of this guard. */
function hasUnsavedEdit() {
  /* `?? ""` on BOTH sides of the comparison — the other half of the coercion in
     openNote(). A read-only payload omits `body` entirely (T8), so comparing a
     seeded `""` against `undefined` would report every such note as dirty and
     prompt on every navigation. Unreachable today, because edit mode is
     unreachable read-only; written so that it stays correct on purpose rather
     than by the accident of two undefineds matching. */
  return Boolean(
    state.editing && state.note && state.draftBody !== (state.note.body ?? ""),
  );
}

/* Ask before throwing away an in-progress edit. Resolves true to proceed.
 *
 * A7. Opening another note calls `dispatch({ note, draftBody: note.body })`,
 * which overwrites the draft — and after a 409 that reopen is the ONLY exit the
 * interface offers, so the advice destroyed the work. Measured, not assumed:
 * the text survives the conflict and survives retries; reopening is what kills
 * it. This is the consent that was missing.
 */
function confirmDiscardDraft() {
  if (!hasUnsavedEdit()) return Promise.resolve(true);
  const { dialog } = configureConfirm({
    title: "Discard your unsaved changes?",
    text: `"${state.note.title}" has edits that were never saved. Leaving this `
      + `note discards them; there is no undo.`,
    okLabel: "Discard changes",
    gateOnTitle: false,
  });
  return new Promise((resolve) => {
    dialog.addEventListener(
      "close", () => resolve(dialog.returnValue === "ok"), { once: true },
    );
    dialog.showModal();
  });
}

/* B: re-save the user's text over a version that moved underneath them.
 *
 * Two round trips on purpose. The stale `body_hash` is the reason the save was
 * refused, so a fresh one must come from the server before the PUT — and the
 * GET is also what lets the confirm name what is about to be replaced. Sending
 * the user's body with the NEW hash is the whole operation; refreshing the hash
 * without re-sending the body would silently save nothing while reporting
 * success.
 */
async function overwriteOnDisk() {
  const note = state.note;
  /* `state.draftBody` is read at PUT TIME, deliberately, and NOT captured here.
     Between this click and the modal appearing there is a real network round
     trip with no dialog up — the textarea is live and the user can keep typing.
     Capturing the body at click time sent the PRE-GET text and reported
     "Saved", destroying every keystroke made during the GET.
     That is this fix reproducing, at a smaller scale, the exact defect it was
     written to close: an interface reporting success while discarding work.
     The HASH is different and must stay captured from the GET — a fresh body
     with a stale hash is the 409 all over again. */
  let fresh;
  try {
    /* Carries `session_id` like `openNote` — without it this read is
       unattributed in telemetry, and it is a read the user genuinely made. */
    const query = state.sessionId
      ? `?session_id=${encodeURIComponent(state.sessionId)}` : "";
    fresh = await api(`/api/notes/${encodeURIComponent(note.id)}${query}`);
  } catch (error) {
    toast(error.message, "error");
    return;
  }

  const where = fresh.vault_path || note.vault_path || "the stored copy";
  const { dialog } = configureConfirm({
    title: "Replace the version on disk?",
    /* Names the file, and says what is lost rather than asking "are you sure?".
       The other version is not junk — it is usually the watcher or brain-mcp
       having written something real — so the wording does not imply it is. */
    text: `"${note.title}" changed on disk at ${where} after you started `
      + `editing. Saving now replaces that version with the text in your `
      + `editor. The changes made on disk will be lost.`,
    okLabel: "Replace on disk",
    /* NO typed-title gate, unlike delete — and the asymmetry is deliberate.
       The same friction has opposite effects depending on what the user does
       when they give up. For permanent deletion, giving up means not deleting,
       so friction is pure benefit. Here, giving up means reopening the note —
       which is exactly the destructive default this whole change removed. A
       gate would push users off the safe path onto the unsafe one. */
    gateOnTitle: false,
  });

  dialog.addEventListener("close", async () => {
    if (dialog.returnValue !== "ok") return;
    try {
      /* Read HERE, not at click time — see the note at the top of this
         function. Anything typed while the GET was in flight is included. */
      const mine = state.draftBody;
      const result = await api(`/api/notes/${encodeURIComponent(note.id)}`, {
        method: "PUT",
        body: { body_hash: fresh.body_hash, body: mine },
      });
      note.body = mine;
      note.body_hash = result.body_hash;
      note.html = result.html;
      dispatch({ saveStatus: "saved", editing: false });
      toast("Saved — the version on disk was replaced");
    } catch (error) {
      setSaveStatus("conflict");
      toast(error.message, "error");
    }
  }, { once: true });
  dialog.showModal();
}

/* Offer the way out, WITHOUT a dispatch — the user is still holding text and a
   rebuild would take their caret. Injected next to the save indicator rather
   than rendered by renderInspector for exactly that reason. */
function showConflictAction() {
  const bar = document.querySelector(".note-bar");
  if (!bar || bar.querySelector(".conflict-action")) return;
  const button = el("button", "ghost-btn conflict-action", "⟳ Replace on disk");
  button.type = "button";
  button.addEventListener("click", overwriteOnDisk);
  bar.appendChild(button);
}

function deleteNote() {
  const note = state.note;
  const { dialog, input, ok } = configureConfirm({
    title: "Delete note",
    text: `This permanently deletes "${note.title}", its chunks, and its vault file.`,
    okLabel: "Delete",
    gateOnTitle: true,
  });

  /* The confirm button stays disabled until the typed title matches. The
     server checks it again — this is the convenience layer, not the control. */
  const onInput = () => { ok.disabled = input.value !== note.title; };
  input.addEventListener("input", onInput);

  dialog.addEventListener("close", async () => {
    input.removeEventListener("input", onInput);
    if (dialog.returnValue !== "ok") return;
    try {
      await api(`/api/notes/${encodeURIComponent(note.id)}`, {
        method: "DELETE",
        body: { confirm: true, expected_title: input.value },
      });
      dispatch({ note: null, selectedId: null });
      loadTree();
      toast("Deleted");
    } catch (error) { toast(error.message, "error"); }
  }, { once: true });

  dialog.showModal();
  input.focus();
}
