# RM-43 — Code-repository ingestion (the owner's own files)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-43". **PR:** #113. The ticket is the
spec; this is the task list. Base: `second-brain/master` at `75a7074`. Depends on RM-06 (the
`cli_registry` seam) and RM-42 (`ingest/chunk_dispatch.chunk_for`, the code chunker), both merged.

## What ships

`brain ingest-code <repo-dir> --author-email E [--author-email E2 …] --since YYYY-MM-DD` ingests
the tracked files of one local git work tree that the owner touched since that day. Each file is
one document, `content_type="code"`, chunked at definition boundaries by RM-42. A new `code`
source kind, a `brain doctor` row for `git`, and `code` in the concept-skip policy. No migration,
no config knob, no MCP tool.

Also BL-35, carried from RM-42: `vault/sync.py` re-chunks an edited `code` mirror through
`chunk_for`, so the definition-level chunks survive a vault edit.

## Files

| File | Change |
|---|---|
| `src/brain/ingest/code_repo.py` (new leaf, < 300 lines) | The `git` reader. `CodeRepoError(BrainError)`; `GitRunner` (argv → stdout bytes); `git_runner(...)` (production runner, on `capped_process.run_capped`); `work_tree_root`, `tracked_files`, `owner_touched_files`. Only reads; quotes no `git` output in any message. |
| `src/brain/ingest/code_files.py` (new leaf, < 250 lines) | Pure selection and reading, no `git`: `select_candidates` (vendored dirs, lockfiles, `--ext`), `read_code_file` (regular file, inside the root, ≤ 200 KB, not binary, not minified, not empty), `build_code_doc` (the `ExtractedDoc`). Skip reasons are named constants. |
| `src/brain/cli_ingest_code.py` (new, < 300 lines) | `brain ingest-code`: the scope refusal, the loop, the cap, the report. `register(app)`. |
| `src/brain/doctor_git.py` (new leaf, ~40 lines) | The `git` row: WARN when `git` is not on `PATH`, else OK. Runs no `git`. |
| `src/brain/doctor_runtime.py` (under the ceiling; §2.2 budget) | One import, one row line, one docstring entry (+4). |
| `src/brain/cli_registry.py` | One import, one `EXTENSION_REGISTRARS` entry (`ingest-code`, after `ingest-folder`). |
| `src/brain/source_kinds.py` | `code` in `VALID_SOURCE_KINDS`, plus its docstring line. |
| `src/brain/graph_rag/extract_policy.py` | `code` in `CONCEPT_SKIP_CONTENT_TYPES`; `CONCEPT_SKIP_POLICY_VERSION` `v2` → `v3`, with the docstring line saying why (the RM-33 precedent). The bump re-reconciles every existing `calendar_event` and `screenshot` once, with no model call (the module's own rule). |
| `src/brain/ui/static/js/results.js`, `discovery.js` | `code: "💻"` in both `SOURCE_ICONS`. `tree.js` unchanged: no month grouping (stated, and pinned by a test). |
| `src/brain/vault/sync.py` (over the ceiling) | BL-35. The import line `chunker.chunk_text` → `chunk_dispatch.chunk_for` (net zero); `_embed_and_insert_chunks` takes `content_type` and `metadata`, and both callers pass them. **Grows the file** (about +12, including the inline reason and the docstring pointer); recorded in both places. |
| `CLAUDE.md` `vault/sync.py` row | Promote the descriptive last hop ("the PR #58 review-round commit") to `538054d` (1,817, net zero), then add a descriptive hop for this change, with its growth note. |
| tests (below) | |
| `CHANGELOG.md`, `docs/cli-reference.md`, spec Appendix B (RM-43 → DONE), backlog (RM-43 row SHIPPED, BL-35 SHIPPED), `skills/ingest-brain/SKILL.md` routing row | Docs. |
| memory `cli.md`, `extractors.md`, `types.md`, `graphrag.md` | Marked "verify merged". |

## The command

```
brain ingest-code <repo-dir> --author-email E [...] --since YYYY-MM-DD
    [--ext py --ext ts ...] [--max-files 2000] [--sensitivity confidential]
    [--tag T ...] [--enrich/--no-enrich (default off)]
```

1. **Scope refusal (exit 2).** Without `--author-email` or `--since`: `ingest-code: --author-email
   and --since are required (only files you committed since that day are ingested)`. `--since`
   not `YYYY-MM-DD`, or a blank or malformed email (no `@`, contains `<`, `>` or whitespace):
   exit 2 with a message naming the flag. `--sensitivity` goes through `_validate_sensitivity_or_exit`.
2. **Not a work tree (exit 1).** `git -C <dir> rev-parse --show-toplevel` fails, or `<dir>` is
   inside a `.git` directory or a bare repository. `git` not on `PATH`: exit 1 with the install
   hint (`xcode-select --install` / `brew install git`).
3. **Which files.**
   - `tracked = ls-files -z --cached --full-name` minus `ls-files -z --cached --ignored
     --exclude-standard --full-name`, so a force-added file that `.gitignore` matches is still
     excluded ("anything git ignores"). Untracked files are never listed.
   - `touched = log -z --name-only --format= --no-renames -i -F --author=<E1> --author=<E2> …
     --since="<day> 00:00:00" HEAD --`. Each email is passed as `<email>` with `-F`, so it matches
     the whole address in `Name <email>`: `ann@x.test` does not match `joann@x.test`. `-i` because
     addresses are case-insensitive in practice. The explicit midnight is needed because git reads
     a bare date as that day at the current time of day.
   - Candidates are `tracked ∩ touched`, sorted. When `<dir>` is a subdirectory of the work tree,
     only files under it count.
4. **Always excluded**, each with a counted reason in the report:
   - a path with a component in `{node_modules, .venv, vendor, dist, build, third_party}`, compared
     case-insensitively;
   - a lockfile, by basename (`package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`,
     `pnpm-lock.yaml`, `poetry.lock`, `Pipfile.lock`, `uv.lock`, `Cargo.lock`, `Gemfile.lock`,
     `composer.lock`, `go.sum`, `flake.lock`, `mix.lock`, `Podfile.lock`, `packages.lock.json`,
     `bun.lockb`) or any `*.lock`;
   - with `--ext`, any other extension;
   - not a regular file (a symlink, a submodule, gone from the work tree), or a path whose real
     location leaves the work tree. Opened with `O_NOFOLLOW` and `fstat`-checked, so a swapped-in
     symlink is not followed;
   - over 200 KB (`fstat` size, and the read is capped at 200 KB + 1). The cap also keeps every
     file clear of tiktoken's backtracking `ValueError` on very long whitespace runs (the
     carry-forward note);
   - binary: a NUL byte, or not valid UTF-8 (a BOM is stripped);
   - minified: a `.min.` name (`app.min.js`), a `.map` file, or a file whose longest line is over
     1,000 characters and whose mean line length is over 200 (decision D4);
   - empty or whitespace-only.
