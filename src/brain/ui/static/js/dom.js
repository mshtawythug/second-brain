/* The three DOM primitives every render module uses.
 *
 * NOT in the spec's §2 table, which lists seven js modules. `$`, `el` and
 * `toast` are used by tree.js, results.js, inspector.js and main.js alike, so
 * the alternative was to park them in store.js — a module whose entire job is
 * "state and URL sync" and which would then also own element construction. One
 * reason to change per module is the inherited rule; this is the file that
 * keeps store.js honest.
 */

export const $ = (id) => document.getElementById(id);

export const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;   // textContent, never innerHTML
  return node;
};

export function toast(message, kind) {
  const node = $("toast");
  node.textContent = message;
  node.dataset.kind = kind || "info";
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, 3600);
}
