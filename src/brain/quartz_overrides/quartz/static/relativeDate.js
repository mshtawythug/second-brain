// Brain wiki — live relative-date client script (home-page Recent rail).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/static/relativeDate.js` by `brain vault
// render --overlay`, and is copied verbatim into every build's
// `static/` dir by Quartz's stock `Plugin.Static()` emitter (no extra
// wiring needed — Quartz already mirrors `quartz/static/` 1:1 into
// the build output). The script is referenced from page `<head>` by
// the brain `Plugin.RelativeDate` transformer's `externalResources()`
// hook (see `relativeDate.ts` for the inject side).
//
// Tested against Quartz v4.5.x (April 2026). Plain vanilla JS — no
// transpile, no bundler — so it survives any future Quartz refactor
// of its plugin pipeline. The only contracts are (a) Quartz keeps
// copying `quartz/static/*` into `<build>/static/`, and (b) the
// recent-rail markup keeps emitting `<span class="brain-rel-date"
// data-date="<ISO 8601>">…</span>` (see
// `brain.wiki.build_homepage._render_bullets`).
//
// What this script does, and why: the recent rail in `index.md` used to
// bake a relative string ("today", "3d ago") at BUILD time. That string
// decays — the home note isn't re-rendered daily, so a doc ingested 3
// days before a build still reads "3d ago" weeks later. The server now
// emits the ABSOLUTE date as `data-date` (the machine-readable source of
// truth) and a non-decaying "Jun 10"-style fallback as the span's text.
// This script recomputes the relative bucket client-side on every page
// load + SPA navigation, so the rail is always honest relative to the
// reader's current local date.
//
// BUCKET PARITY: this MUST mirror
// `brain.wiki.build_homepage._format_relative_date` exactly — same
// calendar-day delta (LOCAL time; compare local-midnight dates, not 24h
// windows) and the same four buckets:
//   * delta <= 0 (today or future) → "today"
//   * 1..6                         → "Nd ago"
//   * 7..34                        → "Nw ago" (delta // 7)
//   * >= 35                        → "Mon D"  (e.g. "Jun 10")
// If you change a bucket here, change it there too (and the parity test
// in `tests/test_brain_recent_homepage.py`).
//
// Idempotent + safe: a missing or unparseable `data-date` leaves the
// element's existing text (the absolute fallback) untouched, so the
// reader never sees a blank or "Invalid Date" cell. Re-runs on the
// Quartz `nav` SPA event so spans rendered into a new route get the live
// text too.
//
// Browser support: targets evergreen Chrome / Firefox / Safari. Uses
// `document.querySelectorAll`, `forEach`, `addEventListener`, and the
// `Date` API — all baseline since 2017.
;(function () {
  // brain: English month abbreviations, index 0 = January. Hard-coded
  // (NOT `toLocaleString`) so the rendered "Mon D" fallback matches
  // Python's `strftime("%b")` English output regardless of the browser
  // locale — avoids env drift between the server-baked fallback and the
  // client-recomputed >= 35-day bucket.
  var MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ]

  // brain: project a Date onto local midnight so the delta is a calendar-
  // day count (matching Python's `_to_date` → `astimezone().date()`),
  // not a raw elapsed-hours count. Without this, a doc ingested at 23:59
  // yesterday vs one at 00:01 today would mis-bucket near the boundary.
  function localMidnight(d) {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate())
  }

  // brain: compute the relative-date text for an ISO 8601 `data-date`.
  // Returns `null` when the value is missing or parses to `Invalid Date`
  // so the caller can leave the absolute fallback in place. Mirrors the
  // bucket order in `_format_relative_date`.
  function relativeText(iso) {
    if (typeof iso !== "string" || iso.length === 0) return null
    var when = new Date(iso)
    if (isNaN(when.getTime())) return null

    var nowMid = localMidnight(new Date())
    var whenMid = localMidnight(when)
    // Whole-day delta. `getTime()` is ms since epoch; dividing the local-
    // midnight difference by a day yields an integer calendar-day count.
    // Round to absorb any DST-induced sub-millisecond drift.
    var deltaDays = Math.round(
      (nowMid.getTime() - whenMid.getTime()) / 86400000,
    )

    if (deltaDays <= 0) return "today"
    if (deltaDays < 7) return deltaDays + "d ago"
    if (deltaDays < 35) return Math.floor(deltaDays / 7) + "w ago"
    // >= 35 days → absolute "Mon D" (no leading zero on the day), matching
    // the server's `_format_absolute_date`.
    return MONTHS[whenMid.getMonth()] + " " + whenMid.getDate()
  }

  // brain: rewrite every recent-rail span's text from its `data-date`.
  // Idempotent — recomputing from `data-date` (never from the existing
  // text) means repeated runs converge on the same value, and a bad
  // `data-date` is a no-op (the absolute fallback text stays put).
  function updateAll() {
    var nodes = document.querySelectorAll(".brain-rel-date[data-date]")
    nodes.forEach(function (el) {
      var text = relativeText(el.getAttribute("data-date"))
      if (text === null) return
      el.textContent = text
    })
  }

  // brain: run on first DOM ready. `DOMContentLoaded` fires before first
  // paint of below-fold content, so any flash of the absolute fallback is
  // bounded to above-fold spans — acceptable, and the fallback is a real
  // date either way.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", updateAll)
  } else {
    updateAll()
  }

  // brain: re-run on Quartz SPA navigation. `enableSPA: true` in
  // `quartz.config.ts` makes Quartz dispatch a `nav` event on `document`
  // after each in-page route change (see
  // `quartz/components/scripts/spa.inline.ts` upstream). Listening for it
  // keeps the relative dates live across the whole session without a full
  // page reload.
  document.addEventListener("nav", updateAll)
})()
