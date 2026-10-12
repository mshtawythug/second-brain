/* The DOM primitives every render module uses — `$`, `el`, `toast` — and the
 * one rule for where a block goes in `#inspector` (`placeInspectorBlock`).
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

/* THE ORDER OF THE BLOCKS AFTER THE NOTE IN `#inspector`, DEFINED ONCE.
 *
 * Three modules draw a block into `#inspector` after the note's own children —
 * marginalia.js, graph.js, related.js — and each re-renders when ITS OWN fetch
 * resolves. With a bare `host.appendChild` the order on screen was the order
 * the network happened to answer in: a late `/links` response re-appended the
 * marginalia below the graph. This list is the one source of truth for the
 * order, by the class each block carries, and `placeInspectorBlock` is the
 * only way a block enters the host.
 *
 * Every block still lands AFTER the note's own children (`.note-head`, then
 * the body/editor on the `1fr` track — see marginalia.js's header), because a
 * block is only ever inserted before ANOTHER BLOCK that ranks after it, or
 * appended; never before a child that is not in this list.
 */
const INSPECTOR_BLOCKS = ["marginalia", "local-graph", "related-rail"];

function blockRank(node) {
  return INSPECTOR_BLOCKS.findIndex((name) => node.classList.contains(name));
}

export function placeInspectorBlock(host, node) {
  const rank = blockRank(node);
  /* A block this list does not name has no place in the order, and guessing
     one is how the order stops being defined in one place. */
  if (rank < 0) throw new Error(`unknown inspector block: ${node.className}`);
  for (const child of host.children) {
    if (blockRank(child) > rank) {
      host.insertBefore(node, child);
      return;
    }
  }
  host.appendChild(node);
}
