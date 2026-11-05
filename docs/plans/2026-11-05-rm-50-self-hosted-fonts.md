# RM-50: self-hosted fonts

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: `docs/specs/2026-10-23-roadmap-completion-design.md`, RM-50. Sources: BL-7; consolidation
spec Q14, §7.1.2, §7.3. Depends on RM-05, which is done (#67,
`tests/test_ui_static_css_urls.py`).

## Goal and acceptance criteria (quoted)

**Goal:** "The reading page uses a real text face, delivered from the package, with no external
origin."

1. "The built wheel contains the font (the packaging test)."
2. "The CSP test expects the new directive and nothing else."
3. "No external origin appears in the static tree."
4. "The reading page's computed font family is the face; other chrome keeps system stacks."

**Scope out:** "the instrument chrome (it keeps system stacks)."

## What the spec does not say

Each of these came from reading the code or measuring. Each one changes the work.

1. **The browser harness has no CSP.** `tests/test_ui_browser.py` serves the tree with
   `SimpleHTTPRequestHandler`, so no browser test sees `security.CSP`. Q14's failure is that
   `default-src 'none'` blocks `@font-face` silently. A row on that harness passes with
   `font-src` missing. AC4 therefore runs against the real `create_app` with its real middleware.
2. **A computed `font-family` proves nothing about loading.** It returns the declared list even
   when the font was blocked. The row must also assert that the `FontFace` loaded and that the
   glyphs painted differently from the fallback.
3. **The licence file trips the offline test.** `test_no_external_urls_anywhere_in_the_static_tree`
   reads every non-binary file. `OFL.txt` has URLs on lines that do not start with a comment
   prefix, so §7.3's note that a vendored licence passes does not hold for a verbatim OFL. That
   test also calls `read_text` on every file not in `_BINARY_SUFFIXES`, so a `.woff2` would raise
   `UnicodeDecodeError`.
4. **`mimetypes` does not know `.woff2` on Python 3.11 or 3.12 without a host database.**
   `MimeTypes(filenames=()).guess_type("a.woff2")` returns `(None, None)` on both; it only
   resolves here through a host file. Starlette then falls back to `application/octet-stream`
   (`responses.py:315`). See D5.
5. **`--measure` is in `ch`, so the face moves it.** `ch` is the advance of "0", and tokens.css says
   "about 0.6em in every family". Measured: Newsreader 0.567em, Literata 0.579em. Both stay in
   the 45-75 band (table below), but the comment and its measured numbers go stale.
6. **`test_the_reading_measure_stays_in_the_classical_band` would measure the fallback.** Canvas
   `measureText` does not wait for a web font. It must await `document.fonts.load(...)` first.
7. **The CSP literal is pinned twice.** `_RM18B_CSP` and
   `test_the_agent_tab_ships_under_the_unchanged_csp` in `tests/test_ui_security.py`. The name
   stops being true, so it is renamed. `index.html:11`'s comment quotes part of the policy and
   stays accurate, so it is left alone.
8. **Subsetting is modification under the OFL,** so a Reserved Font Name forbids keeping the name.
   Source Serif 4's name table reads "with Reserved Font Name 'Source'", although the OFL.txt
   beside it does not. It is excluded for that reason. See the candidate table.
9. **No test builds a wheel.** `release.yml` is the only `python -m build`. The existing guards
   compare the tree to the globs. That is what the spec's test plan names, but AC1 says "built
   wheel". See D4.
10. **The ticket's line count is stale:** `ui/security.py` is 254 lines, not 243. Still far under
    the ceiling.

## Candidates (RM-50.D1, measured)

Built in a scratch directory from `google/fonts` at
`24ddc1f324cac5d9f5aedca126d298f56e3f1640`, with fonttools 4.68.0: `wght` limited to 400-700,
`opsz` pinned to 16, Latin plus Latin Extended-A, layout features `kern liga calt ccmp locl mark mkmk`
plus the numeral features, name table reduced to IDs 0-6, 13, 14. Sizes are roman plus italic.

