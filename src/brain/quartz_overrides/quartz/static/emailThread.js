// Brain wiki — email-thread reading mode runtime (P4.4 of the Wiki UX
// Overhaul).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/static/emailThread.js` by `brain vault render
// --overlay`, and is copied verbatim into every build's `static/` dir
// by Quartz's stock `Plugin.Static()` emitter (no extra wiring needed —
// Quartz already mirrors `quartz/static/` 1:1 into the build output).
// The script is referenced from page `<head>` by the brain
// `Plugin.EmailThreadReader` transformer's `externalResources()` hook
// (see `emailThread.ts` for the inject side).
//
// Tested against Quartz v4.5.x (April 2026). Plain vanilla JS — no
// transpile, no bundler — so it survives any future Quartz refactor of
// its plugin pipeline. The only contracts are (a) Quartz keeps copying
// `quartz/static/*` into `<build>/static/`, and (b) `brain.ingest.gmail
// .to_extracted_thread` keeps emitting `## YYYY-MM-DD HH:MM — <from>`
// for the latest message and `<details><summary>YYYY-MM-DD HH:MM —
// <from></summary>...` for older messages, with `<from>` HTML-ESCAPED
// on BOTH (defect #57 — the H2 was raw, so the address rendered one way
// in the summary and another in the heading). The escape is invisible
// here: `textContent` decodes the entities, so `parseFromAddress` sees
// `Name <addr>` either way. If the markdown assembly shape changes,
// update `parseFromAddress` and the section walker together.
//
// What this script does, and why: an email_thread page (markdown body
// produced by `to_extracted_thread`) renders as zero or more `<details>`
// elements (older messages, collapsed by default) followed by a TRAILING
// `<h2>` — the most recent message, always visible.
//
// THE H2 IS LAST, NOT FIRST. This comment said "a leading `<h2>`" for a
// long time and it was never true: `to_extracted_thread` sorts ascending
// by `internalDate` and passes `collapsed=(idx != last_idx)`, so the one
// uncollapsed section is the LAST one. Verified by running the producer,
// and already pinned by an executable assertion —
// `tests/test_gmail_thread.py::test_most_recent_message_not_collapsed`
// asserts `last_h2 > last_details`, i.e. the plain H2 appears after the
// last `<details>` open tag. The mistake was harmless only by luck: the
// walker below scans to end-of-article rather than assuming a position.
// It had already propagated into a test fixture, which is how a comment
// like this stops being a comment and starts being a belief.
//
// The script enhances that shape with two affordances:
//
//   1. Annotates every message section with `data-brain-thread-from`
//      (the From address parsed from the heading) and
//      `data-brain-is-mine` ("true" iff the From contains the user's
//      email — read from `window.BRAIN_USER_EMAIL`, baked in at build
//      time by the EmailThreadReader transformer).
//   2. Renders a "Show only my replies" toggle button at the top of
//      `<article>`. State persists in localStorage under
//      `brain.email.repliesOnly` as a JSON boolean. When ON, the body
//      gets a `brain-replies-only` class which `_email_thread.scss`
//      uses to hide every `[data-brain-is-mine="false"]` section.
//
// Detection: only runs on email-thread pages. The detection heuristic
// is `/_ingested/gmail/` in the pathname AND the article contains
// either a `<details>` element or an `<h2>` whose text matches the
// "YYYY-MM-DD HH:MM — <from>" shape. Two-condition gate so a non-thread
// page that happens to live under `_ingested/gmail/` (unlikely, but
// defensive) doesn't get a stray button. Symmetric with how the
// explorer toggle (P4.2) gates itself.
//
// Idempotent: the wrapping logic and button render check for an
// existing wiring marker on `<article>` and skip if already done. Re-
// runs on Quartz SPA `nav` events so navigation between thread pages
// re-wires the new article.
//
// Browser support: targets evergreen Chrome / Firefox / Safari. Uses
// `document.querySelectorAll`, `dataset`, `classList`, and
// `addEventListener` — all baseline since 2017.
;(function () {
  // brain: localStorage key for the "Show only my replies" filter
  // state. Mirrors the `brain.<feature>.<setting>` namespace from
  // search.inline.ts (`brain.search.activeSources`) and explorer
  // .inline.ts (`brain.explorer.showIngested`) so brain prefs cluster
  // cleanly under one prefix in devtools.
  var REPLIES_ONLY_KEY = "brain.email.repliesOnly"

  // brain: data attributes the SCSS reads. Centralised here so a
  // future rename is a one-line change in JS + a search-replace in
  // _email_thread.scss.
  var FROM_ATTR = "data-brain-thread-from"
  var IS_MINE_ATTR = "data-brain-is-mine"
  var WIRED_ATTR = "data-brain-thread-wired"

  // brain: body class flipped by the toggle. SCSS keys off this to
  // hide non-user messages.
  var REPLIES_ONLY_CLASS = "brain-replies-only"

  // brain: section class added to wrapped latest-message + each
  // <details>. Gives the SCSS a single hook regardless of whether the
  // section is the trailing H2 (wrapped at runtime) or a markdown
  // <details>.
  var SECTION_CLASS = "brain-thread-message"
  var LATEST_CLASS = "brain-thread-latest"

  // brain: button class. Matches the spec verbatim — the static test
  // pins this literal so a future rename trips it.
  var TOGGLE_CLASS = "brain-email-replies-only-toggle"

  // brain: button labels. Factored out so the static test can pin
  // them and a future i18n pass has a single seam to retarget.
  var LABELS = {
    show: "Show only my replies",
    showingMine: "Show all messages",
  }

  // brain: heading separator between the date and the From address,
  // emitted by `to_extracted_thread`'s `_format_thread_section`. Pinned
  // here as a constant rather than an inline literal so a future
  // markdown-shape change has a single seam.
  var FROM_SEPARATOR = " — "

  // brain: extract the `From` address from a heading text like
  // `"2026-04-28 14:00 — Alice <alice@example.com>"`. Returns the
  // substring after the first ` — ` (em-dash padded with single
  // spaces — the exact form `to_extracted_thread` writes), or empty
  // string when the separator is missing. We don't try to crack the
  // address into name/local/domain — substring containment against
  // `BRAIN_USER_EMAIL` is enough to decide ownership and tolerates
  // both `Name <addr>` and bare-address forms.
  function parseFromAddress(text) {
    if (typeof text !== "string") return ""
    var idx = text.indexOf(FROM_SEPARATOR)
    if (idx < 0) return ""
    return text.slice(idx + FROM_SEPARATOR.length).trim()
  }

  // brain: heuristic match for "this heading text looks like a
  // per-message thread heading". Used for the email-thread page
  // gate so a stray H2 (`## Conclusion`) on an unrelated page can't
  // be mistaken for a thread message. The H2 must contain ` — ` AND
  // start with a YYYY-MM-DD date pattern.
  var THREAD_HEADING_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}\s+—\s+/

  // brain: case-insensitive substring match between the parsed `from`
  // and the user's email. We match on the email (not display name)
  // because Gmail headers are stable on the address and noisy on the
  // name (people change display names per client). Empty user email
  // → no match (button still renders; toggling hides everything,
  // which is the right "you forgot to set BRAIN_USER_EMAIL" cue).
  function fromMatchesUser(from, userEmail) {
    if (!from || !userEmail) return false
    return from.toLowerCase().indexOf(userEmail.toLowerCase()) !== -1
  }

  // brain: read the persisted toggle state. Default (no key, missing
  // localStorage, parse failure, non-boolean) is `false` so a fresh
  // visit shows every message — discoverable affordance over hidden-
  // by-default. Try/catch covers Safari private mode which can throw
  // on `localStorage.getItem`.
  function loadRepliesOnly() {
    if (typeof localStorage === "undefined") return false
    try {
      var raw = localStorage.getItem(REPLIES_ONLY_KEY)
      if (raw === null) return false
      var parsed = JSON.parse(raw)
      return typeof parsed === "boolean" ? parsed : false
    } catch (err) {
      return false
    }
  }

  // brain: persist the toggle state. Errors are swallowed —
  // localStorage may throw `QuotaExceededError` in private mode, and
  // a failed write should not tear down the article. Worst case the
  // user re-flips the toggle next session.
  function persistRepliesOnly(value) {
    if (typeof localStorage === "undefined") return
    try {
      localStorage.setItem(REPLIES_ONLY_KEY, JSON.stringify(value))
    } catch (err) {
      // intentional: see docstring
    }
  }

  // brain: get the user's email from the `<script>` global the build-
  // time transformer injected. Tolerates a missing global (script
  // injection failed, dev-only build skipped the transformer, etc.) —
  // returns "" rather than throwing.
  function getUserEmail() {
    var raw =
      typeof window !== "undefined" && typeof window.BRAIN_USER_EMAIL === "string"
        ? window.BRAIN_USER_EMAIL
        : ""
    return raw.trim()
  }

  // brain: detect an email-thread page. Two-part gate:
  //   1. URL contains `/_ingested/gmail/` (the brain mirror tier
  //      where thread pages live).
  //   2. The article DOM has either a `<details>` element or an
  //      `<h2>` whose text matches the per-message heading shape.
  // Both checks must pass — keeps the toggle off non-thread pages
  // even if a future feature mounts a stray gmail-mirror page.
  function isThreadPage(article) {
    if (!article) return false
    if (window.location.pathname.indexOf("/_ingested/gmail/") === -1) {
      return false
    }
    if (article.querySelector("details")) return true
    var h2s = article.querySelectorAll("h2")
    for (var i = 0; i < h2s.length; i++) {
      if (THREAD_HEADING_RE.test(h2s[i].textContent || "")) return true
    }
    return false
  }

  // brain: wrap the H2-message (and its trailing siblings up to the
  // first `<details>` / end of article) in a synthetic `<section>` so
  // the SCSS / filter has the same shape regardless of whether the
  // section is markdown-emitted `<details>` or the latest plain H2. The
  // wrapper carries the same data attributes so the toggle can hide it
  // via the `[data-brain-is-mine]` selector.
  //
  // POSITION-AGNOSTIC ON PURPOSE, and that is what saved it. The scan
  // finds the FIRST thread-shaped H2 among `article.children` and sweeps
  // forward; it never assumes the H2 is first. On real corpus documents
  // the H2 is LAST, so the `DETAILS` arm of the end-scan below never
  // fires — it is defensive, not load-bearing. Kept rather than deleted:
  // it costs one comparison and it is the reason the "leading `<h2>`"
  // error at the top of this file was invisible instead of a bug.
  //
  // Rationale: `to_extracted_thread` renders the latest message as
  // `## H2 + body` (test-pinned in `tests/test_gmail_thread.py`). We
  // can't change that without churning the test contract; wrapping
  // client-side gets us a uniform DOM shape with zero ingest-side
  // change. Idempotent — checks `WIRED_ATTR` on `<article>` first.
  function wrapLatestMessage(article, userEmail) {
    var children = Array.prototype.slice.call(article.children)
    var startIdx = -1
    for (var i = 0; i < children.length; i++) {
      var child = children[i]
      if (
        child.tagName === "H2" &&
        THREAD_HEADING_RE.test(child.textContent || "")
      ) {
        startIdx = i
        break
      }
    }
    if (startIdx === -1) return // nothing to wrap

    var endIdx = children.length
    for (var j = startIdx + 1; j < children.length; j++) {
      var node = children[j]
      if (
        node.tagName === "DETAILS" ||
        // P4.4 — the runtime button itself is also injected at the top
        // of <article>; don't sweep it into the latest-message section.
        (node.classList && node.classList.contains(TOGGLE_CLASS))
      ) {
        endIdx = j
        break
      }
    }

    var section = document.createElement("section")
    section.className = SECTION_CLASS + " " + LATEST_CLASS
    var fromText = parseFromAddress(children[startIdx].textContent || "")
    section.setAttribute(FROM_ATTR, fromText)
    section.setAttribute(
      IS_MINE_ATTR,
      String(fromMatchesUser(fromText, userEmail)),
    )
    children[startIdx].parentNode.insertBefore(section, children[startIdx])
    for (var k = startIdx; k < endIdx; k++) {
      section.appendChild(children[k])
    }
  }

  // brain: stamp `data-brain-thread-from` + `data-brain-is-mine` on
  // each `<details>` inside the article. The summary's text carries
  // the same `YYYY-MM-DD HH:MM — <from>` heading we parse from H2s.
  // Adds the section class for SCSS parity with the wrapped latest.
  function annotateDetails(article, userEmail) {
    var detailsList = article.querySelectorAll("details")
    for (var i = 0; i < detailsList.length; i++) {
      var d = detailsList[i]
      if (d.hasAttribute(FROM_ATTR)) continue // idempotent
      d.classList.add(SECTION_CLASS)
      var summary = d.querySelector("summary")
      var fromText = summary ? parseFromAddress(summary.textContent || "") : ""
      d.setAttribute(FROM_ATTR, fromText)
      d.setAttribute(
        IS_MINE_ATTR,
        String(fromMatchesUser(fromText, userEmail)),
      )
    }
  }

  // brain: build + insert the toggle button at the top of <article>.
  // Idempotent — bails if a button is already present (covers SPA
  // re-init on the same article element). The button's
  // `aria-pressed` mirrors the body class state for assistive tech.
  function renderToggle(article) {
    if (article.querySelector("." + TOGGLE_CLASS)) return
    var pressed = loadRepliesOnly()

    var button = document.createElement("button")
    button.type = "button"
    button.className = TOGGLE_CLASS
    button.setAttribute("aria-pressed", String(pressed))
    button.textContent = pressed ? LABELS.showingMine : LABELS.show

    document.body.classList.toggle(REPLIES_ONLY_CLASS, pressed)

    button.addEventListener("click", function () {
      var next = !document.body.classList.contains(REPLIES_ONLY_CLASS)
      document.body.classList.toggle(REPLIES_ONLY_CLASS, next)
      persistRepliesOnly(next)
      button.setAttribute("aria-pressed", String(next))
      button.textContent = next ? LABELS.showingMine : LABELS.show
    })

    article.insertBefore(button, article.firstChild)
  }

  // brain: top-level entry. Locates `<article>`, gates on
  // `isThreadPage`, then runs annotate + wrap + render. Idempotent at
  // article granularity (`WIRED_ATTR`).
  function init() {
    var article = document.querySelector("article")
    if (!article) return
    if (!isThreadPage(article)) {
      // Not a thread page — but still flip the body class so a stale
      // `brain-replies-only` from a prior thread page doesn't bleed
      // into a non-thread navigation.
      document.body.classList.remove(REPLIES_ONLY_CLASS)
      return
    }
    if (article.getAttribute(WIRED_ATTR) === "true") {
      // Re-runs of init() on the same article (SPA back/forward) just
      // re-sync the body class with persisted state — no DOM rewrite.
      document.body.classList.toggle(REPLIES_ONLY_CLASS, loadRepliesOnly())
      return
    }

    var userEmail = getUserEmail()
    annotateDetails(article, userEmail)
    wrapLatestMessage(article, userEmail)
    renderToggle(article)
    article.setAttribute(WIRED_ATTR, "true")
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init)
  } else {
    init()
  }

  // brain: re-run on Quartz SPA navigation. `enableSPA: true` in
  // `quartz.config.ts` makes Quartz dispatch a `nav` event on
  // `document` after each in-page route change — same hook the
  // linkSourceTag.js script uses.
  document.addEventListener("nav", init)
})()
