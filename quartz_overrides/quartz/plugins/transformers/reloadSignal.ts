// Brain blue-green serve — reload-signal transformer plugin.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/plugins/transformers/reloadSignal.ts`
// by `brain vault render --overlay`. It does NOT compile or run
// from the brain repo itself; the imports below resolve against
// the dependencies Quartz pulls into the cloned workspace via
// `npm install`, not against any package brain ships.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz
// version restructures the transformer plugin shape or the
// `externalResources()` hook contract, pull the latest
// transformer reference from
// https://github.com/jackyzha0/quartz/tree/v4/quartz/plugins/transformers
// and re-apply the brain tweaks flagged below — `// brain:` for
// value/structural choices on upstream-supported fields, and
// `// brain-extension:` for keys/types that don't exist in stock
// Quartz.
//
// What this transformer does, and why: in the blue-green serve
// architecture (see
// `docs/plans/2026-05-01-quartz-blue-green-serve.md`), Caddy
// serves whichever build dir `<vault>/.quartz/current` symlinks
// to, and the brain build watcher atomically retargets that
// symlink at a fresh `<vault>/.quartz/builds/<id>/` after every
// rebuild. Stock Quartz's `--serve` mode opens a WebSocket from
// the dev server back to the page so it can push a reload — but
// `--serve` is dead in our flow (Caddy serves the static output
// directly; Quartz only builds), so the WebSocket-reload path is
// gone. This transformer injects a polling client
// (`quartz/static/reload.js`, also installed by the overlay) that
// fetches `/.build-id` every 3s and calls `location.reload()`
// when the value changes. The polling target is the per-build
// identifier file `brain.wiki.build_swap` writes at each build
// dir's root; Caddy serves it directly because the build dir IS
// the site root.
//
// brain-extension: env-var contract. The script is injected only
// when `process.env.BRAIN_WIKI_RELOAD === "1"` at BUILD time
// (not at runtime — the gate is read while Quartz is producing
// HTML, then baked into the page). Any other value (including
// unset) means `externalResources()` returns an empty object and
// no `<script>` tag goes out. `bin/brain-up` (the dev daily-use
// path) sets the env to `"1"`; `brain vault render` (the prod
// one-shot path) leaves it unset, so prod builds ship without
// any polling chatter. The reload.js asset itself is still
// copied into `static/` by Quartz's `Plugin.Static()` emitter
// either way — it's just unreferenced unless the gate flips.
//
// brain: cross-references for the full reload contract:
//   * `quartz_overrides/quartz/static/reload.js` — the polling
//     client this transformer points page `<head>` at.
//   * `src/brain/wiki/build_swap.py` — writes `<build>/.build-id`
//     after every successful build, then atomically retargets
//     `<vault>/.quartz/current` at the new build dir.
//   * `bin/brain-up` — sets `BRAIN_WIKI_RELOAD=1` for the build
//     watcher subprocess; spawns Caddy as the static file server
//     pointed at `<vault>/.quartz/current/`.
//   * Caddyfile — must serve `/.build-id` at root (default
//     behavior of `file_server` since the file lives at the
//     build dir's root).
//
// Registration: this transformer has NO ordering requirement
// among the other transformers — it doesn't touch the mdast
// tree. It only contributes a `<script>` tag via
// `externalResources()`. Place it anywhere in the
// `transformers: [...]` list; the brain `quartz.config.ts`
// template wires it AFTER `Plugin.DerivedFenceMark()` so the two
// brain-extension transformers stay grouped.

import { QuartzTransformerPlugin } from "../types"

// brain-extension: the path the page `<script src=...>` tag
// points at. Leading slash is intentional — Quartz emits pages
// at arbitrary slug depths (e.g. `/notes/foo/index.html`) and a
// relative `static/reload.js` would break for any non-root page.
// Server-rooted `/static/reload.js` resolves consistently
// regardless of which page the user landed on. This works
// because Caddy's `file_server` serves the build dir as the site
// root (`<vault>/.quartz/current/static/reload.js`); see
// `docs/plans/2026-05-01-quartz-blue-green-serve.md` for the
// Caddyfile shape.
const RELOAD_SCRIPT_SRC = "/static/reload.js"

// brain-extension: build-time env var that gates injection. Read
// inside `externalResources()` so each plugin instance evaluates
// the gate fresh per build (matters for tests + for `bin/brain-up`
// re-invocations where the env may differ between runs).
const RELOAD_ENV_VAR = "BRAIN_WIKI_RELOAD"

// brain-extension: the transformer plugin itself. Empty options
// shape — the only knobs would be the script path and the env
// var name, both of which are part of the contract documented
// above. Keeping the signature `(opts?: never)` documents that
// no configuration is expected; if a knob is genuinely needed
// later (e.g. a per-vault interval override), widen the type at
// that point rather than guessing now.
export const ReloadSignal: QuartzTransformerPlugin = (_opts?: never) => {
  return {
    name: "ReloadSignal",
    externalResources() {
      // brain: strict equality with `"1"` rather than truthiness
      // so accidental values like `"0"` / `"false"` / `"no"`
      // don't enable the script. Symmetric with the contract
      // documented in `bin/brain-up` (which sets exactly `"1"`).
      if (process.env[RELOAD_ENV_VAR] !== "1") {
        return {}
      }
      return {
        js: [
          {
            // brain: `afterDOMReady` so the script runs after
            // `</body>` (see Quartz's `renderPage.tsx` for the
            // dispatch). The poller doesn't touch the DOM at
            // load time, and afterDOMReady avoids blocking
            // initial paint on the network round-trip.
            loadTime: "afterDOMReady",
            // brain-extension: external script (URL src) rather
            // than inline. The reload.js file is bundled into
            // every build's `static/` dir by Quartz's stock
            // `Plugin.Static()` emitter; pointing at it by URL
            // keeps the script body out of every page's HTML.
            contentType: "external",
            src: RELOAD_SCRIPT_SRC,
          },
        ],
      }
    },
  }
}