| Face | woff2 total | "0" advance | 48ch is about | Numeral features | RFN |
|---|---|---|---|---|---|
| **Newsreader** | 109 KiB | 0.567em | 57 chars | pnum, tnum | none |
| **Literata** | 89 KiB | 0.579em | 51 chars | lnum, onum, pnum, tnum | none |
| Crimson Pro | 91 KiB | 0.565em | 60 chars | lnum, onum, pnum, tnum | none |
| Gelasio | 81 KiB | 0.614em | 60 chars | lnum, onum, pnum, tnum | none |
| Source Serif 4 | not rebuilt with numerals (Latin Ext-A only: 80 KiB) | 0.5em (default instance) | not measured | not checked | **"Source": excluded** |

"48ch is about" comes from `hmtx` over the alphabet, not a rendered page. T4 re-measures it in a
browser. Keeping `opsz` was not measured. It adds deltas, and Newsreader has 11 KiB of headroom.

Proposal: **Newsreader and Literata.** Newsreader is the face §7.1.2 already names. Literata was
drawn for long-form screen reading and leaves more room under the cap.

## Decisions

- **D1, the face.** The spec has the implementer propose two with screenshots and the reviewer
  pick. T1 produces them. **Recommendation: Newsreader**, subject to the screenshots, because
  §7.1.2 names it and it sits mid-band.
- **D2, how much of the face.** (a) Roman only, with browser-synthesised italics. (b) Roman and
  italic, two files. **Recommendation: (b).** Every `<em>` in a note body is italic, and both candidates fit
  under the cap with both styles. This reads the ticket's "one face" as one family, not one
  file.
- **D3, where the face applies.** (a) Prepend it to the `--font-read` token. (b) Apply it to
  `.note-body` and `.note-title` only. **Recommendation: (a).** The token is the "page" role, and
  its other users (Agent answer, Ingest heading, Discovery) chose that role deliberately. `--font-ui`
  and `--font-mono` do not change. A reader of AC4 could take (b), so this is called out.
- **D4, AC1's real wheel.** (a) Glob guards only, plus one manual build recorded in the PR. (b) The
  glob guards plus a CI step in the `lint` job: `python -m build --wheel`, then
  `scripts/check_wheel_static.py <wheel>`, which fails if any file under `ui/static` is missing
  from the wheel. It covers the whole tree, not only the font. (c) A pytest that builds a wheel,
  which needs setuptools or network in every venv and would skip where it cannot run.
  **Recommendation: (b).** This repo has shipped one broken wheel already (`ed8195f`), and the
  guards today read the source tree. `bin/brain-ci` mirrors the step because it mirrors the `lint`
  job. `build` is installed by the step, not added to `dev`.
- **D5, the Content-Type.** (a) A small `StaticFiles` subclass in `ui/app.py` that sets `font/woff2`
  on 200 responses. (b) `mimetypes.add_type`, which is process-global and undone by a later
  `mimetypes.init()`. (c) Accept `application/octet-stream`; browsers do not apply `nosniff` to
  fonts, so the font still loads. **Recommendation: (a),** about 10 lines. It does not depend on
  the host, and a test can prove it by stubbing `starlette.responses.guess_type`.
- **D6, the glob.** The ticket says `static/fonts/*`. **Recommendation:** two explicit patterns,
  `static/fonts/*.woff2` and `static/fonts/*.txt`, per the rule at the top of that pyproject
  section ("Explicit extension patterns"). `test_every_shipped_asset_matches_a_declared_glob`
  still catches anything else placed there.
- **D7, Latin Extended-A.** The ticket says "subset to Latin". **Recommendation: include it.**
  Otherwise a name with a diacritic renders one glyph from the fallback mid-word. Measured sizes
  include it. `unicode-range` declares the same set, so non-Latin text goes straight to the stack.
- **D8, preload.** **Recommendation: none.** `font-display: swap` is what the ticket specifies, the
  server is local, and `<link rel=preload>` is not in scope.

## Files