5. **Each file** goes to `ingest_document` with:
   - `doc = ExtractedDoc(title=f"{repo}/{path}", content=text, content_type="code",
     source_path=str(absolute path), metadata={"path": path, "language": lang, "repo": repo})`.
     Here `repo` is the work tree's directory name, `path` is repo-relative POSIX, and `lang` is
     `code_chunker.language_of(path, None)`, omitted when `None`;
   - `source_kind="code"`, so dedup is by `source_path` (protocol point 3, rule 2);
   - `secret_guard="reject"` always, whatever `BRAIN_SECRET_GUARD` says (RM-43.D1), and no
     `--allow-secrets` flag. A refused file prints `refused <path>: N finding(s)` and the run goes on;
   - `sensitivity` defaults to `confidential`;
   - `enricher=None` unless `--enrich`;
   - `graph_syncer` as the other ingest commands build it. Concepts are skipped by policy; the
     people aspect runs and finds nobody.
6. **Re-run** (AC5): an unchanged file is the pipeline's same-hash `skip`; a changed file updates
   in place, keeping the document id. No volatile metadata, so `volatile_patch` is not needed.
7. **The cap** (`--max-files`, default 2,000, `min=1`) counts files *written* (`ingested` +
   `updated`), as `ingest-notion --max` does. An unchanged file is free, so re-runs drain a large
   repository. Past the cap each remaining candidate is compared with its stored row by
   `source_path` and `sha256` (exact, since reject mode never redacts), and
   `stopped at --max-files N: K file(s) remain; run again to continue` is printed.
8. **Output.**
   - One line per written, refused or failed file, with its repo-relative path through
     `one_line`, then the counts and skip reasons. Failures are printed by class (`describe_failure`).
   - **Exit codes:** 0 when the run completes, including refusals and per-file failures, as
     `ingest-notion` and `ingest-dir` do (decision D3); 1 for a repo, `git` or DB failure before
     the loop; 2 for usage.
   - An `ANALYZE` (`_analyze_after_bulk_write`) runs when anything was written.

