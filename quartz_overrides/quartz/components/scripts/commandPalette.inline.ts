// Brain command palette runtime (Lane C, Cmd/Ctrl-K).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/components/scripts/commandPalette.inline.ts`
// by `brain vault render --overlay`. It does NOT compile or run from
// the brain repo itself — esbuild-loader inside the cloned Quartz
// workspace bundles it into a string that `CommandPalette.tsx`
// exports as `afterDOMLoaded`. The string is then injected into
// every page at `</body>` time by Quartz's `renderPage.tsx`.
//
// What this script does, and why: nothing in stock Quartz exposes a
// Cmd/Ctrl-K palette. We render the palette markup once globally
// (registered in `quartz.layout.ts` `afterBody` slot via the
// `CommandPalette` component), then this script owns the open/close
// lifecycle, fuzzy search against `contentIndex.json`, keyboard
// navigation, and SPA-nav cleanup.
//
// brain (Lane C audit fix 2026-05-02): refactored to native
// `<dialog>` element. We call `dialog.showModal()` to open and
// `dialog.close()` to dismiss. Native dialog gives us:
//   * Focus trap inside the modal — Tab/Shift-Tab cycle within
//     focusable descendants, no manual ring management needed.
//   * Native `Escape` close behavior — fires a `cancel` event we
//     intercept with `preventDefault()` so we can run the close
//     animation before the dialog actually closes.
//   * Automatic focus restoration to the previously-focused element
//     on `dialog.close()` (browser-managed, no manual stash).
//   * `::backdrop` pseudo-element for the scrim (styled in
//     `_cmdk.scss`) — replaces the manual `<div class="brain-cmdk-backdrop">`.
//
// brain (Lane C audit fix 2026-05-02): SPA-mode aware navigation.
// `window.location.assign` defeats Quartz's micromorph SPA — every
// Cmd-K result selection blew the page cache and incurred a full
// paint. `spa.inline.ts` exposes `window.spaNavigate(url, isBack?)`
// at line 146; we route through it when present and only fall back
// to `window.location.assign` when SPA mode is disabled.
//
// brain (Lane C audit fix 2026-05-02): combobox + activedescendant
// a11y. The input carries `role="combobox"` (set in
// `CommandPalette.tsx`); we toggle `aria-expanded` on open/close,
// and stamp `aria-activedescendant` on the input pointing at the
// currently-selected option's id (`brain-cmdk-result-${idx}`) so
// screen readers announce each arrow-key result without DOM focus
// leaving the input.
//
// Contracts the script depends on:
//
//   * `window.fetchData` — a `Promise<Record<FullSlug, ContentDetails>>`
//     defined inline in `renderPage.tsx`'s `<head>` block. Stock
//     Quartz exposes it for the search component; we piggyback so we
//     don't fetch the index twice.
//   * `window.spaNavigate(url, isBack?)` — set by `spa.inline.ts`
//     when `enableSPA: true` in `quartz.config.ts`. Falls back to a
//     full-page navigation when absent.
//   * `<dialog id="brain-cmdk-root">` — the dialog element rendered
//     by `CommandPalette.tsx`. Closed by default.
//   * `<input class="brain-cmdk-input" role="combobox">` — the
//     search input.
//   * `<ul class="brain-cmdk-results" role="listbox">` — populated
//     with `<li class="brain-cmdk-result" role="option">` rows.
//   * `<div class="brain-cmdk-empty">` — shown when the result set
//     is empty for a non-empty query.
//
// Keyboard contract:
//   * `Cmd+K` (macOS) / `Ctrl+K` (Win/Linux) — open the palette
//     (always; works from any page).
//   * `Esc` — close the palette (handled natively via the `cancel`
//     event so the close animation still runs).
//   * `↑` / `↓` — move selection up/down through the result list.
//   * `Enter` — navigate to the selected result via `spaNavigate`
//     when available, else full reload.
//   * Click on a result — same navigation.
//   * Click on the dialog element itself (not its modal child) —
//     close. Native `<dialog>` reports backdrop clicks as
//     `event.target === dialog`.
//
// Browser support: `<dialog>` element has been baseline since 2022
// (Chrome 37, Firefox 98, Safari 15.4). All other features used
// here (`navigator.clipboard`-style globals, optional chaining, the
// `nav` event from spa.inline.ts) match stock Quartz's minimum.

