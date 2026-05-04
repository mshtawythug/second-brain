// Brain Search component — Phase 3.2 of the Wiki UX Overhaul.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/components/Search.tsx` by `brain vault
// render --overlay`, OVERWRITING the stock Quartz `Search.tsx` so the
// component barrel (`components/index.ts`) re-exports the brain
// override instead of upstream. It does NOT compile or run from the
// brain repo itself — esbuild-loader inside the cloned Quartz
// workspace bundles `./scripts/search.inline` into a string that this
// component publishes via `Search.afterDOMLoaded`. The string is then
// injected at `</body>` time by Quartz's `renderPage.tsx`.
//
// Why this override exists: stock Quartz renders search results as
// `<h3>title</h3><p>snippet</p>` and offers no source filter. The brain
// corpus mixes ingested transcripts (krisp / slack / gmail) with
// authored notes (manual / vault), so a search popover that doesn't
// surface "where did this come from" makes results visually
// indistinguishable. P3.2 adds:
//
//   * Source-icon prefix per result row — a one-glance visual key for
//     gmail / krisp / slack / manual / vault.
//   * Source filter chips above the input — toggle to constrain the
//     visible results. State persists in `localStorage` under
//     `brain.search.activeSources` so a user's preferred slice
//     survives SPA navigation and full reloads.
//   * Lazy preview pane — when a result is selected (hover / arrow),
//     fetch `static/contentBodies/<slug>.json` (P3.1's split body file)
//     and render the full content. P3.1 left only a 240-char snippet
//     in `contentIndex.json`; without lazy fetching the preview pane
//     would be permanently truncated.
//   * Date column — short ISO date or "Nd ago" for the past week.
//
// The component layer (this file) only ships the markup + class hooks.
// All dynamic behaviour — chip state, lazy fetching, fuzzy match — is
// owned by the inline script at `./scripts/search.inline.ts`.
//
// brain: source-icon mapping. Pinned here at module scope so the same
// glyph table renders both inline (`renderResultRow` constant
// references it) and in the SSR'd chip rail (the script reads it at
// boot via the embedded JSON below). Unknown sources fall back to the
// vault glyph — better than rendering an empty span.
//
// Coordination point: if a future ingest source is added (e.g. brain
// grows a `notion` extractor), append it BOTH here AND in
// `_search.scss`'s chip palette (via the chip data attribute) AND in
// `commandPalette.inline.ts`'s `kindIcon()` helper if Cmd-K is rebuilt
// to share the same icon vocabulary.
import { QuartzComponent, QuartzComponentConstructor, QuartzComponentProps } from "./types"
// @ts-ignore — esbuild-loader rewrites this to a bundled string.
import script from "./scripts/search.inline"
import { classNames } from "../util/lang"
import { i18n } from "../i18n"

export interface SearchOptions {
  enablePreview: boolean
}

const defaultOptions: SearchOptions = {
  enablePreview: true,
}

// brain: source-icon mapping — single source of truth for glyphs.
// Mirrored in the inline script via the JSON-encoded
// `data-brain-source-icons` attribute on the chip rail; the script
// parses it once at boot rather than re-importing this constant
// (the inline script runs as a `<script type="module">` against
// runtime globals, not a real ES import of this file).
const SOURCE_ICONS: Record<string, string> = {
  gmail: "📧",
  krisp: "🎙️",
  slack: "💬",
  manual: "✍️",
  vault: "🌱",
}

// brain: ordered list of chip values rendered above the search input.
// Order is deterministic and pinned (vs. derived from the index) so
// the chip rail looks identical even when the loaded corpus is missing
// one source. Matches the `chipVocabularies.source` order in
// `graph.inline.ts` so the two filter rails read as siblings.
const CHIP_VALUES: ReadonlyArray<keyof typeof SOURCE_ICONS> = [
  "krisp",
  "slack",
  "gmail",
  "manual",
  "vault",
] as const

export default ((userOpts?: Partial<SearchOptions>) => {
  const Search: QuartzComponent = ({ displayClass, cfg }: QuartzComponentProps) => {
    const opts = { ...defaultOptions, ...userOpts }
    const searchPlaceholder = i18n(cfg.locale).components.search.searchBarPlaceholder
    // brain: serialize the icon table once into a data attribute so the
    // inline script can rebuild the chip glyphs without a duplicate
    // hard-coded copy. JSON.stringify keeps the table escapable through
    // the HTML attribute boundary.
    const iconsJson = JSON.stringify(SOURCE_ICONS)
    return (
      <div class={classNames(displayClass, "search")}>
        <button class="search-button">
          <svg role="img" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 19.9 19.7">
            <title>Search</title>
            <g class="search-path" fill="none">
              <path stroke-linecap="square" d="M18.5 18.3l-5.4-5.4" />
              <circle cx="8" cy="8" r="7" />
            </g>
          </svg>
          <p>{i18n(cfg.locale).components.search.title}</p>
        </button>
        <div class="search-container">
          <div class="search-space">
            {/* brain: chip rail above the input. The script attaches
                click handlers and reads the icon table from the
                `data-brain-source-icons` attribute on the rail's root.
                `data-active="true"` is reflected by the script when
                a chip is in the active set; CSS keys hover/focus
                styling off the same attribute. */}
            <div
              class="brain-search-chips"
              role="group"
              aria-label="Filter by source"
              data-brain-source-icons={iconsJson}
            >
              <button
                type="button"
                class="brain-search-chip brain-search-chip-all"
                data-brain-source="__all__"
                data-active="true"
              >
                All
              </button>
              {CHIP_VALUES.map((value) => (
                <button
                  type="button"
                  class="brain-search-chip"
                  data-brain-source={value}
                  data-active="true"
                >
                  <span class="brain-search-chip-icon" aria-hidden="true">
                    {SOURCE_ICONS[value]}
                  </span>
                  <span class="brain-search-chip-label">{value}</span>
                </button>
              ))}
            </div>
            <input
              autocomplete="off"
              class="search-bar"
              name="search"
              type="text"
              aria-label={searchPlaceholder}
              placeholder={searchPlaceholder}
            />
            <div class="search-layout" data-preview={opts.enablePreview}></div>
          </div>
        </div>
      </div>
    )
  }

  Search.afterDOMLoaded = script

  return Search
}) satisfies QuartzComponentConstructor
