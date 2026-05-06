// Brain command palette component (Lane C, Cmd/Ctrl-P).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/components/CommandPalette.tsx` by `brain
// vault render --overlay`. It does NOT compile or run from the brain
// repo itself; the imports below resolve against the dependencies
// Quartz pulls into the cloned workspace via `npm install`, not
// against any package brain ships.
//
// Tested against Quartz v4.5.x (April 2026). The component shape
// mirrors stock Quartz components — see `Graph.tsx` (also a
// brain-overlayed component) for the canonical pattern. If a future
// Quartz version restructures `QuartzComponent` / `QuartzComponentProps`,
// pull the latest reference component from
// https://github.com/jackyzha0/quartz/blob/v4/quartz/components/
// and re-apply the brain tweaks below.
//
// Strategy — full new component (not an upstream override): nothing
// in stock Quartz exposes a quick-open palette. We render the palette
// markup once per page (registered in `quartz.layout.ts` `afterBody`
// slot — global, ships with every page in the build), then a
// co-located inline script (`scripts/commandPalette.inline.ts`)
// owns the open/close lifecycle, source chips, fuzzy search, keyboard
// handling, and SPA-nav cleanup.
//
// brain (Lane C audit fix 2026-05-02): refactored to native
// `<dialog>` element. Native dialog gives us, for free:
//   * Focus trap inside the modal while open (no manual `Tab` ring
//     management needed).
//   * Native `Escape` close behavior + `cancel` event we can intercept
//     to keep the brain close-animation.
//   * Automatic focus restoration to the previously-focused element
//     on `dialog.close()`.
//   * Native `::backdrop` pseudo-element — replaces the manual
//     `.brain-cmdk-backdrop` div with a CSS-driven scrim that the
//     browser positions above the rest of the page (top of the
//     stacking context, no z-index gymnastics).
// All four are non-trivial to implement correctly by hand; using the
// platform primitive removes drift surface.
//
// Markup contract — must stay in sync with the selector list in
// `_command_palette.scss` (Lane C/P5.3 SCSS partial) and the DOM walk inside
// `commandPalette.inline.ts`. See those two files' top comments.
//
// brain (Lane C audit fix 2026-05-02): combobox semantics on the
// search input. Plain `<input aria-controls=...>` is an a11y no-op;
// the WAI-ARIA combobox pattern is `role="combobox"` +
// `aria-expanded` (toggled by the script) + `aria-haspopup="listbox"`
// + `aria-autocomplete="list"`. Pairs with `aria-activedescendant`
// (set per-keystroke by the script) so screen readers announce each
// arrow-key result.
//
// Responsibility (CLAUDE.md rule 8): this file owns the Preact
// component wrapper. Open/close + search logic lives in
// `scripts/commandPalette.inline.ts`; the visual rules live in
// `../styles/brain/_command_palette.scss`. Don't let them spill into each
// other.

import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"
// @ts-ignore — esbuild-loader resolves .ts to a bundled string; TS doesn't know.
import script from "./scripts/commandPalette.inline"
import { SOURCE_ICONS, SOURCE_CHIP_ORDER } from "../util/sourceIcons"

const CHIP_VALUES: ReadonlyArray<keyof typeof SOURCE_ICONS> = SOURCE_CHIP_ORDER

const CommandPalette: QuartzComponent = (_props: QuartzComponentProps) => {
  // brain: native `<dialog>` element. The script calls `showModal()`
  // to open and `close()` to dismiss; both manage focus + scroll-lock
  // + the `::backdrop` pseudo-element automatically. The `id` is the
  // selector hook used by `commandPalette.inline.ts` and `_command_palette.scss`
  // — don't rename without updating both.
  //
  // brain: `aria-label` on the dialog gives screen readers a name
  // when the dialog opens (replacing the now-unnecessary `role` /
  // `aria-modal` attributes — native dialog provides both).
  return (
    <dialog id="brain-cmdk-root" class="brain-cmdk-root" aria-label="Command palette">
      <div class="brain-cmdk-modal">
        <div class="brain-cmdk-chips" role="group" aria-label="Filter quick open by source">
          <button
            type="button"
            class="brain-cmdk-chip brain-cmdk-chip-all"
            data-brain-source="__all__"
            data-active="true"
            aria-pressed="true"
          >
            All
          </button>
          {CHIP_VALUES.map((value) => (
            <button
              type="button"
              class="brain-cmdk-chip"
              data-brain-source={value}
              data-active="true"
              aria-pressed="true"
            >
              <span class="brain-cmdk-chip-icon" aria-hidden="true">
                {SOURCE_ICONS[value]}
              </span>
              <span class="brain-cmdk-chip-label">{value}</span>
            </button>
          ))}
        </div>
        <input
          class="brain-cmdk-input"
          type="text"
          autocomplete="off"
          autocorrect="off"
          spellcheck={false}
          placeholder="Search the wiki…"
          // brain (Lane C audit fix): WAI-ARIA combobox pattern.
          //   * role="combobox" tells AT this is a search input that
          //     drives an associated popup.
          //   * aria-expanded mirrors visibility (set by the script
          //     when the dialog opens / closes).
          //   * aria-haspopup="listbox" + aria-autocomplete="list"
          //     describes the popup shape.
          //   * aria-controls links to the listbox by id.
          //   * aria-activedescendant (set per-keystroke by the
          //     script) points to the currently-selected option's id
          //     so AT can announce it on each arrow press without
          //     moving DOM focus off the input.
          role="combobox"
          aria-expanded="false"
          aria-haspopup="listbox"
          aria-controls="brain-cmdk-results"
          aria-autocomplete="list"
          aria-label="Search the wiki"
        />
        {/* brain: results list rendered by the inline script. The
            initial `<ul>` is empty; the script injects `<li
            class="brain-cmdk-result" id="brain-cmdk-result-N">` rows
            from the contentIndex match. Each `<li>` carries the
            stable id the input's `aria-activedescendant` points at. */}
        <ul
          class="brain-cmdk-results"
          id="brain-cmdk-results"
          role="listbox"
          aria-label="Search results"
        ></ul>
        <div class="brain-cmdk-empty" hidden>
          No results.
        </div>
        <div class="brain-cmdk-footer" aria-hidden="true">
          <kbd>↑</kbd>
          <kbd>↓</kbd>
          <span>navigate</span>
          <span>·</span>
          <kbd>↵</kbd>
          <span>open</span>
          <span>·</span>
          <kbd>esc</kbd>
          <span>close</span>
        </div>
      </div>
    </dialog>
  )
}

// brain: wire the inline script. esbuild-loader resolves the
// `.inline.ts` import to its compiled bundled string at build time;
// Quartz inlines the string into the page at `</body>` time when
// `afterDOMLoaded` is set.
CommandPalette.afterDOMLoaded = script

// brain: no component-css here — visuals live in the global
// `_command_palette.scss`, which is `@use`-imported via `custom.scss`. That
// ensures the styles ship with EVERY page (not just pages that mount
// the component), so first-paint of any page already has the modal's
// resting state (closed `<dialog>`) rendered without flashing.

export default (() => CommandPalette) satisfies QuartzComponentConstructor