// brain: tell TypeScript about the global Quartz wires
// `renderPage.tsx` and `spa.inline.ts` install. We don't import them
// because that creates a real ES module dependency; the inline
// script runs as `<script type="module">` and these are runtime
// globals, not module exports.
declare global {
  // brain: defined inline by Quartz's renderPage.tsx as
  // `const fetchData = fetch("./static/contentIndex.json").then(d => d.json())`.
  // The exact shape (object keyed by slug) matches stock + brain
  // contentIndex emitter output. We treat it as a plain `Record`
  // because we only read `title` / `tags`.
  // eslint-disable-next-line no-var
  var fetchData: Promise<Record<string, ContentEntry>> | undefined

  interface Window {
    addCleanup?: (fn: () => void) => void
    // brain (Lane C audit fix): set by `spa.inline.ts` line 146 when
    // `enableSPA: true`. Signature mirrors the upstream definition
    // (see `spa.inline.ts` `async function navigate(url, isBack)`).
    spaNavigate?: (url: URL, isBack?: boolean) => Promise<void>
  }
}

// brain: minimal subset of `ContentDetails` we read. Keeping the type
// narrow makes the script independent of upstream content-index
// changes — if a future Quartz adds new fields, we don't care.
interface ContentEntry {
  title?: string
  tags?: string[]
  content?: string
  // brain: the brain content-index emitter adds these via
  // `BrainContentDetails` (in `quartz/plugins/emitters/contentIndex.ts`).
  // Used to render the kind icon next to each result.
  tier?: string
  source?: string
}

interface Result {
  slug: string
  title: string
  tags: string[]
  score: number
}

const ROOT_SELECTOR = "#brain-cmdk-root"
const INPUT_SELECTOR = ".brain-cmdk-input"
const RESULTS_SELECTOR = ".brain-cmdk-results"
const EMPTY_SELECTOR = ".brain-cmdk-empty"

// brain: prefix for each result `<li>`'s id. The `aria-activedescendant`
// on the input points at one of these per-keystroke. Single source
// of truth so renderResults + setSelected can't drift apart.
const RESULT_ID_PREFIX = "brain-cmdk-result-"

const MAX_RESULTS = 12

// brain: matches `--motion-mid` in `_tokens.scss`. Used to delay the
// actual `dialog.close()` after we strip `.is-open`, so the exit
// animation can run before the browser tears down the modal. If
// `--motion-mid` ever changes in `_tokens.scss`, update this in lock
// step (the value is documented next to the call site below).
const CLOSE_ANIMATION_MS = 200

// brain: cleanup registry — same convention as Quartz's other inline
// scripts. `window.addCleanup` is wired by `spa.inline.ts` and
// drains every cleanup before each SPA navigation, preventing
// listener leaks on long sessions.
function registerCleanup(fn: () => void): void {
  if (typeof window.addCleanup === "function") {
    window.addCleanup(fn)
  }
}

// ---------------------------------------------------------------------------
// Fuzzy match — small, deterministic, no dependency.
// ---------------------------------------------------------------------------
//
// We match the query against the title (heaviest weight), then tags
// (medium weight), then a slug fallback (lightest weight). The score
// is a simple substring + position heuristic — fast enough for the
// ~1k-doc personal corpus and easy to reason about.
//
// brain: NOT a full Levenshtein/BM25 — for personal-corpus scale the
// substring approach is plenty and avoids pulling in flexsearch (which
// stock Quartz already loads for the search component, but this
// script runs in `afterBody` and importing it cleanly here would
// double-load the bundle).

