# RM-53: `brain examples`

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: `docs/specs/2026-10-23-roadmap-completion-design.md`, RM-53. Size XS, wave 9, depends on
RM-06 (merged). Not started: `src/brain/resources/` does not exist, and `brain examples` and
`cli_examples` appear nowhere in `src/`, `tests/`, `skills/`, `docs/cli-reference.md` or
`CHANGELOG.md`.

## Goal and acceptance criteria (quoted from the spec)

**Goal:** "Find the right command by example."

**Scope in:** "`brain examples [TOPIC]` (through RM-06); a packaged resource
`src/brain/resources/examples.md` with its own `package-data` glob." **Scope out:** "generating
examples from `docs/cli-reference.md`."

**Design:** "the command prints curated, grouped invocations from the resource, by topic: search,
ingest, graph, brief and review, maintenance. Every example is synthetic (rule 15). A test parses
every `brain …` line in the resource and asserts that its command path exists in the Typer app, so
the examples cannot rot silently."

**Acceptance criteria:**

1. "Each topic prints, and an unknown topic lists the valid ones."
2. "The rot test passes, and fails when an example names a command that does not exist."
3. "The installed wheel contains the resource."

The spec already settles RM-53.D1: curated, not generated. Ceiling: none.

## Design

### The resource format

Strict and line-oriented, so that no `brain …` line can sit where the rot test does not look:

````markdown
# brain examples

## search: Find what you already stored

```bash
# Hybrid search across everything
brain search "vendor renewal"
```
````

The parser accepts only these:

- **Outside a fence:** blank lines, the one H1, and `## <slug>: <title>`.
- **Inside a fence:** blank lines, `# comment` lines, and lines starting `brain `.

Anything else raises. That includes prose outside a fence, an indented `  brain …`, a typo such as
`brian …`, `echo x | brain …`, a line ending in `\`, a fence left open, an empty topic, and a
repeated slug.

Without this strictness the rot test could pass vacuously: a stale line that the parser does not
recognise as an invocation would never be checked.

### The command

- `brain examples TOPIC` prints that topic. Bare `brain examples` prints all of them (D2).
- Output is plain `typer.echo`, not Rich. Rich would wrap long invocations at terminal width and
  read `[...]` as markup, and both break copy and paste.
- An unknown topic writes `Unknown topic. Valid topics: search, ingest, ...` to stderr and exits 2,
  the repo's bad-invocation code. The valid list comes from the resource, so there is no second
  source of truth. The input is **not echoed**, so an argument cannot put escape sequences on the
  terminal.
- An unreadable or malformed resource (a broken install) raises `ExamplesResourceError(BrainError)`.
  The command prints one fixed line with no path and exits 1.
- The command never calls `Config.load()` and never connects to a database. It works before
  `brain init` and with a bad `BRAIN_*` variable.

### Content rules for the resource

These are reviewed by eye, except where a test is named.

- Synthetic only (rule 15): `vendor renewal`, `Jane Doe`, `@example.com`.
- Runnable as typed: concrete values, no `<placeholder>` tokens, because a shell reads `<` as a
  redirect (tested).
- Nothing destructive: no `brain rm`, `setup --reset`, `capture review`, or anything that needs
  typed confirmation. People copy and paste examples.
- `brain ingest-gmail` always carries a scope flag; the CLI refuses without one.
- Roughly 5 to 7 invocations per topic.
- Printable ASCII only (tested). The repo's bidi scan does not cover `.md`, and a reader pastes
  these lines into a shell.

## Affected files

| File | Change | Budget |
|---|---|---|
| `src/brain/resources/__init__.py` | new, docstring only (D5) | 1 line |
| `src/brain/resources/examples.md` | new, the curated examples | ≤ 90 lines |
| `src/brain/examples.py` | new stdlib leaf: `ExampleTopic`, `parse_examples(text)`, `load_examples()`, `ExamplesResourceError` | ≤ 90 lines |
| `src/brain/cli_examples.py` | new: `register(app)` and rendering only | ≤ 50 lines |
| `src/brain/cli_registry.py` | one import, one `EXTENSION_REGISTRARS` entry | +2 |
| `pyproject.toml` | `"brain.resources" = ["*.md"]` and its `exclude-package-data` twin | +6 |
| `scripts/check_wheel_static.py` | checks a tuple of trees (D4) | +12 |
| `CONTRIBUTING.md` | the wheel-check row names the new tree | one row reworded |
| `docs/cli-reference.md` | `## brain examples` section | +15 |
| `CHANGELOG.md` | `[Unreleased]` entry | +5 |
| `skills/brain-maintenance/SKILL.md` | mention the command; **net zero lines**, the file is at its 300-line cap | ±0 |
| `tests/test_examples.py` | new: parser pairs, resource content | ≤ 110 lines |
| `tests/test_examples_rot.py` | new: the rot test and its checker's pairs | ≤ 110 lines |
| `tests/test_cli_examples.py` | new: the command, including one real subprocess | ≤ 80 lines |
| `tests/test_packaging_examples.py` | new: glob guards, mirrors `test_packaging_demo.py` | ≤ 45 lines |
| `tests/test_check_wheel_static.py` | second tree covered; fixtures updated | +25 |
| `tests/test_cli_registry_extensions.py` | pinned lists gain `examples` | +3 |
| `tests/test_import_hygiene.py` | cold-import entry for `brain.cli_examples` | +1 |

