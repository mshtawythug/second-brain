// Brain wiki redesign — link source-tagger client script (Lane B).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/static/linkSourceTag.js` by `brain vault
// render --overlay`, and is copied verbatim into every build's
// `static/` dir by Quartz's stock `Plugin.Static()` emitter (no extra
// wiring needed — Quartz already mirrors `quartz/static/` 1:1 into
// the build output). The script is referenced from page `<head>` by
// the brain `Plugin.LinkSourceTag` transformer's `externalResources()`
// hook (see `linkSourceTag.ts` for the inject side).
//
// Tested against Quartz v4.5.x (April 2026). Plain vanilla JS — no
// transpile, no bundler — so it survives any future Quartz refactor
// of its plugin pipeline. The only contracts are (a) Quartz keeps
// copying `quartz/static/*` into `<build>/static/`, and (b) the
// brain redesign keeps the `data-brain-link-kind="ingested"` CSS
// attribute selector pattern.
//
// What this script does, and why: the `linkKindMark.ts` transformer
// stamps `data-brain-link-kind="ingested"` on every `<a>` whose URL
// starts with `_ingested/`, but it can't determine WHICH source
// (krisp/slack/gmail/manual) the target came from without a
// document-level lookup that's expensive at build time. The source
// is encoded in the URL path itself (`_ingested/<source>/<slug>`),
// so this 25-line client script extracts it once on `DOMContentLoaded`
// and stamps `data-brain-source="<source>"` on each ingested link.
// `_links.scss` reads that attribute via `&[data-brain-source="krisp"]`
// rules to pick the source-tinted left-rail color from the
// `--brain-source-*` token palette in `_tokens.scss`.
//
// Idempotent: if `data-brain-source` is already set (e.g. SPA
// navigation re-runs us, or a future build-time emitter pre-stamps
// it), we skip — first writer wins. Quartz's enableSPA mode
// dispatches `nav` events on each route change; the listener at the
// bottom re-runs the tagger so links rendered into the new page get
// tagged too.
//
// Browser support: targets evergreen Chrome / Firefox / Safari. Uses
// `document.querySelectorAll`, `forEach`, and `addEventListener` —
// all baseline since 2017.
;(function () {
  // brain: the four sources `brain ingest --source <name>` accepts.
  // Anything else falls back to "manual" via the `--brain-source-manual`
  // CSS variable default in `_links.scss`. Keeping the allowlist
  // explicit means we don't accidentally stamp `data-brain-source="."`
  // on a malformed URL.
  var KNOWN_SOURCES = ["krisp", "slack", "gmail", "manual"]

  // brain: the data-attribute the SCSS reads. Mirrors the constant in
  // `_links.scss`. If you rename one, rename both.
  var SOURCE_ATTR = "data-brain-source"

  // brain: extract the source segment from a `_ingested/<source>/...`
  // URL. Handles three forms the build may emit:
  //   * `_ingested/krisp/...`        (relative)
  //   * `/_ingested/krisp/...`       (absolute)
  //   * `./_ingested/krisp/...`      (explicit relative)
  // Returns the source string (lowercased, validated against the
  // allowlist) or `null` if extraction fails.
  function sourceFromHref(href) {
    if (typeof href !== "string") return null
    // Strip query strings + fragments before splitting on `/`.
    var clean = href.split("?")[0].split("#")[0]
    var match = clean.match(/(?:^|\/|\.\/)_ingested\/([^/]+)/)
    if (match === null) return null
    var source = match[1].toLowerCase()
    if (KNOWN_SOURCES.indexOf(source) === -1) return null
    return source
  }

  // brain: tag every ingested-link `<a>` in the document. Selector
  // covers both the relative and absolute href forms; the prefix
  // check below also covers the explicit `./` form. Idempotent —
  // skips elements that already carry `data-brain-source`.
  function tagAll() {
    var nodes = document.querySelectorAll(
      'a[href^="_ingested/"], a[href*="/_ingested/"], a[href^="./_ingested/"]',
    )
    nodes.forEach(function (a) {
      if (a.hasAttribute(SOURCE_ATTR)) return
      var source = sourceFromHref(a.getAttribute("href"))
      if (source === null) return
      a.setAttribute(SOURCE_ATTR, source)
    })
  }

  // brain: run on first DOM ready. `DOMContentLoaded` fires before
  // first paint of below-fold content, so any flash-of-unstyled-link
  // is bounded to above-fold links — acceptable per the redesign
  // plan's risk note.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", tagAll)
  } else {
    tagAll()
  }

  // brain: re-run on Quartz SPA navigation. `enableSPA: true` in
  // `quartz.config.ts` makes Quartz dispatch a `nav` event on
  // `document` after each in-page route change (see
  // `quartz/components/scripts/spa.inline.ts` upstream). Listening for
  // it keeps source tagging consistent across the whole session
  // without a full page reload.
  document.addEventListener("nav", tagAll)
})()