function scoreMatch(query: string, text: string, weight: number): number {
  if (text.length === 0) return 0
  const idx = text.toLowerCase().indexOf(query)
  if (idx === -1) return 0
  // brain: position bonus — earlier matches score higher (matching
  // the start of the title beats matching mid-string).
  const positionBonus = Math.max(0, 10 - idx) * 0.1
  // brain: length bonus — closer match-length to text-length means
  // the match consumed more of the string (1.0 for exact match).
  const lengthBonus = query.length / text.length
  return weight + positionBonus + lengthBonus
}

function fuzzy(
  query: string,
  data: Record<string, ContentEntry>,
): Result[] {
  const q = query.trim().toLowerCase()
  if (q.length === 0) return []

  const out: Result[] = []
  for (const slug in data) {
    const entry = data[slug]
    const title = entry.title ?? slug
    const tags = entry.tags ?? []

    let score = 0
    score += scoreMatch(q, title, 3.0)
    for (const tag of tags) {
      score += scoreMatch(q, tag, 1.5) * 0.5 // dilute multi-tag matches
    }
    score += scoreMatch(q, slug, 0.8)

    if (score > 0) {
      out.push({ slug, title, tags, score })
    }
  }

  // Highest score first; tie-break on title for deterministic order.
  out.sort((a, b) => b.score - a.score || a.title.localeCompare(b.title))
  return out.slice(0, MAX_RESULTS)
}

// ---------------------------------------------------------------------------
// Render results into the listbox
// ---------------------------------------------------------------------------

function kindIcon(slug: string): string {
  // Tag pages live under `tags/`. Ingested artifacts under `_ingested/`.
  // Everything else is a regular note. Use simple glyphs — the
  // CSS handles color/sizing.
  if (slug.startsWith("tags/")) return "#"
  if (slug.startsWith("_ingested/")) return "📄"
  if (slug.startsWith("daily/")) return "🗓"
  return "📝"
}

function renderResults(
  list: HTMLUListElement,
  empty: HTMLElement,
  input: HTMLInputElement,
  results: Result[],
): void {
  // brain: clear by replacing innerHTML — micromorph isn't relevant
  // here because the list owns its own subtree exclusively.
  list.innerHTML = ""

  // brain: any prior `aria-activedescendant` is stale — clear it
  // before the new render. setSelected() below will re-stamp on
  // the first item if there are results.
  input.removeAttribute("aria-activedescendant")

  if (results.length === 0) {
    empty.removeAttribute("hidden")
    return
  }
  empty.setAttribute("hidden", "")

  results.forEach((result, idx) => {
    const li = document.createElement("li")
    li.className = "brain-cmdk-result"
    li.setAttribute("role", "option")
    li.setAttribute("data-slug", result.slug)
    li.setAttribute("data-index", String(idx))
    // brain (Lane C audit fix): stable id for `aria-activedescendant`.
    // The id MUST be unique on the page; the `RESULT_ID_PREFIX` plus
    // the index gives us that without random-id generation.
    li.id = RESULT_ID_PREFIX + idx
    li.setAttribute("aria-selected", idx === 0 ? "true" : "false")

    const icon = document.createElement("span")
    icon.className = "brain-cmdk-icon"
    icon.textContent = kindIcon(result.slug)
    li.appendChild(icon)

    const body = document.createElement("div")
    body.className = "brain-cmdk-body"

    const title = document.createElement("div")
    title.className = "brain-cmdk-title"
    title.textContent = result.title
    body.appendChild(title)

    const slug = document.createElement("div")
    slug.className = "brain-cmdk-slug"
    slug.textContent = result.slug
    body.appendChild(slug)

    li.appendChild(body)
    list.appendChild(li)
  })

  // brain (Lane C audit fix): point the input's
  // `aria-activedescendant` at the freshly-rendered first result.
  // Without this, screen readers wouldn't announce the default
  // selection (the visible `aria-selected` indicator alone isn't
  // enough — combobox semantics require the link from the input).
  input.setAttribute("aria-activedescendant", RESULT_ID_PREFIX + "0")
}