No file crosses 800 lines and no row in `docs/ceiling-table.md` changes. `cli.py` is not touched.

After merge, as on earlier tickets: the Appendix B tracker row and the auto-memory `cli.md` and
`types.md` entries.

## Tests

Every guard has a "refuses" test and a "keeps ordinary behaviour" test.

### Parser (`tests/test_examples.py`)

| Guard | Refuses | Keeps |
|---|---|---|
| Fence contents | `brian search x`, `  brain search x`, `echo x \| brain ingest-stdin`, a trailing `\` | `brain search "x"`, `# a comment`, blank lines |
| Outside a fence | prose, a `brain search x` line | H1, `## slug: title`, blank lines |
| Topic shape | empty topic, repeated slug, heading without a title, fence left open | two well-formed topics, in file order |
| Resource bytes | a control character, an ESC, a bidi control, a non-ASCII letter | the real resource |
| Placeholders | `brain show <id-prefix>` | `brain ingest-stdin --source slack < thread.txt` |

The real resource must parse to exactly the five slugs of D1, in order, each with at least one
invocation. That floor stops the rot test passing over an empty list.

### Rot (`tests/test_examples_rot.py`, against the real `brain.cli.app`)

`invocation_problem(root, line)` shlex-splits the line, stops at the first shell operator token
(`|`, `<`, `>`, `>>`, `&&`, `;`), walks the group tree, then checks flags (D3).

| Refuses | Keeps |
|---|---|
| `brain nosuchcommand` | `brain search "x"` |
| `brain vault nosuchsub` (bad path under a real group) | `brain graphrag communities build` (a nested path) |
| `brain vault` (stops on a group that needs a subcommand) | `brain graphrag communities` (a group with `invoke_without_command`) |
| `brain search "x" --nosuchflag` | `brain search "x" --json`, `--limit=5`, `-n 5` |
| a required option missing | the same command with it |
| a bad flag before a `<` | a bad-looking token after a `<` is not brain's and is ignored |

Then a parametrized test over every invocation in the real resource (AC2).

### Command (`tests/test_cli_examples.py`)

| Refuses | Keeps |
|---|---|
| `brain examples nosuch` exits 2, stderr lists every valid topic, stdout empty | each topic exits 0 and prints its own invocations and not another topic's (AC1) |
| hostile argument (`$'\x1b]0;x\x07'`) is absent from the output | bare `brain examples` prints all five |
| broken resource (standard test double on the loader) exits 1 with the fixed line and no path | a long invocation is not wrapped at `COLUMNS=40`; `[` survives |

**One real test:** a subprocess runs `python -m brain examples search` with
`BRAIN_SECRET_GUARD=block`, a value that makes `Config.load()` raise, and `DATABASE_URL` pointing
at a closed port. Exit 0 proves the command needs no config and no database. The control runs
`python -m brain status` under the same environment and asserts it exits 2, so the bad variable is
shown to bite.

