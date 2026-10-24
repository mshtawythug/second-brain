# RM-05 — The offline test resolves every CSS `url()`

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-05 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(§3, Wave 1). The ticket is the spec. This plan is the task list, as DoD item 1 asks.
**Backlog row:** BL-3 in [`2026-10-23-backlog-reconciliation.md`](./2026-10-23-backlog-reconciliation.md).
**Reuses:** `tests/test_ui_static_assets.py`'s own helpers: `static_dir()`, `_shipped_by_suffix`,
`_package_data_globs` and `_glob_matches`. No `src/` change.

## Files

| File | Change |
|---|---|
| `tests/test_ui_static_assets.py` | Two regexes (`_CSS_COMMENT_RE`, `_CSS_URL_RE`), three helpers (`_css_url_targets`, `_css_url_problems`, `_unshipped_targets`), the test `test_every_css_url_resolves_on_disk`, a parametrized test of the extraction, and synthetic-tree tests for each acceptance criterion. |
| `CHANGELOG.md` | An Unreleased entry. |
| Spec Appendix B, backlog BL-3 | Tracker rows, in a follow-up commit once the PR number exists. |

**Not touched:** every file under `src/`, so no ceiling row needs a promoted hop and no daemon
reinstall follows. No skill or memory file documents the static-asset tests.

## Details

### Extraction: `_css_url_targets(css) -> list[str]`

1. Strip `/* … */` comments (non-greedy, `DOTALL`), replacing each with a space.
   `css/graph.css` and `css/related.css` say "no url()" in a comment.