## The `git` runner

`git_runner(args, *, repo, which=shutil.which, popen=subprocess.Popen, base_env=os.environ,
timeout=GIT_TIMEOUT_SECONDS (120), max_stdout_bytes=GIT_MAX_STDOUT_BYTES (64 MiB))`:

- **The binary** is found with `which("git")` and run by its absolute path.
- **argv:** `[git, "--no-pager", "-C", repo, "-c", "core.fsmonitor=false", "-c",
  "core.quotePath=false", "-c", "log.showSignature=false", *args]`. A repository's
  `core.fsmonitor` names a command git would run, and signature display would start `gpg`.
- **env (minimal):** `PATH=os.defpath`, `HOME` from `base_env` (so the owner's global excludes
  apply), `LC_ALL=C`, `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0`, `GIT_CONFIG_NOSYSTEM=1`.
  Nothing else is inherited.
- **Process handling** is `run_capped`'s: no stdin, stderr discarded unread, process-group kill
  at the timeout or the cap. An `OSError`, a timeout, an oversize or a non-zero exit is a
  `CodeRepoError` whose message names the subcommand and the exit code, never git's output,
  raised `from None`.

## Tests (TDD, RED first; real Postgres on 5434, `second_brain_test_rm43`)

Fixture `tests/code_repo_fixtures.py`:

- builds a synthetic repository in `tmp_path` with the real `git`, under a hermetic env
  (`HOME=tmp_path`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, fixed author and
  committer dates);
- two synthetic authors (`owner@example.test`, `other@example.test`);
- the owner's files: a Python module, a TypeScript file, a README;
- the other author's file;
- an ignored-but-force-added file and an untracked ignored file;
- `node_modules/` and `vendor/` files the owner committed, a `package-lock.json`, a `*.min.js`,
  a long-line minified file, a binary (NUL), a non-UTF-8 file, an oversize file, an empty file and
  a tracked symlink;
- one owner file older than `--since`;
- a planted fake key (`tests/secret_fixtures.SYNTHETIC_AWS_KEY`) in an owner file.

All names are synthetic constants.

- `tests/test_code_repo.py`: the runner (argv shape, minimal env, absolute binary, every error
  path through an injected `popen`/`which`, the message quoting no output), `work_tree_root`
  (work tree, subdirectory, not a repo, bare), `tracked_files` (ignored subtraction),
  `owner_touched_files` (two authors, `-F` whole-address match, case, `--since` midnight, an
  empty repository).
- `tests/test_code_files.py`: every exclusion, `--ext` normalisation, the escape and symlink
  checks, the 200 KB boundary (exactly 200 KB is kept, 200 KB + 1 is skipped), the minified
  heuristic's edges, the BOM, and the document shape.
- `tests/test_cli_ingest_code.py`: through `CliRunner` against 5434.
  - **AC1:** only the owner's files.
  - **AC2:** every exclusion holds.
  - **AC3:** the planted key is refused with `BRAIN_SECRET_GUARD=warn` set; nothing is written
    for that file.
  - **AC4:** stored `confidential`, and `--sensitivity normal` overrides.
  - **AC5:** a re-run is unchanged; an edit updates in place (same id); definition-level chunk
    headers.
  - **AC6 (first half):** `--max-files 2` writes two and reports the remainder; no summary
    without `--enrich` (a recording enricher is never called; `--enrich` calls it).
  - The scope refusals (exit 2) and the not-a-work-tree and missing-`git` exits (1).
  - `Config.load` is driven by env (`DATABASE_URL`, vault in `tmp_path`); the enricher and the
    `git` binary are swapped with `monkeypatch`, which is a standard test double.
- `tests/test_graphrag_code_concept_skip.py` (AGE, 5434), **AC6 (second half):** ingest through
  the command, then `brain graphrag build --backfill --concepts` twice with an extractor that fails
  if called. The second run reports `reconciled 0, skipped 1`, the RM-30 test's shape.
- `tests/test_code_source.py` (nodb): the kind is valid, both glyph tables have it, there is no
  month grouping, and the skill row exists.