// ---------------------------------------------------------------------------
// Selection state
// ---------------------------------------------------------------------------

function currentSelectedIndex(list: HTMLUListElement): number {
  const items = list.querySelectorAll<HTMLLIElement>(".brain-cmdk-result")
  for (let i = 0; i < items.length; i++) {
    if (items[i].getAttribute("aria-selected") === "true") return i
  }
  return -1
}

function setSelected(
  list: HTMLUListElement,
  input: HTMLInputElement,
  target: number,
): void {
  const items = list.querySelectorAll<HTMLLIElement>(".brain-cmdk-result")
  if (items.length === 0) {
    input.removeAttribute("aria-activedescendant")
    return
  }
  // brain: clamp to range and wrap — wrapping makes ↑ from the top
  // jump to bottom and vice versa, which matches Cmd-K conventions
  // (Reflect, Linear, Spotlight all wrap).
  const clamped = ((target % items.length) + items.length) % items.length
  items.forEach((item, i) => {
    item.setAttribute("aria-selected", i === clamped ? "true" : "false")
    if (i === clamped) {
      item.scrollIntoView({ block: "nearest" })
    }
  })
  // brain (Lane C audit fix): re-point the input's
  // `aria-activedescendant` at the newly-selected option's id so
  // AT announces it on each arrow press.
  input.setAttribute("aria-activedescendant", RESULT_ID_PREFIX + clamped)
}

// ---------------------------------------------------------------------------
// Open / close (native dialog)
// ---------------------------------------------------------------------------

function openPalette(dialog: HTMLDialogElement, input: HTMLInputElement): void {
  // brain (Lane D audit fix 2026-05-02): the previous order added
  // `.is-open` BEFORE `showModal()`, intending the class to drive
  // the entrance animation. The Lane C reviewer flagged — and
  // browser testing confirmed — that this order BREAKS the
  // entrance:
  //
  //   * Before `showModal()`, the dialog is `display: none` (native
  //     `<dialog>` default without the `[open]` attribute). Setting
  //     `.is-open` on a `display:none` element doesn't trigger any
  //     transition because the element isn't rendered.
  //   * `showModal()` flips the dialog to its open state in a
  //     single synchronous paint. Because `.is-open` is already
  //     set, the modal child's computed style at first paint is
  //     `transform: scale(1); opacity: 1` — the FINAL state. There
  //     is no `from` keyframe state to interpolate from, so the
  //     transition has no animation to run.
  //   * Result: the modal pops in instantly with no scale-up.
  //
  // Correct order:
  //   1. `showModal()` first — dialog flips to `display: flex` with
  //      `.is-open` ABSENT, so the modal child paints at its
  //      resting state (`transform: scale(0.96); opacity: 0` from
  //      `_cmdk.scss` line 152-154). This is the from-state of the
  //      entrance.
  //   2. `requestAnimationFrame` — wait one frame so the resting-
  //      state paint commits to the screen before the next mutation.
  //      Without the rAF, modern browsers would batch both DOM
  //      changes into a single paint and the transition would still
  //      have nothing to interpolate.
  //   3. Add `.is-open` on the next frame — the class flip retargets
  //      the modal child's transform/opacity to (scale(1)/opacity(1));
  //      the browser sees the property change and runs the
  //      transition (250ms scale + opacity per `--motion-mid`).
  //   4. Sync `aria-expanded="true"` + clear input + focus it.
  //      ARIA + focus changes don't need to wait for the animation;
  //      AT consumers and keyboard users want immediate feedback.
  //
  // The `_cmdk.scss` rules are unchanged — the resting state lives
  // on `.brain-cmdk-modal` directly, the open state lives on
  // `#brain-cmdk-root.is-open .brain-cmdk-modal`. The fix is purely
  // in the script-side mutation order.
  //
  // The dialog's native `cancel` event still fires on Esc and runs
  // the close path (`closePalette`) unchanged — listener wired at
  // attach time in `setupPalette`, independent of this entrance
  // order rework. Close-path mutation order is in `closePalette`
  // below.
  if (typeof dialog.showModal === "function") {
    dialog.showModal()
  } else {
    // brain: defensive fallback for the (vanishingly rare) browser
    // that lacks `<dialog>` support — show the element directly so
    // the script doesn't crash. Focus trap + backdrop won't work,
    // but the palette itself remains usable.
    dialog.setAttribute("open", "")
  }
  // brain (Lane D fix): rAF before adding `.is-open` so the resting-
  // state paint commits before the class flip retargets the
  // animation. See block comment above.
  requestAnimationFrame(() => {
    dialog.classList.add("is-open")
  })
  input.setAttribute("aria-expanded", "true")
  input.value = ""
  input.focus()
}