| File | Change | Budget |
|---|---|---|
| `src/brain/ui/security.py` | `font-src 'self'` in `CSP`, comment | +2 |
| `src/brain/ui/app.py` | D5 subclass, used by the `/static` mount | +15 |
| `src/brain/ui/static/fonts/*.woff2` | roman, italic | at most 120 KiB total |
| `src/brain/ui/static/fonts/OFL.txt` | upstream licence, verbatim | new |
| `src/brain/ui/static/css/reading.css` | two `@font-face` rules, provenance comment | +25 |
| `src/brain/ui/static/css/tokens.css` | `--font-read`, refreshed `--measure` comment | ±6 |
| `pyproject.toml` | two globs, comment | +6 |
| `scripts/check_wheel_static.py` | D4, new | at most 80 |
| `.github/workflows/ci.yml`, `bin/brain-ci` | D4 step | +8 each |
| `CHANGELOG.md` | entry | +3 |

Nothing crosses the 800-line ceiling, and no row in `docs/ceiling-table.md` changes. No new command,
config or skill text. No migration. `ci.json` is untouched (the ticket says Eval: none).

`@font-face` uses a relative `url("../fonts/...")`, which RM-05's test resolves and checks against
the globs. Appendix B's row is the coordinator's update at merge.

## Tasks

- **T1, D1 screenshots.** Against the `brain demo` sandbox (55433, synthetic) and a throwaway vault,
  both candidates, both themes, at a note and at the Agent tab. Local files only, never committed
  or published; the paths go to team-lead. If Docker cannot run the sandbox, say so rather than
  substitute live data. Stop for the pick.
- **T2, tests first.** The tests below, seen failing.
- **T3, implementation** of the table above with the chosen face.
- **T4, re-measure** `--measure` in a browser, update the tokens.css comment and the `60.18` comment
  in `tests/test_ui_browser.py`.
- **T5, real checks.** A real `python -m build --wheel` plus the script; `brain ui` against the
  sandbox; mutation proof for each guard, in a `git archive` copy.

## Tests

Each guard has a pair: it refuses the bad case and keeps the ordinary one.

**CSP** (`tests/test_ui_security.py`)
- Refuses: the literal gains `font-src 'self'` and nothing else, so any wider value (`*`, `data:`,
  `https:`) fails exact equality.
- Keeps: the other seven directives are byte-identical. The renamed agent-tab test still covers
  `/`, `/static/js/ask.js` and `/api/ask/config`, and gains a font path.

**Budget and licence** (new `tests/test_ui_fonts.py`, nodb, helpers over synthetic directories)
- Refuses: total over 120 KiB; a file without the `wOF2` magic; a `.woff2` with no licence beside it;
  an `@font-face` with no `font-display: swap`; a `src` that is not relative.
- Keeps: the shipped tree passes; a directory just under the cap passes; a stylesheet with no
  `@font-face` passes.
- Keeps: `--font-ui` and `--font-mono` do not name the face. Refuses: a synthetic token block that
  puts it there.

**Offline** (`tests/test_ui_static_assets.py`)
- `.woff2` joins `_BINARY_SUFFIXES`, which stays inside the packaging walk.
- `fonts/OFL.txt` is exempt from the URL scan by exact path.
- Refuses: `fonts/x.css` or a differently named `.txt` carrying `https://` still fails, through a
  helper taking a synthetic tree.
- Keeps: the licence itself passes.

**Packaging**
- `test_every_shipped_asset_of_a_kind...` is not extended to `.woff2`. It reads `mimetypes`, which
  depends on the host; the Content-Type tests below read the server.
- Refuses: a font outside every glob (RM-05 already has this through its helper). Keeps: the shipped
  fonts match.
- `scripts/check_wheel_static.py`, on synthetic zips. Refuses: a wheel missing `fonts/*.woff2`.
  Keeps: a complete wheel passes, and `.py` or `__pycache__` entries are not demanded.

**Content-Type** (D5)
- With `starlette.responses.guess_type` stubbed to `(None, None)`: a font is `font/woff2`, and the
  stub is proven real because a `.png` falls to `application/octet-stream`.
- Keeps: unstubbed, `.css` and `.js` are unchanged; a conditional request still gets a 304.