### Packaging (AC3)

- `tests/test_packaging_examples.py`: the glob covers the tree, there is no broad `**/*` or `*`,
  `importlib.resources` resolves the file.
- These read the source tree, which is exactly how RM-50's hole stayed open. The real guard is
  `scripts/check_wheel_static.py`, which builds a real wheel in CI's `lint` job and in
  `bin/brain-ci` (D4).
- `tests/test_check_wheel_static.py`: a wheel missing `brain/resources/examples.md` exits 1 and
  names it; a wheel carrying both trees passes; an empty resources tree is a setup error, not a
  pass.

### End to end, once by hand, reported in the PR

Run the real `scripts/check_wheel_static.py`. Build a wheel, install it non-editable into a
throwaway uv venv, run `brain examples` and `brain examples nosuch` from outside the repo, and
delete the venv.

### Mutation proofs, in `git archive` copies only

| Mutation | Expected to fail |
|---|---|
| Remove the `brain.resources` glob from `pyproject.toml` | real `check_wheel_static.py`, exit 1 naming `examples.md` |
| Change one resource line to `brain searhc "x"` | rot test |
| Change one resource flag to `--jsonn` | rot test |
| Drop the walker's group-bottom check | `brain vault` row |
| Parser tolerates a line outside a fence | parser refuses row |
| Echo the input in the unknown-topic message | hostile argument row |
| Command calls `Config.load()` | the real subprocess test |
| Remove the `EXTENSION_REGISTRARS` entry | command tests, pinned list |

### Test sets

Each run includes the named set: `test_log_privacy_rules`, `test_log_privacy`, `test_import_hygiene*`,
`test_vault_delete`, `test_cli_registry*`, `test_repo_hygiene_files`, every src-wide rglob scan, the
skills tests, and `scripts/check_ceiling_table.py`. Also `test_cli_help_rendering`, `test_ci_workflow`
and `test_bin_brain_ci`. No full suite locally; CI runs it. `df` and the lock around every pytest.

## Risks

- **A pinned list I have not found.** `test_cli_registry_extensions.py` is known. Another list would
  surface in the named set or in CI's full run.
- **Typer's click internals.** A comment in `cli_errors.py` records that Typer changed its `Context`
  class across releases. The rot walker uses only `typer.main.get_command`, `.commands`, `.params`,
  `.opts`, `.secondary_opts` and `.required`, and detects a group with `hasattr(cmd, "commands")`,
  with no direct `click` import. CI's pinned Typer is the arbiter.
- **Flag values that look like flags.** Curated content avoids them. If one is ever needed the rot
  test fails loudly, which is the safe direction.
- **Renaming the scope of `check_wheel_static.py`.** The filename stays, because
  `tests/test_ci_workflow.py`, `bin/brain-ci`, `ci.yml` and `CONTRIBUTING.md` pin it. Only its
  docstring and messages widen.
- **The installed daemons** run a non-editable install, and a merge does not affect them; this
  command has no daemon surface.

## Decisions

**D1. The slug for "brief and review".**
(a) `brief-review`; (b) `brief`, with a `review` alias; (c) two topics.
**Recommend (a).** The spec lists five topics. In the unknown-topic list, `brief-review` tells someone
who typed `review` where to go, without alias code.

**D2. What bare `brain examples` does.**
(a) Print every topic; (b) print only the topic list; (c) exit 2 asking for a topic.
**Recommend (a).** The goal is finding a command, and `brain examples | grep gmail` is the natural
way to do that. Scripted use gets the whole resource, and the unknown-topic path already lists the
names.

**D3. How much the rot test checks.**
(a) Command path only, the spec's minimum; (b) path, every flag exists, required options present;
(c) full `make_context`.
**Recommend (b).** A renamed flag is likelier than a renamed command, and it costs about 15 test
lines. (c) is wrong here: it fails on `exists=True` paths such as `brain ingest ./notes/plan.pdf`
(`cli_ingest.py:239`) and runs option callbacks. Positional-count checking is left out, because a
flag's value and a positional cannot be told apart without parsing.

