// Brain wiki redesign — email-thread reading-mode transformer plugin
// (P4.4 of the Wiki UX Overhaul).
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/plugins/transformers/emailThread.ts`
// by `brain vault render --overlay`. It does NOT compile or run from
// the brain repo itself; the imports below resolve against the
// dependencies Quartz pulls into the cloned workspace via
// `npm install`, not against any package brain ships.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz
// version restructures the transformer plugin shape or the
// `externalResources()` hook contract, pull the latest transformer
// reference from
// https://github.com/jackyzha0/quartz/tree/v4/quartz/plugins/transformers
// and re-apply the brain tweaks flagged below — `// brain:` for
// value/structural choices on upstream-supported fields, and
// `// brain-extension:` for keys/types that don't exist in stock
// Quartz.
//
// What this transformer does, and why: P4.4 ships an "Email thread
// reading mode" — every per-message section in an `email_thread`
// body becomes a collapsible `<details>` block (older messages
// closed, latest expanded), and a "Show only my replies" filter
// button hides every section whose `From:` header doesn't equal the
// owner's email. The runtime that wires these affordances lives in
// `quartz/static/emailThread.js`; this transformer's job is to (a)
// inject that script into every page and (b) bake the owner's email
// (read from `process.env.BRAIN_USER_EMAIL` at build time) into a
// tiny inline `<script>window.BRAIN_USER_EMAIL = "..."</script>`
// snippet so the runtime knows whose `From:` to match against.
//
// brain: this transformer ALWAYS injects the runtime — same pattern
// as `LinkSourceTag` and `CodeCopy`, both of which are part of the
// production redesign and don't gate on env vars. The user-email
// global is emitted only when `BRAIN_USER_EMAIL` is set; without it
// the runtime renders the toggle button anyway but the filter
// matches no message (correct "you forgot to set the env var" cue).
//
// brain: cross-references for the email-thread reading-mode contract:
//   * `quartz_overrides/quartz/static/emailThread.js` — the runtime
//     this transformer points page `<head>` at.
//   * `quartz_overrides/quartz/styles/brain/_email_thread.scss` — the
//     SCSS partial that paints `<details>` and the filter button,
//     and reads the body class (`brain-replies-only`) plus per-
//     section attributes (`data-brain-is-mine`) the runtime stamps.
//   * `src/brain/ingest/gmail.py` — `to_extracted_thread()`, which
//     emits the `## YYYY-MM-DD HH:MM — <from>` headings + per-message
//     `<details>` markup the runtime parses.
//   * `src/brain/config.py` — the `Config.user_email` field and
//     `BRAIN_USER_EMAIL` env-var contract this transformer mirrors.
//
// Registration: this transformer has NO ordering requirement among
// the other transformers — it doesn't touch the mdast tree, only
// contributes resources via `externalResources()`. Place it anywhere
// in the `transformers: [...]` list; the brain `quartz.config.ts`
// template wires it next to `LinkSourceTag` / `CodeCopy` so the
// brain script-only transformers stay grouped.

import { QuartzTransformerPlugin } from "../types"

// brain-extension: the path the page `<script src=...>` tag points
// at. Leading slash is intentional — Quartz emits pages at arbitrary
// slug depths (e.g. `/_ingested/gmail/<thread>/index.html`) and a
// relative `static/emailThread.js` would break for any non-root
// page. Server-rooted `/static/emailThread.js` resolves consistently
// regardless of which page the user landed on. This works because
// Caddy's `file_server` (and Quartz's stock `file_server` in dev)
// serves the build dir as the site root
// (`<vault>/.quartz/current/static/emailThread.js`).
const SCRIPT_SRC = "/static/emailThread.js"

// brain-extension: build-time env var carrying the owner's email.
// Read inside `externalResources()` so each plugin instance evaluates
// the value fresh per build (matters for tests + for `bin/brain-up`
// re-invocations where the env may differ between runs).
const USER_EMAIL_ENV_VAR = "BRAIN_USER_EMAIL"

// brain: minimal escape pass for the user-email value before it gets
// interpolated into the inline `<script>` body. The email comes from
// the user's own `.env` so it's not adversarial input, but a stray
// `</script>` (malformed entry) or `\` would break the script tag
// boundary. Escaping `<`, `>`, `\`, and `"` keeps the inline-script
// stream parseable. Symmetric with how stock Quartz escapes inline
// JSON in its `description.ts` transformer.
function escapeForJsString(value: string): string {
  return value
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/\r/g, "\\r")
    .replace(/\n/g, "\\n")
}

// brain-extension: the transformer plugin itself. Empty options
// shape — the only knobs would be the script path and the env var
// name, both of which are part of the contract documented above.
// Keeping the signature `(opts?: never)` documents that no
// configuration is expected; if a knob is genuinely needed later
// (e.g. a per-vault user-allowlist override), widen the type at that
// point rather than guessing now.
export const EmailThreadReader: QuartzTransformerPlugin = (_opts?: never) => {
  return {
    name: "EmailThreadReader",
    externalResources() {
      // brain: read the env var fresh per build. `.trim()` so a
      // trailing newline from a `.env` quirk doesn't bleed into the
      // JS global.
      const rawEmail = (process.env[USER_EMAIL_ENV_VAR] ?? "").trim()
      const escapedEmail = escapeForJsString(rawEmail)

      // brain: emit two JS resources:
      //
      //   1. inline — sets `window.BRAIN_USER_EMAIL` BEFORE the runtime
      //      script reads it. `loadTime: "beforeDOMReady"` ensures the
      //      global is in place before the runtime's
      //      `DOMContentLoaded` listener fires. Always emitted (even
      //      when the env var is unset) so the runtime always sees
      //      `window.BRAIN_USER_EMAIL` defined as `""` rather than
      //      `undefined` — simpler null handling on the runtime side.
      //
      //   2. external — the runtime itself, served from
      //      `/static/emailThread.js`. `loadTime: "afterDOMReady"`
      //      because the runtime walks the article DOM at boot, and
      //      `afterDOMReady` guarantees the article is in place.
      //
      // `spaPreserve` left unset on both — Quartz's SPA reuses the
      // existing `<script>` between navigations only when set; we want
      // both scripts to re-run on each `nav` event so the runtime can
      // re-detect the page and re-inject the toggle.
      return {
        js: [
          {
            loadTime: "beforeDOMReady",
            contentType: "inline",
            script: `;window.BRAIN_USER_EMAIL = "${escapedEmail}";`,
          },
          {
            loadTime: "afterDOMReady",
            contentType: "external",
            src: SCRIPT_SRC,
          },
        ],
      }
    },
  }
}
