/* The global keyboard map, and the typing guard that scopes single-key binds. */

import { $ } from "/static/js/dom.js";
import { dispatch, state } from "/static/js/store.js";
import { saveNote } from "/static/js/inspector.js";

export function wireKeys() {
  document.addEventListener("keydown", (event) => {
    const typing = event.target.closest("input, textarea, [contenteditable]");

    if ((event.metaKey || event.ctrlKey) && event.key === "k") {
      event.preventDefault(); $("q").focus(); $("q").select(); return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key === "s") {
      event.preventDefault(); if (state.editing) saveNote(); return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key === "e") {
      event.preventDefault();
      if (state.note && state.note.editable) dispatch({ editing: !state.editing });
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key === "b") {
      event.preventDefault(); $("tree").closest(".rail").classList.toggle("is-open"); return;
    }
    if (typing) return;                       /* single-key bindings only outside inputs */
    if (event.key === "/") { event.preventDefault(); $("q").focus(); }
    if (event.key === "Escape") { document.activeElement.blur(); }
  });
}