function closePalette(dialog: HTMLDialogElement, input: HTMLInputElement): void {
  // brain (Lane C audit fix): mirror sequence to openPalette.
  //   1. Strip `.is-open` to start the exit animation.
  //   2. Wait one rAF + the close-animation duration (200ms; matches
  //      `--motion-mid` minus rAF latency) before calling `close()`.
  //      Without the delay the dialog vanishes mid-animation.
  //   3. `dialog.close()` natively restores focus to the
  //      previously-focused element (browser-managed) — no manual
  //      stash/restore needed.
  //   4. Sync `aria-expanded="false"` immediately so AT announces
  //      the collapse before the visible animation finishes.
  if (!dialog.classList.contains("is-open")) return
  dialog.classList.remove("is-open")
  input.setAttribute("aria-expanded", "false")
  requestAnimationFrame(() => {
    window.setTimeout(() => {
      if (typeof dialog.close === "function") {
        dialog.close()
      } else {
        dialog.removeAttribute("open")
      }
    }, CLOSE_ANIMATION_MS)
  })
}

function navigateToSlug(slug: string): void {
  // brain (Lane C audit fix 2026-05-02): SPA-mode aware navigation.
  // `window.location.assign` triggers a full page reload — defeats
  // Quartz's enableSPA + micromorph and causes a flash on every
  // selection. `spa.inline.ts:146` exposes `window.spaNavigate`
  // when SPA mode is enabled; route through it when present.
  // Falls back to `location.assign` so the palette still works on
  // a hypothetical SPA-disabled build.
  //
  // brain: build a proper `URL` object from the slug + the page
  // origin. spaNavigate expects a real URL (not a string); using
  // the constructor handles slug normalization for free (it strips
  // accidental double-slashes, encodes path segments, etc.).
  const url = new URL("/" + slug, window.location.origin)
  if (typeof window.spaNavigate === "function") {
    void window.spaNavigate(url)
  } else {
    window.location.assign(url)
  }
}

// ---------------------------------------------------------------------------
// Wire-up
// ---------------------------------------------------------------------------