**Browser, real app and real CSP** (new `tests/test_ui_browser_fonts.py`; the filename is inside CI's
glob; uvicorn on a pre-bound socket, fake context as in `test_ui_security.py`, `/api/**` stubbed)
- Keeps: an open note's `.note-body` has the face first in its computed family; both `FontFace`s report
  `loaded`; rendered width differs from the fallback; no `securitypolicyviolation`; every request
  stays on the app's origin.
- Refuses: the same page with `font-src` stripped from the header by the test's route. The row's
  check must report the face as not loaded, which proves it can see Q14's silent failure.
- Keeps: rail, ledger and buttons resolve to `ui-sans-serif`, `.gutter` to `ui-monospace`.

`test_the_reading_measure_stays_in_the_classical_band` awaits the font before measuring.

**Run every time:** `tests/test_log_privacy_rules.py`, `tests/test_log_privacy.py`,
`test_import_hygiene*`, `tests/test_vault_delete.py`, `tests/test_cli_registry*.py`, the src-wide
rglob scans, skills tests, `scripts/check_ceiling_table.py`, `tests/test_ci_workflow.py`, all
`tests/test_ui_*`, `ruff check`, `mypy src/`. CI runs the full suite.

## Security

- **CSP** widens by `font-src 'self'` only: no external origin, no `unsafe-inline`, no nonce.
- **Supply chain.** The font is a third-party binary parsed by the browser. Provenance is recorded
  next to the rule: upstream commit and SHA-256 of the inputs. The subset command is recorded so the
  build can be repeated. fonttools is a one-off tool in a scratch venv and joins no dependency list.
- **Metadata.** The name table drops designer and vendor URL records and keeps copyright and licence,
  as the OFL requires. WOFF2 is compressed, so the URL scan cannot read those records; they are
  metadata and nothing fetches them.
- **Confidential data.** No read path, no `documents` statement, no `sensitivity` surface. Static
  files only. The F6 audit is unaffected.
- **Untrusted input.** None at runtime; the font is packaged, never user-supplied.
- **Screenshots.** Synthetic sandbox only, local only.

## Risks

- **Taste.** Handled by D1.
- **Swap shift.** Text reflows when the face arrives. It arrives from loopback, and only when a
  note first uses it.
- **Headroom.** Newsreader leaves 11 KiB. A wider subset needs the cap revisited, and the budget test
  says so.
- **PII hook.** `OFL.txt` carries upstream's copyright line with a public GitHub org. If a pass flags
  it, the answer is an allowlist entry for a public token, never `--no-verify`. The licence text
  must stay verbatim.
- **Pinned tests.** Other guards may read the static tree; the named set finds them.
- **Real-app browser test.** The first browser row with uvicorn, so the fixture must shut down
  cleanly and leave nothing running.

## Rulings and what changed in the build

**Rulings on the plan:** D1 Newsreader, without stopping for a pick; the screenshots stay local and
the reviewer may overturn it. D2 to D8 as recommended.

**Departures from the text above:**
- `scripts/check_wheel_static.py` builds with `python -m pip wheel` from a clean copy instead of
  `python -m build`. pip is already in both venvs, so `build` is not installed anywhere, and the
  copy is what keeps a stale `build/` from hiding a missing file. It is one command, so
  `tests/test_bin_brain_ci.py`'s lint-parity guard applies as it stands.
- The script is 99 lines, not 80.
- The browser job's selection is an explicit roster, not a glob. `tests/test_ui_browser_fonts.py`
  was added to it, as `tests/test_ci_workflow.py` requires.
- `fonts/SOURCE.txt` is new. It holds the provenance in place of the comment above the `@font-face`
  rules, and a test ties its digests to the shipped files. It carries no URL scheme, so it needs no
  scan exemption.
- `.lede` does not use the face. It sets no `font-family` and sits outside `.note-body`, so it keeps
  the UI stack. D2's first draft said otherwise. Nothing was changed for this.
- Two RM-05 tests used `static/fonts/` as their example of a directory no glob ships. It now is one,
  so they use `static/vendor/`.
- T1's data came from `brain demo --database-url` on a throwaway database, by ruling. `/api/recent` is
  empty for that corpus, so the Discovery screenshot stubs its two rows, and the Agent-turn screenshot
  injects a turn's markup. Both go through the real stylesheets.