- Updated:
  - `test_source_kinds_identity.py`: `ADDED_KINDS` gets `code`, with RM-43 in the comment;
  - `test_demo_corpus.py`: `DEMO_EXEMPT_KINDS` gets `code`, with the reason (needs a git work tree;
    stored `confidential`; keeps the 22-doc count);
  - `test_cli_registry_extensions.py`: the shipped tuple and the help order;
  - `test_extract_policy.py`: `code` is skipped and the version is `v3`;
  - `doctor` tests for the new row (`test_doctor_git.py`), and the composition test if it lists
    rows;
  - `test_vault_sync_code_mirror.py`: the last assertion flips. After the edit, the heads are
    `pkg/mod.py:1-1 <module>`, `pkg/mod.py:4-5 first` and `pkg/mod.py:8-9 second`. Renamed to
    say "with the code chunker".

Coverage is 100% line and branch on the new and touched modules. Guards are mutation-proven in
`git archive` copies with `PYTHONPATH` pinned, in batches of at most five. The guards are the
ignored subtraction, the `-F <email>` match, each exclusion, the escape check, the 200 KB cap,
the forced `reject`, the default `confidential`, `enrich` off, the cap counter, the policy entry
and the sync routing. Before the push: the log-privacy rules tests, every `rglob` scan of `src/`,
and `scripts/check_ceiling_table.py`.

**End-to-end:** `.venv/bin/brain ingest-code` against a synthetic repository in the scratchpad,
the 5434 DB and a `tmp` vault, then `brain search` for a symbol. The transcript goes in the PR.

## Rulings (approved by the coordinator, 2026-10-31)

- **D1 — title:** `<repo dir name>/<repo-relative path>`, so two repositories' `src/main.py` do
  not collide in lists.
- **D2 — subdirectory:** a `<repo-dir>` inside a work tree narrows the scan to files under it.
- **D3 — exit code:** 0 with the counts when files are refused or fail, matching `ingest-notion`
  and `ingest-dir`. Secret refusals are printed loudly: one red line per file, and a red
  summary saying the files were NOT ingested and what to do.
- **D4 — the minified heuristic:** a `.min.` name, `*.map`, or a longest line over 1,000
  characters together with a mean over 200, so one long string literal does not drop a
  normal file.
- **D5 — no `--allow-secrets`** on this command. A false positive is handled by editing the
  file or ingesting it with `brain ingest`.
- **D6 — the policy bump** to `v3` re-reconciles existing `calendar_event` and `screenshot`
  documents once, with no model call.
- **Links, ruling (b):** a `code` body materializes no wiki links. `vault/sync.py` hands
  `_materialize_links` (and the dry run's count) an empty body for `content_type="code"`
  (`_link_body`), which also clears any rows an earlier sync wrote. A `[[Name]]` in code would
  otherwise resolve to a note titled "Name" and show as a backlink. The alternative,
  documenting the phantom backlink as a residual, was rejected.
- **BL-35:** `_embed_and_insert_chunks` goes through `chunk_for`; the CLAUDE.md `vault/sync.py`
  row promotes `538054d` and adds the growth note.

## Found while finishing (production fixes, each with a regression test)

- **`capped_process.kill_group` on macOS.** `killpg` on a process group whose members have all
  exited but are not yet reaped fails with `EPERM`, not `ESRCH`. A child that wrote past the
  stdout cap and then exited was reported as "could not run (PermissionError)" instead of
  oversized. `kill_group` now suppresses `PermissionError` too. The fix is in the shared
  runner, so `gh`, `osascript` and the voice tools get it as well
  (`tests/test_capped_process.py`, two tests).
- **The missing-`git` hint.** `find_work_tree` turned every `CodeRepoError` into "not inside a
  git work tree", including the install hint. The runner now raises `GitNotFoundError` (a
  `CodeRepoError`), and `find_work_tree` lets it through.
- **A bad `--sensitivity` exits 1, not 2.** It goes through the shared up-front check that
  `ingest-dir` uses (`_validate_sensitivity_or_exit`), which exits 1 before anything is read.
  The module docstring says so; the other usage errors exit 2.

## Residuals (out of scope, named)

- No prune: a file deleted from the repository, or no longer touched in the window, keeps its
  document. The spec scopes out history. `brain rm` removes one.
- Wiki links in a code mirror are closed by ruling (b) above, not left as a residual.
- **Eval is the owner's step:** 3–5 code queries in the local golden corpus, then a re-record of
  `ci.json`. This PR touches neither; the PR body says so.
