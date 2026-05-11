// Brain wiki redesign — code-block copy-button injector (Lane C).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/static/codeCopy.js` by `brain vault render
// --overlay`, and is copied verbatim into every build's `static/` dir
// by Quartz's stock `Plugin.Static()` emitter (no extra wiring needed
// — Quartz already mirrors `quartz/static/` 1:1 into the build
// output). The script is referenced from page `<head>` by the brain
// `Plugin.CodeCopy` transformer's `externalResources()` hook (see
// `codeCopy.ts` for the inject side).
//
// Tested against Quartz v4.5.x (April 2026). Plain vanilla JS — no
// transpile, no bundler — so it survives any future Quartz refactor
// of its plugin pipeline.
//
// What this script does, and why: Quartz's stock `Body.tsx` wires a
// `.clipboard-button` into every `<pre>`, but its visual treatment
// (gray border, white background, top-right floating chip) reads as
// 2018-Hugo. The 2026 redesign wants a small-caps "Copy" pill with
// the redesign palette, an "↗ Copied" success state, ARIA labelling,
// and graceful keyboard focus. Rather than rewrite `Body.tsx` (which
// would require also vendoring its clipboard.inline.ts), we hide
// the stock button via CSS in `_code.scss` and inject our own
// `.brain-code-copy` button here.
//
// Idempotency contract: every `<pre>` gets at most one
// `.brain-code-copy` button. We mark each processed `<pre>` with
// `data-brain-copy-injected="true"` so SPA navigation re-runs of
// the tagger don't double-inject. First writer wins.
//
// Also handles the `data-language` attribute fallback. Quartz's
// `Plugin.SyntaxHighlighting` stamps `<pre data-language="bash">`
// directly, but for fenced code blocks that the highlighter doesn't
// match (e.g. unrecognised languages), the attribute lands only on
// the inner `<code class="language-X">`. The CSS-only language label
// in `_code.scss` reads the attribute via `attr()`, which only works
// on `pre[data-language]`. This script copies the attribute from the
// inner `<code>` to the outer `<pre>` whenever the outer is missing
// it, so the label reliably renders.
//
// Browser support: targets evergreen Chrome / Firefox / Safari. Uses
// `navigator.clipboard.writeText`, `document.querySelectorAll`, and
// `addEventListener` — all baseline since 2017.
;(function () {
  // brain: the data-attribute we stamp on processed `<pre>` blocks.
  // Used as the idempotency check.
  var INJECTED_ATTR = "data-brain-copy-injected"

  // brain: regex matching `language-X` classes Quartz writes onto
  // `<code>` for fenced blocks. Captures the language token.
  var LANGUAGE_CLASS_RE = /(?:^|\s)language-([\w+-]+)/

  // brain: copy the inner `<code class="language-X">`'s language token
  // up to the outer `<pre data-language="X">` whenever the outer is
  // missing it. CSS reads from the outer; the highlighter sometimes
  // only stamps the inner. Idempotent — skips when outer already has
  // the attribute.
  function liftLanguageAttr(pre) {
    if (pre.hasAttribute("data-language")) return
    var code = pre.querySelector("code")
    if (code === null) return
    var match = LANGUAGE_CLASS_RE.exec(code.className)
    if (match === null) return
    pre.setAttribute("data-language", match[1])
  }

  // brain: extract the code text the user wants to copy. Stock
  // Quartz's clipboard handler reads `dataset.clipboard` (a JSON
  // override) when present, otherwise falls back to `innerText`.
  // Mirror that contract so syntax-highlighted blocks with line
  // numbers don't leak the gutter into the copy buffer.
  function codeTextFor(pre) {
    var inner = pre.querySelector("code")
    if (inner === null) return pre.innerText
    if (inner.dataset.clipboard) {
      try {
        return JSON.parse(inner.dataset.clipboard)
      } catch (_e) {
        // Fall through to innerText if the JSON is malformed.
      }
    }
    return inner.innerText
  }

  // brain: the action accessible name when the button is at rest.
  // Mirrored into `aria-label` after every flash so SR users hear
  // the right verb when the chip returns to its idle state.
  var BASE_LABEL = "Copy code to clipboard"

  // brain: the actual copy handler. Modern browsers gate
  // `navigator.clipboard` behind secure contexts (https / localhost).
  // Falls back to a no-op (visible "Copy failed" pulse) when the
  // browser blocks it — better UX than a silent click.
  function copyHandler(pre, button) {
    return function (event) {
      event.preventDefault()
      var text = codeTextFor(pre)
      if (typeof navigator.clipboard === "undefined") {
        // Non-secure context — no clipboard API available.
        flashLabel(button, "Unavailable")
        return
      }
      navigator.clipboard.writeText(text).then(
        function () {
          flashLabel(button, "Copied")
        },
        function () {
          flashLabel(button, "Failed")
        },
      )
    }
  }

  // brain: swap the button label to a status string for 1.4s, then
  // restore. Toggles `.is-copied` so `_code.scss` can paint the
  // success state.
  //
  // brain (Lane C audit fix 2026-05-02): also mirror the status into
  // `aria-label`. Per ARIA spec, `aria-label` OVERRIDES the
  // element's text content for the accessible name — so updating
  // only `textContent` left SR users hearing "Copy code to clipboard"
  // on every press, regardless of whether the copy succeeded.
  // Mapping: "Copied" → "Code copied to clipboard" (action-confirming
  // affirmative), "Failed" / "Unavailable" → "<base label> — <status>"
  // (annotated state) so the user hears why the action didn't take.
  function flashLabel(button, status) {
    var prior = button.dataset.brainOriginalLabel || "Copy"
    button.textContent = status
    var statusLabel
    if (status === "Copied") {
      statusLabel = "Code copied to clipboard"
    } else {
      statusLabel = BASE_LABEL + " — " + status.toLowerCase()
    }
    button.setAttribute("aria-label", statusLabel)
    button.classList.add("is-copied")
    window.setTimeout(function () {
      button.textContent = prior
      button.setAttribute("aria-label", BASE_LABEL)
      button.classList.remove("is-copied")
    }, 1400)
  }

  // brain: build the copy button as a DOM node. Class `.brain-code-copy`
  // matches the SCSS selector in `_code.scss`. ARIA label spells out
  // the action for screen readers (the visible "Copy" text is also
  // the accessible name; `aria-label` refines it for non-visual
  // users — and is updated on each flashLabel() call so it tracks
  // the visible status).
  function buildButton() {
    var button = document.createElement("button")
    button.type = "button"
    button.className = "brain-code-copy"
    button.textContent = "Copy"
    button.setAttribute("aria-label", BASE_LABEL)
    button.dataset.brainOriginalLabel = "Copy"
    return button
  }

  // brain: walk every `<pre>` in the article body and inject the
  // copy button if not already done. We scope to `article` so popover
  // hints and chrome blocks (no code) aren't touched.
  function injectAll() {
    var pres = document.querySelectorAll("article pre")
    pres.forEach(function (pre) {
      // brain: idempotency — skip already-processed blocks.
      if (pre.getAttribute(INJECTED_ATTR) === "true") return
      pre.setAttribute(INJECTED_ATTR, "true")

      // brain: ensure the language label has an attribute to read.
      liftLanguageAttr(pre)

      // brain: build + wire the button.
      var button = buildButton()
      button.addEventListener("click", copyHandler(pre, button))
      pre.appendChild(button)
    })
  }

  // brain: run on first DOM ready. `DOMContentLoaded` fires before
  // first paint of below-fold content; above-fold pre blocks may
  // briefly miss the button, but the hover-only opacity means it's
  // invisible until cursor enters the pre — no flash.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectAll)
  } else {
    injectAll()
  }

  // brain: re-run on Quartz SPA navigation. `enableSPA: true` in
  // `quartz.config.ts` makes Quartz dispatch a `nav` event on
  // `document` after each in-page route change. The injected button
  // doesn't survive the morph (micromorph swaps the article DOM),
  // so we re-walk on every nav.
  document.addEventListener("nav", injectAll)
})()