async function setupPalette(): Promise<void> {
  const dialog = document.querySelector<HTMLDialogElement>(ROOT_SELECTOR)
  if (dialog === null) return // no palette markup on this page (e.g. 404)

  const input = dialog.querySelector<HTMLInputElement>(INPUT_SELECTOR)
  const list = dialog.querySelector<HTMLUListElement>(RESULTS_SELECTOR)
  const empty = dialog.querySelector<HTMLElement>(EMPTY_SELECTOR)
  if (input === null || list === null || empty === null) return

  // brain: wait for the global content index. `window.fetchData` is
  // installed by `renderPage.tsx`'s prescript; if absent for any
  // reason (e.g. an experimental layout disables it), bail
  // gracefully without breaking the page.
  if (typeof window.fetchData === "undefined") return
  const data = await window.fetchData

  // ---- Cmd-K toggle (page-level keyboard listener) ------------------
  const onKeyDown = (event: KeyboardEvent): void => {
    const isOpen = dialog.open
    const isCmdK =
      (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k"

    if (isCmdK) {
      event.preventDefault()
      if (isOpen) {
        closePalette(dialog, input)
      } else {
        openPalette(dialog, input)
      }
      return
    }

    // brain (Lane C audit fix): drop manual Esc handling — native
    // `<dialog>` fires a `cancel` event on Escape that we hook
    // below. Manual Esc here would race with the cancel handler.

    if (!isOpen) return

    if (event.key === "ArrowDown") {
      event.preventDefault()
      setSelected(list, input, currentSelectedIndex(list) + 1)
      return
    }
    if (event.key === "ArrowUp") {
      event.preventDefault()
      setSelected(list, input, currentSelectedIndex(list) - 1)
      return
    }
    if (event.key === "Enter") {
      event.preventDefault()
      const items = list.querySelectorAll<HTMLLIElement>(".brain-cmdk-result")
      const idx = currentSelectedIndex(list)
      const item = idx >= 0 ? items[idx] : null
      if (item !== null && item !== undefined) {
        const slug = item.getAttribute("data-slug")
        if (slug !== null) {
          closePalette(dialog, input)
          navigateToSlug(slug)
        }
      }
      return
    }
  }

  // ---- Native `cancel` event (Esc + browser-driven close) ----------
  // brain (Lane C audit fix): native dialog fires `cancel` on Esc.
  // We `preventDefault()` so the browser doesn't immediately close
  // the dialog (which would skip our exit animation), then run the
  // brain close path which animates and then calls `close()` itself.
  const onCancel = (event: Event): void => {
    event.preventDefault()
    closePalette(dialog, input)
  }

  // ---- Input → search ---------------------------------------------
  const onInput = (): void => {
    const results = fuzzy(input.value, data)
    renderResults(list, empty, input, results)
  }

  // ---- Click on a result ------------------------------------------
  const onListClick = (event: MouseEvent): void => {
    const target = event.target as HTMLElement | null
    if (target === null) return
    const item = target.closest<HTMLLIElement>(".brain-cmdk-result")
    if (item === null) return
    const slug = item.getAttribute("data-slug")
    if (slug === null) return
    closePalette(dialog, input)
    navigateToSlug(slug)
  }

  // ---- Backdrop dismiss -------------------------------------------
  // brain (Lane C audit fix): native dialogs report backdrop clicks
  // as `event.target === dialog` (clicks on dialog descendants
  // bubble with `target` set to the actual descendant). This is the
  // canonical way to dismiss-on-backdrop with `<dialog>`.
  const onDialogClick = (event: MouseEvent): void => {
    if (event.target === dialog) {
      closePalette(dialog, input)
    }
  }

  document.addEventListener("keydown", onKeyDown)
  dialog.addEventListener("cancel", onCancel)
  input.addEventListener("input", onInput)
  list.addEventListener("click", onListClick)
  dialog.addEventListener("click", onDialogClick)

  registerCleanup(() => {
    document.removeEventListener("keydown", onKeyDown)
    dialog.removeEventListener("cancel", onCancel)
    input.removeEventListener("input", onInput)
    list.removeEventListener("click", onListClick)
    dialog.removeEventListener("click", onDialogClick)
  })
}

// brain: same registration pattern as `linkSourceTag.js` — run on
// first load + on every SPA `nav` event so the listeners get
// re-attached after micromorph swaps the DOM. Without the nav
// re-registration the palette's input would stop responding after
// the first internal-link click.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    void setupPalette()
  })
} else {
  void setupPalette()
}

document.addEventListener("nav", () => {
  void setupPalette()
})