2. `finditer` with one quote-aware pattern, case-insensitive (CSS function names are ASCII
   case-insensitive): `\burl\(\s*("…"|'…'|bare)\s*\)`. Matches do not overlap, so the
   double-quoted `data:` target in `css/base.css` consumes the `url(%23n)` nested inside it.
   The bare alternative refuses a backslash, so a CSS escape there (`url(a\)b)`) matches
   nothing, instead of being cut short and checked as `a\`.
3. Each target is stripped of surrounding whitespace, as the URL parser does.
4. **Nothing is skipped silently.** After every match is removed, a `url(` that is still left is
   one the pattern could not parse (a backslash escape, a stray quote in a bare target). That raises
   `ValueError`, which the test reports as a failure. Without this, a `url()` the regex did not
   understand would simply go unchecked.

### Resolution: `_css_url_problems(stylesheet, static_root) -> (files, problems)`

- Skips `data:` targets and pure `#fragment` targets (these fetch no file).
- A target with **no path** is a problem: an empty one (`url()`, `url("")`, `url('  ')`) or a
  query-only one (`url("?v=1")`, `url("?#iefix")`), tested with `urlsplit(target).path`. Joined as
  a URL it resolves to the stylesheet itself, and joined as a path it resolves to the directory,
  so neither `exists()` nor `is_file()` catches it.
- Every other target is joined the way a browser does: `urljoin` against the stylesheet's served
  URL (`<origin>/static/<path>`). A relative target therefore resolves against the stylesheet's
  own directory, as the ticket says. A root-relative `/static/...` target resolves to the same
  file the server would send (the form `index.html` uses), rather than failing.
- A target on another origin is a problem. So is a path outside `/static/`, and so is a file that
  resolves outside `static_dir()` after `resolve()`. That last one happens through a symlink, or
  through a percent-encoded `%2e%2e/`, which is decoded only after `urljoin` and so passes the
  `/static/` prefix check. Together these are AC 3.
- The query and fragment are dropped (`font.woff2?v=1#iefix` fetches `font.woff2`). The path is
  percent-decoded, and the result must pass `is_file()`.
- Each problem names the stylesheet (relative to `static_dir()`) and the target (AC 2), and says
  which check fired ("outside /static/", "not same-origin", "names no file", …).
- A `url(` that `_css_url_targets` cannot parse comes back as one problem ("could not parse"),
  never as an exception and never as nothing.

### Packaging: `_unshipped_targets(name, files, package_root, globs) -> list[str]`

One problem for each resolved file that matches no `package-data` glob, relative to
`package_root` (`brain/ui`). It is a helper of its own because, on today's tree, it receives no
files: the only real `url()` is a `data:` SVG. Without a synthetic test it would be dead code that
no mutation could turn red.

### The test on the real tree

`test_every_css_url_resolves_on_disk` checks every shipped `.css` file, found with
`_shipped_by_suffix(".css")`. That covers `css/*.css` and any future top-level `static/*.css`,
which pyproject also declares. It asserts:

- At least one stylesheet was found.
- No problems.
- Every resolved file matches a declared `package-data` glob (`_unshipped_targets`).

**The packaged copy.** `static_dir()` reads through `importlib.resources`, so in CI (an editable
install) it reads `src/`. No wheel is built: `test_every_shipped_asset_matches_a_declared_glob`
already proves every file under `brain/ui/` is in the wheel. The new test also checks each
resolved target against the globs. The day RM-50 adds `static/fonts/x.woff2`, a missing
`static/fonts/*.woff2` pattern fails *this* test too, naming the stylesheet that needs the file.

There is no "at least one `url()` found" floor, which would go red if `base.css` lost its noise
texture. The residue check in step 4 already makes an unparsed `url(` fail. A stylesheet with no
`url(` at all genuinely has nothing to check.

## Tests (RED first)

| AC | Test |
|---|---|
| 1. Green on `master` | `test_every_css_url_resolves_on_disk` |
| 2. `url("missing.woff2")` fails, naming the stylesheet and the target | `test_a_missing_css_url_target_is_named` |
| 3. A `url()` that escapes `static_dir()` fails | `test_a_css_url_escaping_static_fails` (parametrized: `../../`, `../../../`, root-relative outside `/static/`, other origin, protocol-relative, both of those with a path that names a real local file, and an encoded `%2e%2e/%2e%2e/`), `test_a_css_url_through_a_symlink_out_of_static_fails` |
| 4. `data:` passes, including a nested `url(%23id)` | `test_data_and_fragment_urls_pass`, and the `nested data URI` case of the extraction test |
| 5. A comment-only `url()` is ignored | the `comment` cases of the extraction test, and `test_a_commented_out_css_url_is_not_checked` |
| 6. An empty `url()` fails | `test_an_empty_css_url_fails` (parametrized: `url()`, `url("")`, `url('  ')`, and the query-only `url("?v=1")`, `url("?#iefix")`) |

Extraction, parametrized (`test_css_url_extraction`): double-quoted, single-quoted, bare,
whitespace inside the parentheses, several on one line, upper-case `URL(`, a comment (one-line and
multi-line), a nested `data:` URI, an empty target. `test_an_unparseable_css_url_raises` covers
the residue check, and `test_an_unparseable_css_url_is_a_problem` covers how `_css_url_problems`
reports it. `test_unshipped_targets_reports_only_files_no_glob_ships` runs `_unshipped_targets`
against the real `brain.ui` globs: `static/fonts/a.woff2` is reported, and `static/brain-logo.png`
and `static/css/x.css` are not, so both an accept-all and a reject-all helper fail. Each
`test_a_css_url_escaping_static_fails` case asserts which check fired. Positive cases are included so that a resolver that rejects everything also
fails.

Resolution positives: `test_resolvable_css_urls_pass` (sibling, `../` into a sibling directory,
root-relative `/static/...`, query and fragment, percent-encoded space).

Mutation proof (DoD 2), in a `git archive` copy with `PYTHONPATH` pinned:

1. A typo'd `url()` added to a real stylesheet turns the real-tree test red.
2. Removing the comment strip turns the comment cases red.
3. Making the pattern stop being quote-aware (bare only) turns the nested `data:` case red.
4. Removing the no-path check turns all five `test_an_empty_css_url_fails` cases red.
5. `is_file()` swapped for `exists()` turns `test_a_css_url_naming_a_directory_fails` (`./`, `..`) red.
6. Removing the residue check turns `test_an_unparseable_css_url_raises` red.
7. Removing the symlink containment check turns the symlink test red.
8. A `url()` to a real `static/fonts/x.woff2` that no glob ships turns the real-tree test red.
9. Removing the origin check turns the `*-static-path` escape cases red. The first run of the
   harness found this mutation surviving, because every other-origin case also had a path
   outside `/static/`. The two cases were added for that reason.

Added in review round 1 (PR #67), each of which survived before its test existed:

10. `_css_url_problems` swallowing the parse error (`return [], []`) turns
    `test_an_unparseable_css_url_is_a_problem` red.
11. `_unshipped_targets`'s glob check disabled (`if False:`) turns its synthetic test red.
12. The same check made to reject everything (`if True:`) turns it red too.
13. The no-path check narrowed back to `if not target:` turns the two query-only cases red.
14. Removing the `/static/` prefix check turns the three "outside /static/" escape cases red. The
    origin check used to catch them by accident; they now assert the reason.
15. Letting the bare alternative accept a backslash again turns the `escaped-paren` case red.
16. Restoring the old, symlink-only wording of the containment problem turns the symlink test
    and the `encoded-dot-dot` case red.

## Risks

- A `/*` inside a CSS string would be stripped as a comment opener. Nothing ships one, and the
  ticket prescribes stripping comments first.
- `@import "x.css"` (a string, not `url()`) is out of scope, as the ticket defines it. Nothing
  ships one.