**D4. How AC3's "installed wheel" is proved.**
(a) Generalize `scripts/check_wheel_static.py` to a tuple of trees; (b) a pytest that builds a wheel;
(c) source-tree glob tests only.
**Recommend (a), plus the cheap glob tests from (c).** The script already builds a real wheel from a
clean copy in CI's `lint` job and in `bin/brain-ci`, with its timeouts and minimal environment
reviewed under RM-50. (b) adds a slow, index-dependent test and duplicates it. (c) alone is the hole
the script exists to close.

**D5. A regular package or a data directory.**
(a) `src/brain/resources/__init__.py` with `"brain.resources" = ["*.md"]`; (b) no `__init__.py`,
extending `"brain" = ["migrations/*.sql", "resources/*.md"]`.
**Recommend (a).** The spec asks for its own glob, and `brain.demo` and `brain.templates` set the
precedent. A cold-import failure also reads clearly as `ModuleNotFoundError`, which the loader turns
into the fixed error.

## Security

The ticket's stated surface is "synthetic examples only". There is no ingested data, no filesystem
input from the user, no network, no subprocess, no database read, and so no `sensitivity` path and
nothing for the F6 audit.

What remains, all handled above:

- **The `TOPIC` argument is the only untrusted input.** It is compared with the slugs and never
  printed or used to build a path. The slug selects from a parsed mapping, so `../../x` is simply an
  unknown topic (a test covers it alongside the hostile argument).
- **The resource is trusted, like the code it ships with,** and read through `importlib.resources`
  from a fixed name. It gets no size cap or `O_NOFOLLOW`, because anyone who can change it can change
  `brain/*.py`. It is printable ASCII by test, so a bad edit cannot hide a character from a reviewer
  or a paste.
- **Fixed error strings** with no paths. `ExamplesResourceError` is never stored on an attribute.
- **Copy-paste safety** is the real risk in an examples file: nothing destructive, and no unscoped
  `ingest-gmail`.
- **No regex** is planned in the parser beyond an anchored heading match on a trusted file.

A dedicated security reviewer looks disproportionate here, so I would run the normal code review and
completion audit. Your call at PR time.

## Rulings and deviations (recorded after approval)

- **D1 to D5 approved as recommended,** and no dedicated security reviewer.
- **Added by the coordinator at approval:** every example is safe to run as written against a real
  brain, and `brain ingest-gmail` always carries a scope flag. `tests/test_examples_rot.py` holds
  this: an allowlist of classified commands, a banned-flag list, a preview flag or a `# Writes:`
  comment on anything that writes, and a required Gmail scope flag.
- **`brain brief` and `brain review weekly` are classified as writing.** `brief` checks saved watches
  and records their hits; `review weekly` writes a vault page unless `--no-emit`. `--wiki` is banned
  outright because it can deliver the digest off the machine.
- **Line budgets exceeded, none near the ceiling:** `examples.md` 129 (budget 90), because each of
  its 36 invocations carries a comment and a blank line; `examples.py` 104 (90); `test_examples_rot.py`
  268 (110), from the safety checks above; `test_examples.py` 143 (110); `test_cli_examples.py` 124 (80).
- **`check_wheel_static.py`'s messages changed,** from "static files" to "files" and "shipped files".

## Fix round: the safety checker could be bypassed (rev-122)

- **One invocation only, enforced at load time.** `is_valid_fence_line` refuses `;`, `&`, `|`, `>`,
  `$`, backtick and parentheses, and any `<` that is not the second-to-last word. The trailing-word
  rule lives here rather than in the test's `_args`, so it holds when the resource loads; the checkers
  call the same function.
- **A subcommand after a group's flag is refused,** because Click parses the group's options and then
  dispatches: `brain gaps --since 30d push` runs `gaps push`.
- **Added beyond the review's list: an option's value is no longer read as a flag.** `--tag --dry-run`
  passed the safety check as a preview, when Click takes `--dry-run` as the tag and the command writes.
  `_flags` now skips values and expands aliases, so `-l` counts as `--label`.
- **The Gmail scope list is compared with the real refusal text,** by equality.
