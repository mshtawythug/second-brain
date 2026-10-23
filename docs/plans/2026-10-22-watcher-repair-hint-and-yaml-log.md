# Vault logs by exception class: plan and proof

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/watcher-repair-hint-and-yaml-log`, base `second-brain/master` @ `2d900e9`
(PR #56 merged).

## Part 1: no vault log line carries an exception's message

### Problem

PR #56 switched two `link_rewrite.py` WARNINGs to `brain.errors.describe_failure(exc)`, the class
only. Other vault sites still pass the caught exception object, so its MESSAGE goes into the log.

That is a content leak in two ways:

- **A YAML error quotes the line it failed on.** PyYAML's `MarkedYAMLError` does this. Measured on
  2026-10-22: `parse_frontmatter('---\ntitle: a: SECRET-VALUE\n---\n…')` raises a `ScannerError`
  whose text contains `title: a: SECRET-VALUE`. `_people.yml` holds people's names and emails.
- **A psycopg DETAIL quotes row values** (handled for `link_rewrite.py` in PR #56).

CLAUDE.md forbids document content in a WARNING log, and `describe_failure` exists for exactly this.

### The inventory, re-derived

The inventory came from an AST scan, not a grep. Multi-line logger calls defeat a grep for
`%s", e`. The scan matches each `<…log…>.<level>(...)` call inside an `except … as NAME` handler
whose arguments contain `NAME` other than inside `describe_failure(...)`. The same scan is now
test G1, below. At `2d900e9`:

**`src/brain/vault`: 16 sites, all fixed here (class only):**

| File | Sites | What they catch |
|---|---|---|
| `derived_links/directory.py` | `load_people_yml` ×2; `refresh_calendar`; `refresh_contacts` | `yaml.YAMLError` (quotes `_people.yml`); `OSError`; `DirectoryRefreshError`/`RuntimeError` (gws stderr) |
| `derived_links/fence.py` | read, malformed frontmatter, rewrite | `OSError`; `ValueError`/`yaml.YAMLError` (quotes the note) |
| `link_rewrite.py` | read, malformed frontmatter, write | `OSError`; `ValueError`/`yaml.YAMLError` (quotes the note) |
| `rename.py` | EXDEV fallback; writes failed (`except Exception`); partial-file cleanup; restore | `OSError`; any exception |
| `sync.py` | the two id-write-back ERRORs | `OSError` |

**Recorded, not fixed here (outside `src/brain/vault`):**

| File | Site | Catches |
|---|---|---|
| `graph_rag/communities_summary.py` | the embedding pass failed | `Exception` |
| `graph_rag/extract.py` | extraction aborted: Ollama unavailable | `OllamaUnavailable` |
| `graph_rag/fuse.py` | the hybrid vector leg failed; the hybrid FTS leg failed | `Exception` (a psycopg error can carry DETAIL) |
| `graph_rag/global_.py` | the global vector leg failed | `Exception` (same) |

**Also recorded: five `logger.exception(...)` calls in `vault/watch.py`.** They are:

- the worker's "worker failed on …";
- the handler's two guards, "classify failed" and "schedule failed";
- the debounce-timer start fallback;
- the test-only `on_event` hook guard.

A traceback carries the exception message too.

- They are left alone deliberately. A traceback is the watcher daemon's only diagnosis for an
  unexpected failure, and it logs at ERROR, not WARNING.
- Making them class-only is a policy decision about diagnosability, not a mechanical fix.

### Tests (RED first at `2d900e9`)

| ID | Test | RED at `2d900e9` |
|---|---|---|
| G1 | no `src/brain/vault` logger call passes a caught exception raw (the AST scan above) | listed all 16 sites |
| G2 | the scan flags `e` passed bare and nested (`str(e)`) | passes (it guards the guard) |
| G3 | the scan accepts `describe_failure(e)` and `errors.describe_failure(e)`, and ignores a name not bound by a handler | passes (same) |
| Y1 | a malformed `_people.yml` is logged as `ScannerError`, without the quoted line | logged `name: a: synthetic-secret-value-42` |
| Y2 | `rewrite_vault_links` on malformed frontmatter: the same | logged the quoted line |
| Y3 | `rewrite_derived_fences` on a synced note whose file was then broken: the same | logged the quoted line |

### Ceiling

- `sync.py` is over 800 lines. This touch is **net-zero**: two arguments are replaced in place,
  and the import joins the existing `from ..errors` line. Its CLAUDE.md row promotes the
  startup-blind hop (`5e3c0f2`, 1,817) and adds a descriptive hop.
- The other four files are under 800 lines. Re-derive with `wc -l`.

## Part 2: dropped

The plan was for the watcher's shutdown WARNING to name `brain graphrag remove <ids>` as the cheap
graph repair. That is dropped from this branch, because PR #57 (`brain graphrag remove`) updates
`graph_lane.LOST_REMOVES_REPAIR` itself, and `watch.py` imports that constant.

This branch does not touch the constant, the WARNING, W4 or `watch.py`. The branch name keeps
"repair-hint" from the original scope.

## Proof

### RED at `2d900e9`

G1, Y1, Y2 and Y3 failed at their targeted assertions; the table above gives the messages. G2 and
G3 passed, as they should: they test the scan, not the code.

### Mutation matrix (five clauses), in a `git archive` copy only: 9 of 9 OK

The judge reads only each JUnit failure's `message`, and the failing set must EQUAL the prediction.
The control passed, the negative controls (a wrong text, a wrong set) were both rejected, and the
archive was restored byte-identical.

| Mutant | Edit | Killed by |
|---|---|---|
| Y1 | `load_people_yml`'s YAML WARNING passes `exc` raw | G1, Y1 |
| Y2 | `rewrite_vault_links`' malformed-frontmatter WARNING passes `e` raw | G1, Y2 |
| Y3 | the fence rewrite's malformed-frontmatter WARNING passes `e` raw | G1, Y3 |
| S1 | both `sync.py` id-write-back ERRORs pass `e` raw (applied 2×, as declared) | G1 |
| R1 | `rename.py`'s "writes failed" ERROR passes `exc` raw | G1 |
| D1 | `refresh_calendar`'s WARNING passes `exc` raw | G1 |
| GM1 | the guard loses its `describe_failure` exemption | G1, G3 |
| GM2 | the guard binds no handler names (so it can never fire) | G2 |
| GM3 | the guard only sees a logger named exactly `logger` (missing `_logger`) | G2 |

**What GM2 and GM3 prove:** G1 passing on the fixed tree is not vacuous. A blind scan (GM2) or a
narrowed one (GM3) is caught by G2, while G1 would still pass.

### Suites (run serially on a sibling test DB): 654 passed

That is `test_vault_log_privacy` plus every suite for the five touched modules:

- `tests/derived_links/`, `test_cli_directory`;
- `test_cli_note_rename`, the four `test_rename_*`;
- `test_cli_relink_derived`, `test_mark_confidential_fence_refresh`;
- `test_vault_link_rewrite`, `test_vault_sync_link_rewrite`;
- the other six `test_vault_sync*`, `test_cli_vault_sync`;
- the repo-hygiene and reading-surface guards.

`ruff check` and `mypy src/` are clean.
