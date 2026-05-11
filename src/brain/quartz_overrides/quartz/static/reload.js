// Brain blue-green serve — client-side reload watcher.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/static/reload.js` by `brain vault render
// --overlay`, and is copied verbatim into every build's `static/`
// dir by Quartz's stock `Plugin.Static()` emitter (no extra wiring
// needed — Quartz already mirrors `quartz/static/` 1:1 into the
// build output). The script is referenced from page `<head>` only
// when the brain `Plugin.ReloadSignal` transformer's
// `externalResources()` hook decides to inject it.
//
// Tested against Quartz v4.5.x (April 2026). The file is plain
// vanilla JS (no transpile, no bundler) so it survives any future
// Quartz refactor of its plugin pipeline — the only contract is
// that `Plugin.Static()` keeps copying `quartz/static/*` into
// `<build>/static/`. If a future Quartz version changes the static
// asset pipeline, update both this file's load path and the
// `reloadSignal.ts` transformer in lock-step.
//
// What this script does, and why: in the blue-green serve
// architecture (see
// `docs/plans/2026-05-01-quartz-blue-green-serve.md`), Caddy
// serves whichever build dir `<vault>/.quartz/current` symlinks
// to. The brain build watcher rebuilds the site into a fresh
// `<vault>/.quartz/builds/<id>/` dir on every vault change, then
// atomically retargets `current` at the new build. Because the
// swap happens server-side (rename(2) on a symlink), open
// browsers have no signal that the page they're viewing is now
// stale. Quartz's own `--serve` mode opens a WebSocket from the
// dev server to reload tabs in place, but `--serve` is dead in
// our flow (we serve via Caddy, not Quartz) — so this polling
// loop is the replacement reload mechanism.
//
// Mechanism: every 1s, fetch `/.build-id` (a one-line text file
// `brain.wiki.build_swap` writes to each build dir) as a conditional
// request after the first successful response. Caddy serves the file
// with an ETag; this client reuses that ETag via `If-None-Match` so
// unchanged builds return `304 Not Modified` without a response body.
// The first 200 response is captured as the baseline; any subsequent
// 200 response with a different value triggers `location.reload()`.
// Network errors, non-2xx responses, and 304s are swallowed silently —
// a transient blip must NOT reset the baseline (that would cause
// spurious reloads on the next successful poll). Polling pauses while
// the tab is hidden (`document.visibilityState === "hidden"`) and
// resumes on `visibilitychange` to "visible" so backgrounded tabs
// don't burn battery / network.
//
// Env-var contract: the script tag itself is gated by
// `BRAIN_WIKI_RELOAD=1` at Quartz BUILD time (not at script
// runtime). When the env var is unset or any other value, the
// `Plugin.ReloadSignal` transformer returns `{}` from its
// `externalResources()` hook and no script tag is injected —
// the script file is still copied into `static/` by Quartz, but
// nothing references it. `bin/brain-up` (the dev daily-use
// path) sets the env to `"1"`; `brain vault render` (the prod
// one-shot path) leaves it unset, so prod builds ship without
// any polling chatter.
//
// Browser support: targets evergreen Chrome / Firefox / Safari.
// No polyfills, no IE, no transpile. Uses `fetch`, async/await,
// and `document.visibilityState` — all baseline since 2017.
;(function () {
  // Polling cadence in ms. 1s keeps edit-to-UI latency at ≤1s for
  // warm builds. Server load remains trivial: responses are
  // ETag-gated (304 No Body on unchanged builds), so
  // 10 tabs × 1s = ~10 req/s against Caddy, still negligible for
  // a UUID-sized static file.
  var INTERVAL_MS = 1000

  // Path to the per-build identifier. `brain.wiki.build_swap`
  // writes a one-line file at the build dir's root; Caddy's
  // file_server serves it directly because the build dir IS the
  // site root. Trailing newline is normal — strip it before
  // comparing.
  var BUILD_ID_PATH = "/.build-id"

  // Must match full-build ids from `brain.wiki.build_swap._generate_build_id()`
  // and fast-path ids from `quartz/cli/build_partial_handler.js`. Rejecting
  // non-empty garbage bodies (for example an HTML fallback) prevents a
  // bad 200 response from poisoning the ETag baseline.
  var BUILD_ID_PATTERN = /^(?:\d{8}-\d{6}-[0-9a-f]{6}|fastpath-\d+-[0-9a-f]{8})$/

  // Last ETag returned by Caddy for `/.build-id`. `null` means the
  // first request has not completed yet, so no conditional header can
  // be sent. This is independent from the baseline body value: the
  // ETag saves bandwidth on unchanged builds, while the body comparison
  // remains the correctness check that decides when to reload.
  var lastEtag = null

  // Baseline build id captured on the first successful poll.
  // `null` means "not yet known"; any response other than the
  // baseline triggers a reload. Once set, this is NEVER cleared
  // by a network failure — that would cause the next successful
  // poll to silently rebaseline against a possibly-newer build
  // and miss the swap.
  var baseline = null

  // Single shared interval handle so we can `clearInterval` on
  // visibilitychange. `null` means "not currently polling".
  var timer = null

  function buildHeaders() {
    if (lastEtag === null) return {}
    return { "If-None-Match": lastEtag }
  }

  async function poll() {
    try {
      var r = await fetch(BUILD_ID_PATH, {
        cache: "no-cache",
        headers: buildHeaders(),
      })
      if (r.status === 304) return
      if (!r.ok) return
      var etag = r.headers.get("ETag")
      var id = (await r.text()).trim()
      if (!BUILD_ID_PATTERN.test(id)) return
      if (etag) {
        lastEtag = etag
      }
      if (baseline === null) {
        baseline = id
        // One-time banner so the user can confirm the watcher is
        // wired up by opening DevTools. Subsequent polls are silent.
        // eslint-disable-next-line no-console
        console.log("[brain] reload watcher active (build = " + id + ")")
        return
      }
      if (id !== baseline) {
        location.reload()
      }
    } catch (_) {
      // Network blip / DNS hiccup / Caddy bouncing. Keep polling
      // — do NOT reset `baseline`, otherwise the next successful
      // poll would silently rebaseline and we'd miss the next
      // real swap.
    }
  }

  function start() {
    if (timer !== null) return
    timer = setInterval(poll, INTERVAL_MS)
    // Fire one immediately so the baseline is captured without
    // waiting for the first interval tick.
    poll()
  }

  function stop() {
    if (timer === null) return
    clearInterval(timer)
    timer = null
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      stop()
    } else {
      start()
    }
  })

  // Kick off immediately if the tab is foreground at script load.
  // (afterDOMReady scripts run after `</body>`, so the document
  // is parsed and we can read `visibilityState` safely.)
  if (document.visibilityState !== "hidden") {
    start()
  }
})()
