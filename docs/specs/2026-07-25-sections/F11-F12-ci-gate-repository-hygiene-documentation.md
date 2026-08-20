# F11-F12 — CI gate, repository hygiene, documentation and skills

> Design section of `docs/specs/2026-07-25-agent-memory-safety-ui-design.md`.
> Global constraints (PII, production safety, quality gates, style) are inherited from
> section 4 of that document and are not restated here.

## Repository hygiene, CI, documentation, and skills

### 1. Goal

`github.com/mshtawythug/second-brain` is a public, MIT, PyPI-published project (`secondbrain-py` 0.2.1) whose README advertises a **CI** badge — but no workflow in the repo ever runs `ruff check`, `mypy src/`, or `pytest`. A contributor can open a PR that breaks the type checker, drops coverage below the 85% floor, or deletes a test, and every check on the PR will still be green. This section closes that: a real `ci.yml` quality gate wired to the same AGE-test-Postgres pattern the existing eval/benchmark workflows already use, a truthful badge, the four standard OSS files a public repo is expected to carry (`SECURITY.md`, `CODE_OF_CONDUCT.md`, `.github/dependabot.yml`, `.github/CODEOWNERS`), a `## [Unreleased]` CHANGELOG section, a surgical README delta, three new agent skills covering the proactivity/synthesis suite and `brain ask`/`brain audio`, and the doc pages those skills and files link to. Outcome: a PR to this repo is *mechanically* held to the standard CLAUDE.md and `PULL_REQUEST_TEMPLATE.md` already claim, and a first-time visitor finds the files GitHub's community-standards checklist looks for.

---

### 2. Current state

#### A. CI — verified, survey is correct

`.github/workflows/` contains exactly five files:

| File | `name:` | What it runs |
|---|---|---|
| `benchmark.yml` | `benchmark` | `pytest -m benchmark --no-cov -v` (`benchmark.yml:88`) |
| `eval.yml` | `eval` | `pytest -m eval --no-cov -v` (`eval.yml:89`) |
| `publish-age-image.yml` | — | GHCR multi-arch AGE image build |
| `release.yml` | — | `v*` tag → GitHub Release + PyPI Trusted Publishing (`release.yml:16-20`, `release.yml:84`) |
| `traffic-stats.yml` | — | daily traffic snapshot |

**Nothing runs the default `pytest` invocation, `ruff check`, or `mypy src/`.** Both `eval.yml:89` and `benchmark.yml:88` deliberately pass `--no-cov`, so neither exercises the coverage floor either. Confirmed by `grep -rn "uses: " .github/workflows/*.yml` and reading all five in full.

The badge at `README.md:8` is:

```markdown
[![CI](https://github.com/mshtawythug/second-brain/actions/workflows/eval.yml/badge.svg)](https://github.com/mshtawythug/second-brain/actions/workflows/eval.yml)
```

**Correction to the survey:** the badge is not *broken* — `eval.yml` exists, so the badge renders and links somewhere real. It is **mislabeled**: it says "CI" but reports the status of a workflow that (per its own header comment, `eval.yml:11-14`) "SKIP[s] cleanly when Ollama / a curated corpus is unavailable, so this job's value in CI is import/collection regression coverage." A green "CI" badge today means "the eval harness still imports" — not "lint, types, and tests pass."

Established quality-gate baseline (I ran these):

- `ruff check` → **`All checks passed!`**, exit 0.
- `mypy src/` → **`Success: no issues found in 158 source files`**.
- `pytest --collect-only -q` → **5747 tests collected, 100 deselected** in 4.06s.

So the gate is green *today*; CI is codifying a standard already met, not chasing a red build.

`pyproject.toml:131`:

```toml
addopts = "-ra --strict-markers --cov=brain --cov-report=term-missing --cov-fail-under=85 -m 'not eval and not benchmark and not e2e and not phase3'"
```

Four markers are excluded by default (`pyproject.toml:132-186` documents each): `e2e` (needs `npx quartz` + a live vault), `benchmark` (50k-entity graph load), `eval` (live corpus + Ollama), `phase3` (needs optional migration 021). `live_db` (`pyproject.toml:149`) and `integration` (`pyproject.toml:168`) are **not** excluded — they run in the default suite and self-skip. **(Superseded 2026-08-07, C7: `live_db` IS now excluded — it asserted ranking against the operator's live production corpus, so it reported machine conditions rather than code. Run it deliberately with `pytest -m live_db --no-cov`. `integration` and `live_ollama` remain selected.)**  There is no `[tool.coverage]` section; `--cov=brain` + `--cov-fail-under=85` is the whole configuration.

Ollama-dependent tests inside the default suite skip cleanly, verified at each call site: `tests/test_search_canary_queries.py:112,114`, `tests/test_search_floor_default_excludes_known_bad.py:110,121`. They reach for the *prod* corpus via `tests/conftest.py:75` `prod_database_url()`, which — with no `BRAIN_PROD_DATABASE_URL` and no repo `.env` (both true on a CI runner) — falls back to `_DEFAULT_PROD_DB_URL = "postgresql://brain:brain@localhost:55432/second_brain"` (`tests/conftest.py:72`). That port is closed in CI, so they skip on connection failure rather than fail. Every Docker interaction in the default suite is mocked (`tests/test_demo_core.py:177-192`, `tests/test_setup.py:128-162`); no default-suite test shells out to a real `docker`.

`tests/conftest.py:26-59` hard-refuses to run the destructive schema reset against the prod database (ports 5433/55432, or dbname `second_brain`), aborting at collection time. CI must therefore point `TEST_DATABASE_URL` at port 5434 / `second_brain_test`, exactly as `eval.yml:53-54` already does.

#### B. Standard OSS files — verified absent

```
ABSENT: SECURITY.md
ABSENT: CODE_OF_CONDUCT.md
ABSENT: .github/dependabot.yml
ABSENT: .github/CODEOWNERS
ABSENT: .github/FUNDING.yml
```

Present already: `LICENSE` (MIT), `CONTRIBUTING.md` (13 KB), `.github/PULL_REQUEST_TEMPLATE.md`, `.github/ISSUE_TEMPLATE/{bug_report.md,feature_request.md,config.yml}`. `.github/ISSUE_TEMPLATE/config.yml` carries a deliberate commented-out Discussions link with a "do not link a surface that 404s" rationale — that discipline is the house style and I follow it below.

Dependabot-relevant manifests: one `pyproject.toml` (root), five workflow files under `.github/workflows/`, and exactly one first-party `Dockerfile` at `src/brain/templates/docker/age/Dockerfile` (the other hits from `find` are `build/` and `.claude/worktrees/` copies, both gitignored — `.gitignore:12`, `.gitignore:15`). Currently-pinned actions: `actions/checkout@v4`, `actions/setup-python@v5`, `docker/{setup-qemu,setup-buildx,login,metadata}-action@v3/v5`, `docker/build-push-action@v6`, `pypa/gh-action-pypi-publish@release/v1`.

#### C. CHANGELOG — verified

`CHANGELOG.md` is Keep-a-Changelog 1.1.0 + SemVer (`CHANGELOG.md:5-6`), with the dual-axis explainer at `CHANGELOG.md:8-21` (`v*` = the CLI/`secondbrain-py`; `age-image-*` = the GHCR AGE image). Head entry is `## [0.2.1] - 2026-07-20` (`CHANGELOG.md:23`). **There is no `## [Unreleased]` section.**

Merged-but-unlogged work since the last tag (`git log --oneline v0.2.1..HEAD`):

```
1cfd23d2 fix(stats): make traffic snapshot truly best-effort + rebase-before-push + latest-by-date
c9a52e5  ci(stats): daily traffic snapshot workflow + history
163f158  docs(readme): add live PyPI + GitHub stats badges
```

**Additional finding the survey missed:** `git tag --list` returns only `v0.2.0` and `v0.2.1`. The link reference at `CHANGELOG.md:80` — `[0.1.0]: …/releases/tag/v0.1.0` — points at a tag that does not exist, so that link 404s for anyone who clicks it.

#### D. README — verified

`README.md` is 155 lines. GIF usage: `usage.gif` (`README.md:18`), `demo.gif` (`README.md:46`), `mcp.gif` (`README.md:114`). The other four tracked GIFs (`ask`, `graphrag`, `proactivity`, `wiki`) are used from `docs/`. All 7 are tracked (`git ls-files docs/assets/`); note `wiki.gif` has **no** companion `.tape` — the other six do.

Command groups the README **does not** mention: `init`, `doctor`, `status`, `analyze`, `ingest-stdin`, `ingest-gmail`, `reembed`, `timeline`, `enrich`, `explain`, `eval`, `rate`, `todo`, `brief`, `ask`, `audio`, `resurface`, `edit`, `rm`, `mark-draft`, `mark-published`, `daily`, `backlinks`, `links`, `orphans`, `graph`, `people`, `uninstall`, and every sub-app (`vault`, `note`, `backfill`, `owner`, `wiki`, `claude`, `graphrag`, `elicit`, `capture`, `review`, `connect`, `gaps`). That omission is **deliberate and correct** — `README.md:106` routes the reader to `docs/cli-reference.md`, and `docs/cli-reference.md` (534 lines) already documents `setup`, `demo`, ingest options, search diagnostics, tags/edit/draft/rm, Gmail, status/health, `capture`, `enrich`, `rate`, `people`, `elicit`, and all eight proactivity commands with per-command sections at lines 340-534. The README does not need feature text; it needs three small truth-fixes.

#### E. Skills — verified

`skills/` has seven directories, each containing a single `SKILL.md` and nothing else:

| Skill | Lines | Covers |
|---|---|---|
| `brain-authoring` | 207 | `note new` / `daily` / `note rename` / `edit` / `tag` / `mark-draft` |
| `brain-graph` | 241 | `graphrag search/themes/entity/entities/stats` |
| `brain-maintenance` | 224 | `doctor` / `status` / `analyze` / `reembed` / `init` / `backfill` / `vault sync-summaries` / `relink-derived` |
| `brain-todo` | 140 | `todo` + Krisp action-item deferred fetch |
| `consult-brain` | 215 | `search` / `show` / `explain` / `rate` |
| `elicit-brain` | 149 | `elicit` / `elicit list` |
| `ingest-brain` | 170 | `ingest` / `ingest-dir` / `ingest-stdin` / `ingest-gmail` |

House format, confirmed across all seven: YAML frontmatter with exactly `name:` and a folded `description: >` block; the description ends with a literal `MANDATORY TRIGGERS:` sentence listing comma-separated natural-language phrases; the description cross-references sibling skills to disambiguate routing (`consult-brain` frontmatter: "For themes / patterns / connections across interactions, use `brain-graph` instead."). Body: an H1 matching the skill's human name, then H2 sections; typically a "when this fires / pick the right tool" routing section, fenced `bash` recipes with inline `#` comments, a flag table, an error/troubleshooting section, and a closing "Safety rules" or "Operational notes" H2. Length 140-241 lines. Tone: imperative, second-person-to-the-agent, with explicit prohibitions in bold ("**Do NOT make up MCP parameter syntax**", `skills/brain-todo/SKILL.md:75`).

**Uncovered features** (survey correct): `brain brief`, `resurface`, `review`, `timeline`, `connect`, `gaps`, `capture`, `ask`, `audio`. Also uncovered and unmentioned by the survey: `brain rate` is covered inside `consult-brain` Step 4 (`skills/consult-brain/SKILL.md:120`), so it needs no new skill.

**The two installers behave differently — this matters and is not documented anywhere:**

- **`bin/brain-skills-sync`** (96 lines, bash) enumerates *every immediate subdirectory of the repo `skills/`* at `bin/brain-skills-sync:36-41` — "never a hardcoded (and therefore drift-prone) list" — and `cp -R`s each into `$BRAIN_SKILLS_DEST` (default `~/.claude/skills`), wholesale-replacing on drift so repo deletions propagate (`bin/brain-skills-sync:78-81`). It installs **all seven** skills under their own names. `--check` reports drift and exits 1.
- **`brain claude install-skill`** (`src/brain/cli.py:9213`, implemented in `src/brain/cli_claude.py`) does something entirely different: it reads **one** file from package data — `resource_files("brain.templates.skill") / "SKILL.md"` (`src/brain/cli_claude.py:40`) — and writes it to `~/.claude/skills/brain/SKILL.md` (`src/brain/cli_claude.py:15-17`). That packaged file is 63 lines and uses a *different* frontmatter shape (`description:` + `when_to_use:`, no `name:`, no `MANDATORY TRIGGERS:`). It is a single condensed CLI cheat-sheet for pip/pipx users, shipped via `pyproject.toml:104` (`"brain.templates.skill" = ["*.md"]`). It **does not** read `skills/` and is unaffected by anything added there.

**Consequence for this design: a new skill dropped into `skills/<name>/SKILL.md` needs no registration anywhere.** `brain-skills-sync` picks it up automatically, and `tests/test_brain_skills_sync.py:26-27` enumerates the same way (`{p.name for p in SRC_SKILLS.iterdir() if p.is_dir()}`), so the existing tests auto-cover it. Nothing needs to change in `pyproject.toml`, and `brain claude install-skill` is out of scope.

#### F. Docs — verified

`.gitignore:45-59` is a private-by-subdir allowlist. Ignored: `docs/plans/`, `docs/audits/`, `docs/specs/`, `docs/notes/`, `docs/qa/`, `docs/summaries/`, `docs/launch/`, plus two named files. `git ls-files docs/` returns exactly:

```
docs/README.md  docs/cli-reference.md  docs/configuration.md  docs/graphrag.md
docs/vault-and-wiki.md  docs/guides/claude-desktop-setup.md
docs/assets/{ask,demo,graphrag,mcp,proactivity,usage,wiki}.gif + 6 .tape files
```

Because the ignore list names *directories* rather than globbing `docs/*`, **a new top-level `docs/*.md` file is tracked by default** — no `.gitignore` change is needed for any new page placed at `docs/<name>.md` or under `docs/guides/`.

#### G. One drift finding to fix in passing

`docker-compose.age-test.yml:6` states "Prod stays on stock pgvector on **port 5433**". Prod is on **55432** (`docker ps` → `second-brain-postgres 0.0.0.0:55432->5432`; `tests/conftest.py:72`; `README.md:85`). 5433 is the *historical* mapping, still refused defensively by `tests/conftest.py:26`. One-line comment fix.

---

### 3. User-visible surface

This section adds no Python and no CLI flags. Its user-visible surface is (a) GitHub check names on a PR, (b) the README badge, (c) new repository files, (d) three new agent skills.

#### 3.1 CI check names (what a contributor sees on a PR)

```
✓ ci / Lint and types (ruff + mypy)          38s
✓ ci / Tests (pytest, AGE Postgres)          11m 24s
✓ eval / Retrieval eval gate                  2m 51s
✓ benchmark / GraphRAG P95 perf gate (AGE)   18m 02s
```

Two jobs, not one: `lint` needs no Docker and finishes in well under a minute, so a formatting or typing mistake fails fast and cheap instead of after an 11-minute container+suite run. Both live in one workflow (`ci.yml`) so a single badge covers both.

Literal step output the implementer should expect from the `test` job:

```
============================= test session starts ==============================
platform linux -- Python 3.11.x, pytest-8.3.x, pluggy-1.5.x
rootdir: /home/runner/work/second-brain/second-brain
configfile: pyproject.toml
testpaths: tests
plugins: cov-5.0.0, mock-3.14.0
collected 5847 items / 100 deselected / 5747 selected
...
---------- coverage: platform linux, python 3.11.x -----------
TOTAL                                           18349   2500    86%
Required test coverage of 85% reached. Total coverage: 86.xx%
========== 5680 passed, 67 skipped, 100 deselected in 5xx.xxs ==========
```

The skip count is non-zero **by design** — the `live_db` canaries and any Ollama-dependent default-suite tests skip on an Ollama-less, prod-DB-less runner. A CI run with **zero** skips would mean something reached a live service it should not have.

#### 3.2 Badge change (`README.md:8`)

Before (points at the eval harness, labeled "CI"):
```markdown
[![CI](https://github.com/mshtawythug/second-brain/actions/workflows/eval.yml/badge.svg)](https://github.com/mshtawythug/second-brain/actions/workflows/eval.yml)
```

After (points at the real quality gate):
```markdown
[![CI](https://github.com/mshtawythug/second-brain/actions/workflows/ci.yml/badge.svg)](https://github.com/mshtawythug/second-brain/actions/workflows/ci.yml)
```

**Backward-compatibility risk:** a workflow badge 404s (renders "no status") until the workflow has run at least once on the repo's default branch. The `ci.yml` file and the badge edit must therefore land in the **same** commit/PR, and the badge will show "no status" for the few minutes between merge and the first `push`-triggered run on `master`. That is acceptable and self-healing; the alternative (badge first, workflow later) leaves a visibly broken badge on the public README. Do **not** delete the eval badge — there isn't one; `eval.yml` was only ever surfaced under the "CI" label, so this is a relabel, not a removal.

#### 3.3 New files (user-visible on the repo landing page)

`SECURITY.md`, `CODE_OF_CONDUCT.md`, `.github/dependabot.yml`, `.github/CODEOWNERS`. GitHub surfaces the first two in the **Security** tab and the community-standards checklist respectively; `CODEOWNERS` adds automatic review requests; `dependabot.yml` produces update PRs.

#### 3.4 New skills

Three new directories under `skills/`, picked up automatically by `bin/brain-skills-sync` (whose output gains three lines):

```
$ bin/brain-skills-sync
  brain-ask: installed
  brain-authoring: unchanged
  brain-graph: unchanged
  brain-maintenance: unchanged
  brain-proactivity: installed
  brain-todo: unchanged
  brain-capture: installed
  consult-brain: unchanged
  elicit-brain: unchanged
  ingest-brain: unchanged
done: 3 installed, 0 updated, 6 unchanged → /Users/you/.claude/skills
```

**Backward-compatibility risk:** `bin/brain-skills-sync --check` exits **1** on any drift, and "not installed" counts as drift (`bin/brain-skills-sync:54-55`). Anyone with the seven current skills already synced will get `brain-ask: MISSING (not installed)` and a non-zero exit until they re-run the sync. That is the script's intended contract (it is how it reports "the repo moved ahead of you"), so no code change is warranted — but `CONTRIBUTING.md` and the release notes must say "run `bin/brain-skills-sync` after upgrading."

No JSON surface changes. No CLI output changes.

---

### 4. Module layout

| Path | New/changed | Purpose | Est. lines |
|---|---|---|---|
| `.github/workflows/ci.yml` | **new** | Quality gate: ruff + mypy job, pytest-on-AGE-Postgres job | 110 (≈45 comment header in house style, matching `eval.yml:1-33`) |
| `SECURITY.md` | **new** | Supported versions, private reporting, response SLA, local-first threat model | 75 |
| `CODE_OF_CONDUCT.md` | **new** | Contributor Covenant 2.1, verbatim, with a GitHub-private-reporting enforcement contact | 130 |
| `.github/dependabot.yml` | **new** | pip + github-actions + docker ecosystems, weekly, grouped | 60 (incl. comments) |
| `.github/CODEOWNERS` | **new** | Global owner + narrowed owners for migrations/workflows/skills | 20 |
| `CHANGELOG.md` | changed | Insert `## [Unreleased]` after line 21; fix the dead `[0.1.0]` link at line 80 | +30 / −1 |
| `README.md` | changed | Badge retarget (line 8) + 2 doc-index lines + 1 skills line | +4 / −1 |
| `docs/README.md` | changed | Two new rows in the doc table + two Project-docs bullets | +4 |
| `docs/cli-reference.md` | changed | No new sections needed; add skill cross-refs to existing `## brain capture` and `## Proactivity and synthesis` | +6 |
| `docs/agent-skills.md` | **new** | Which skill covers what; `brain-skills-sync` vs `brain claude install-skill`; how to add one | 110 |
| `CONTRIBUTING.md` | changed | New "Continuous integration" subsection; `bin/brain-skills-sync` step | +25 |
| `skills/brain-proactivity/SKILL.md` | **new** | `brief` / `resurface` / `review` / `timeline` / `connect` / `gaps` | 200 |
| `skills/brain-ask/SKILL.md` | **new** | `ask` (cited synthesis) + `audio` (two-host overview) | 165 |
| `skills/brain-capture/SKILL.md` | **new** | `capture` / `capture list` / `capture review` | 130 |
| `docker-compose.age-test.yml` | changed | Fix the "port 5433" prod comment (line 6) → 55432 | +1 / −1 |
| `tests/test_ci_workflow.py` | **new** | Static assertions over `ci.yml` (see §8) | 130 |
| `tests/test_repo_hygiene_files.py` | **new** | Presence/shape of SECURITY/CoC/dependabot/CODEOWNERS/CHANGELOG | 150 |
| `tests/test_skill_frontmatter.py` | **new** | House-format contract over **every** `skills/*/SKILL.md` | 140 |

Every new `.py` file gets a module docstring, full type hints, and `pathlib.Path`. No file approaches 800 lines. **No migration is required by this section** — it adds no schema, no columns, no SQL. Migrations `024_agent_attribution.sql` and `025_document_sensitivity.sql` belong to the AGENT-MEMORY and SAFETY sections respectively and are not touched here.

---

### 5. Design detail

#### 5.1 `.github/workflows/ci.yml`

Reuses the `eval.yml` pattern verbatim where it is already right, and diverges only where the eval gate's choices are wrong for a quality gate.

```yaml
name: ci

on:
  pull_request:
  push:
    branches: [main, master]
  workflow_dispatch:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

permissions:
  contents: read

jobs:
  lint:
    name: Lint and types (ruff + mypy)
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
          cache-dependency-path: pyproject.toml

      - name: Install package (with dev extras)
        run: pip install -e ".[dev]"

      - name: ruff check
        run: ruff check

      - name: mypy src/
        run: mypy src/

  test:
    name: Tests (pytest, AGE Postgres)
    runs-on: ubuntu-latest
    timeout-minutes: 30

    env:
      DATABASE_URL: postgresql://brain:brain@localhost:5434/second_brain_test
      TEST_DATABASE_URL: postgresql://brain:brain@localhost:5434/second_brain_test

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
          cache-dependency-path: pyproject.toml

      - name: Pull the pinned AGE image (build fallback if the registry is down)
        run: docker compose -f docker-compose.age-test.yml pull --quiet || true

      - name: Start the AGE test instance
        run: docker compose -f docker-compose.age-test.yml up -d

      - name: Install package (with dev extras)
        run: pip install -e ".[dev]"

      - name: Wait for the AGE instance to be ready
        run: |
          for i in $(seq 1 60); do
            if pg_isready -h localhost -p 5434 -U brain -d second_brain_test; then
              echo "AGE instance is ready"
              exit 0
            fi
            echo "waiting for AGE instance ($i/60)..."
            sleep 2
          done
          echo "AGE instance did not become ready in time" >&2
          docker compose -f docker-compose.age-test.yml logs --tail=50
          exit 1

      - name: Run the quality gate (default markers + 85% coverage floor)
        run: pytest

      - name: Tear down the AGE test instance
        if: always()
        run: docker compose -f docker-compose.age-test.yml down -v
```

Decisions, each with its reason:

1. **Triggers / concurrency copied from `eval.yml:36-44`** — `pull_request` + `push: [main, master]` + `workflow_dispatch`; group `ci-${{ github.ref }}`; `cancel-in-progress` only on PRs so master keeps a complete audit trail. Consistency across four workflows beats novelty.
2. **`permissions: contents: read`** — a hardening improvement over `eval.yml`/`benchmark.yml`, which declare no `permissions` block and therefore inherit the repo default. Nothing here writes; `release.yml:22` already demonstrates the house pattern of declaring least privilege.
3. **Pull-before-up, not `up -d --build`.** `eval.yml:69` and `benchmark.yml:66` use `up -d --build`, which compiles Apache AGE from C source on every run — minutes of wall clock. `eval.yml:68` even carries `# TODO flip to pull-first once ghcr.io/mshtawythug/second-brain-age is public`. `ci.yml` runs on **every** PR, so it is the workflow that most needs the fast path. Compose v2 with both `image:` and `build:` (`docker-compose.age-test.yml:24-27`) will **build** rather than pull on a bare `up`, so the explicit `pull --quiet || true` step is required; `|| true` preserves the local-build fallback the compose file's comment promises (`docker-compose.age-test.yml:18-23`) if GHCR is unreachable or the image is still private. **Do not** retrofit this into eval/benchmark in this PR — one change per PR; log it as a follow-up.
4. **`pytest` with no flags.** The default `addopts` (`pyproject.toml:131`) already excludes `eval`, `benchmark`, `e2e`, `phase3` and already enforces `--cov=brain --cov-fail-under=85`. Passing extra flags would silently diverge CI from what a contributor runs locally, which is the exact failure mode CI exists to prevent. In particular do **not** copy `--no-cov` from `eval.yml:89` — the coverage floor is the point.
5. **Marker exclusions, restated for the workflow header comment:** `e2e` needs a Node toolchain plus the live `~/brain-vault/.quartz/` workspace and is non-hermetic (`pyproject.toml:136-142`); `benchmark` loads a 50k-entity/1M-mention graph and is gated in its own workflow (`pyproject.toml:155-162`); `eval` needs a live corpus + Ollama and is gated in `eval.yml` (`pyproject.toml:150-154`); `phase3` needs the optional migration-021 `doc_date` column so v1/v2 databases stay green (`pyproject.toml:169-174`). `live_db` and `integration` are intentionally *not* excluded — `integration` needs only the port-5434 container CI already starts, and `live_db` self-skips. **(Superseded 2026-08-07, C7: `live_db` IS now excluded — it asserted ranking against the operator's live production corpus, so it reported machine conditions rather than code. Run it deliberately with `pytest -m live_db --no-cov`. `integration` and `live_ollama` remain selected.)** 
6. **Ollama is not installed and must not be.** Installing a model in CI would add ~1 GB of download and minutes of warmup for tests that are explicitly designed to skip. The contract is: any test needing Ollama either carries the `eval` marker (excluded) or calls `pytest.skip()` on the connection failure — as `tests/test_search_canary_queries.py:112` and `tests/test_search_floor_default_excludes_known_bad.py:121` do. `tests/test_skill_frontmatter.py` is not the place to enforce this; §8 adds an explicit regression test instead.
7. **Timing.** `lint` ≈ 40-90 s (pip install dominates; `cache: pip` keyed on `pyproject.toml` per `eval.yml:64-65` makes repeat runs fast). `test` ≈ 30-60 s image pull + 20 s Postgres readiness + ~9 min suite (the suite is recorded at ~530 s post-truncate-reset optimization) ≈ **11-13 min**, `timeout-minutes: 30` giving 2× headroom for a cold cache or a registry-miss local build.

#### 5.2 `SECURITY.md`

```markdown
# Security Policy

## Supported versions
| Version | Supported |
|---|---|
| 0.2.x   | ✅ Fixes land on the latest 0.2.x release. |
| 0.1.x   | ❌ Please upgrade. |
Pre-1.0: only the latest minor line receives security fixes.

## Reporting a vulnerability
Please do NOT open a public issue.
Use GitHub's private vulnerability reporting:
Security tab → "Report a vulnerability"
(https://github.com/mshtawythug/second-brain/security/advisories/new)

## What to expect
- Acknowledgement: within 7 days.
- Assessment + remediation plan: within 30 days.
- Fix + advisory: coordinated disclosure; credit given unless you decline.
Best-effort, maintained by one person in their own time. No bug bounty.

## Security model — what this project is
[threat model, below]

## Out of scope
[list, below]
```

**Reporting channel: GitHub private vulnerability reporting, not an email address.** Rationale (this is a hard constraint, not a preference): CLAUDE.md rule 15 forbids committing PII, and a maintainer's personal email address in a public file is exactly that. It is also unverifiable — I must not invent one, and there is no maintainer email anywhere in the tracked tree to copy (`git ls-files | xargs grep` for an `@` address in a maintainer role returns nothing; `pyproject.toml` has no `authors` field with an email, only `[project.urls]` at lines 67-70). GitHub private reporting requires no address to be published, routes to whoever has repo admin, and gives a built-in advisory + CVE workflow. **Implementation prerequisite:** private vulnerability reporting must be enabled in *Settings → Code security → Private vulnerability reporting* before the advisory URL resolves — the same "do not link a surface that 404s" discipline `.github/ISSUE_TEMPLATE/config.yml` already applies to Discussions. If the maintainer has not enabled it at merge time, ship `SECURITY.md` with the toggle instruction as a TODO comment and the link commented out, exactly as `config.yml` does.

**Threat model section content** (this is what makes the file useful rather than boilerplate for a local-first tool):

- **Everything is local by default.** The corpus lives in a Postgres container bound to `127.0.0.1:55432` (`docker-compose.yml`), with credentials `brain:brain`. That is intentional for a single-user local database and is **not** a vulnerability report we can act on — but it *is* a real risk if a user changes the port binding to `0.0.0.0` or exposes the host. Say so plainly.
- **The optional wiki binds an HTTP port.** `brain wiki install` + Caddy serve the rendered vault (`docs/vault-and-wiki.md`). It has **no authentication**. Anyone who can reach that port reads the entire personal corpus. Users must bind it to loopback or put it behind their own auth; do not expose it to a LAN or the internet.
- **`brain-mcp` is a stdio server** (`pyproject.toml:54`) with no network listener; its trust boundary is the agent that spawns it. An agent with MCP access to the brain can read every non-draft document.
- **Secrets** — `.env` is gitignored (`.gitignore:1`); `VOYAGE_API_KEY` is the only outbound-service credential, and only when `BRAIN_EMBEDDER=voyage`. Default `arctic` and `none` backends make zero outbound calls.
- **Data egress** — with the default local Ollama backend, no document content leaves the machine. With `voyage`, chunk text is sent to Voyage AI. State this so users can reason about it.
- **In scope:** SQL injection, path traversal in ingest/vault writes, arbitrary code execution from a crafted PDF/DOCX/Markdown document, secrets leaked into logs or the vault, the MCP server returning documents marked draft/sensitive.
- **Out of scope:** the default `brain:brain` local Postgres credentials; anything requiring an attacker to already have shell access as the user; user-initiated exposure of the wiki port; third-party model/registry supply chain.

#### 5.3 `CODE_OF_CONDUCT.md`

Contributor Covenant **2.1**, verbatim (the canonical text — do not paraphrase; it is CC BY 4.0 and the attribution footer must be kept). One substitution: the `[INSERT CONTACT METHOD]` placeholder in the Enforcement section becomes "a private report via the repository's **Security** tab → *Report a vulnerability*, or a direct message to the repository owner on GitHub (`@mshtawythug`)." Same reasoning as §5.2 — no personal email committed, and the GitHub handle is already public in every repo URL in the tree (`README.md:8`, `pyproject.toml:68`), so it is not new PII.

#### 5.4 `.github/dependabot.yml`

```yaml
version: 2
updates:
  # 1. Python dependencies declared in the root pyproject.toml.
  - package-ecosystem: pip
    directory: "/"
    schedule:
      interval: weekly
      day: monday
      time: "06:00"
      timezone: "America/Los_Angeles"
    open-pull-requests-limit: 5
    commit-message:
      prefix: "chore"
      include: scope
    groups:
      dev-dependencies:
        patterns: ["pytest*", "ruff", "mypy", "coverage", "types-*", "reportlab"]
      runtime-minor-patch:
        update-types: ["minor", "patch"]
    ignore:
      # Pinned deliberately in pyproject.toml:50 — a major bump breaks the
      # pytest-cov integration. Unpin there first, then drop this ignore.
      - dependency-name: "coverage"
        update-types: ["version-update:semver-major"]

  # 2. The five workflow files under .github/workflows/.
  - package-ecosystem: github-actions
    directory: "/"
    schedule:
      interval: weekly
      day: monday
    open-pull-requests-limit: 3
    commit-message:
      prefix: "ci"
    groups:
      actions:
        patterns: ["*"]

  # 3. The Apache AGE base image. Note the non-obvious directory: the single
  #    canonical Dockerfile lives INSIDE the package so wheels ship it
  #    (pyproject.toml:102), and docker-compose.age-test.yml:25 builds from
  #    there. build/ and .claude/worktrees/ copies are gitignored and invisible
  #    to Dependabot.
  - package-ecosystem: docker
    directory: "/src/brain/templates/docker/age"
    schedule:
      interval: monthly
    open-pull-requests-limit: 2
    commit-message:
      prefix: "chore"
```

Design notes: **weekly** for pip and actions (the project has one maintainer; daily would be noise), **monthly** for docker (the AGE image is version-pinned to `pg16-v1.5.0-rc0-pgv0.8.2` and a base-image bump is a deliberate, tested event — see `CHANGELOG.md:16-21` on the separate `age-image-*` tag axis). **Grouping** collapses the dev-tooling churn into one PR instead of eight. **PR limits** 5/3/2 cap the queue at ten. `commit-message.prefix` matches the conventional-commit types the PR template requires (`.github/PULL_REQUEST_TEMPLATE.md`, "Conventional commits" checklist item). Every Dependabot PR will now run the new `ci.yml` — which is precisely the point: automated dependency bumps are only safe once a real gate exists.

#### 5.5 `.github/CODEOWNERS`

```
# Default owner for everything.
*                               @mshtawythug

# Schema is append-only and forever (CLAUDE.md "Migration Safety") — an edit to
# an already-shipped migration is a data-loss bug, so review is never optional.
/src/brain/migrations/          @mshtawythug

# CI, release, and publish workflows hold the PyPI Trusted Publishing and GHCR
# credentials paths.
/.github/workflows/             @mshtawythug

# Agent-facing skills ship behavior to every Claude Code user who runs
# bin/brain-skills-sync.
/skills/                        @mshtawythug

# Security policy and the PII gate's committed allowlist.
/SECURITY.md                    @mshtawythug
/.pii-allowlist.txt             @mshtawythug
```

With a single maintainer, the narrowed rules are redundant *today* but are the documentation of intent that makes adding a second maintainer a one-line change rather than an archaeology exercise. `CODEOWNERS` only enforces anything when branch protection has "Require review from Code Owners" enabled — that is a repo-settings action, listed in §9.

#### 5.6 `CHANGELOG.md` — the `## [Unreleased]` section

Inserted immediately after the dual-axis explainer (after `CHANGELOG.md:21`), before `## [0.2.1]`:

```markdown
## [Unreleased]

### Added

- **Continuous-integration quality gate** (`.github/workflows/ci.yml`) — every
  pull request and every push to `main`/`master` now runs `ruff check`,
  `mypy src/`, and the full `pytest` suite (with its 85% coverage floor)
  against the pinned Apache AGE Postgres test instance. The README's **CI**
  badge now reports this gate; previously it reported the eval harness.
- `SECURITY.md` — supported versions, private vulnerability reporting via
  GitHub's Security tab, response expectations, and the security model of a
  local-first tool (local database, optional unauthenticated wiki port,
  stdio MCP server).
- `CODE_OF_CONDUCT.md` — Contributor Covenant 2.1.
- `.github/dependabot.yml` — weekly grouped updates for Python dependencies and
  GitHub Actions, monthly for the Apache AGE base image.
- `.github/CODEOWNERS` — review routing, with migrations, workflows, and
  `skills/` called out explicitly.
- Three new Claude Code skills under `skills/`, installed by
  `bin/brain-skills-sync`: **`brain-proactivity`** (`brief` / `resurface` /
  `review` / `timeline` / `connect` / `gaps`), **`brain-ask`** (`ask` cited
  synthesis and `audio` overviews), and **`brain-capture`** (the quick-capture
  inbox). Run `bin/brain-skills-sync` after upgrading to install them.
- `docs/agent-skills.md` — which skill covers which commands, and the
  difference between `bin/brain-skills-sync` (all repo skills) and
  `brain claude install-skill` (the single packaged CLI cheat-sheet).
- Daily repository-traffic snapshot workflow and history
  (`.github/workflows/traffic-stats.yml`), plus live PyPI and GitHub stats
  badges in the README.

### Fixed

- Repository-traffic snapshot is now genuinely best-effort: it rebases before
  pushing and picks the latest entry by date, so a transient API failure or a
  concurrent push no longer fails the workflow.
- `docker-compose.age-test.yml` header comment said prod ran on port 5433; prod
  is on 55432 (5433 is the historical mapping, still refused defensively by the
  test-suite prod guard).
- `CHANGELOG.md`'s `[0.1.0]` link pointed at a `v0.1.0` release tag that was
  never pushed.
```

The last three `### Added` bullets and both `### Fixed` traffic bullets cover the merged-but-unlogged commits `163f158`, `c9a52e5`, `1cfd23d2`. Two placeholder bullets are reserved for the sibling sections of this release — **agent attribution** (migration `024_agent_attribution.sql`) and **document sensitivity** (migration `025_document_sensitivity.sql`) — to be filled in by those sections' authors; this section must not describe features it did not design.

The dead-link fix at `CHANGELOG.md:80`: replace the `[0.1.0]` release-tag URL with the compare/commit-history URL that actually resolves, or drop the link definition and leave `## [0.1.0]` unlinked. **Recommended:** drop the link definition — inventing a tag reference is worse than no link, and back-dating a `v0.1.0` tag onto a commit chosen after the fact is revisionist.

Add at the bottom: `[Unreleased]: https://github.com/mshtawythug/second-brain/compare/v0.2.1...HEAD`.

#### 5.7 README — the minimal edit (net +4 / −1 lines)

Exactly four changes, no new sections, no new GIFs, no feature prose:

1. **Line 8, replace in place** — badge URL `eval.yml` → `ci.yml` (§3.2). Net 0.
2. **After line 106** (the existing "The full command surface … lives in the [CLI reference]" paragraph), append **one sentence** to that same paragraph — no new paragraph, no new heading:
   > Agents get the same surface through the bundled [skills](docs/agent-skills.md) — `bin/brain-skills-sync` installs them into `~/.claude/skills/`.

   Net +1.
3. **In the `## Documentation` list (lines 148-155)**, insert two bullets: `docs/agent-skills.md` after the CLI-reference bullet, and `SECURITY.md` after `CONTRIBUTING.md`:
   ```markdown
   - [Agent skills](docs/agent-skills.md) — the Claude Code skills that ship with the repo, and how to install them
   ...
   - [SECURITY.md](SECURITY.md) — security model and how to report a vulnerability
   ```
   Net +2.
4. **`CODE_OF_CONDUCT.md`** — append to the existing `CONTRIBUTING.md` bullet as a parenthetical rather than adding a fourth bullet: `— dev setup, codebase layout, running the tests (see also the [Code of Conduct](CODE_OF_CONDUCT.md))`. Net +1.

**What explicitly does NOT go in the README:** any description of `brief`/`resurface`/`review`/`timeline`/`connect`/`gaps`/`ask`/`audio`/`capture` — `docs/cli-reference.md:198-534` already documents all nine with examples, and `README.md:106` already routes there. Any new GIF — the seven tracked GIFs stay as-is, and `ask.gif`/`proactivity.gif`/`graphrag.gif`/`wiki.gif` remain used from `docs/` where they belong. Any CI-setup instructions — those go in `CONTRIBUTING.md`. This is the whole discipline that took the README from 1645 lines to 155; a +4 delta preserves it.

#### 5.8 The three new skills

Format follows the seven existing files exactly: `---` frontmatter with only `name:` and a folded `description: >` ending in a `MANDATORY TRIGGERS:` sentence; H1 = human name; H2 sections; fenced `bash` with `#` comments; a flag table; an error section; a closing safety/operational section. Each cross-references siblings for routing, and each explicitly disclaims the others' territory — that is how `consult-brain` and `brain-graph` avoid fighting today.

**`skills/brain-proactivity/SKILL.md`** (~200 lines)

```yaml
---
name: brain-proactivity
description: >
  The proactive half of the user's local second-brain — surface what's due for
  review, digest what just landed, synthesize across a week, flag
  contradictions and stale notes, suggest missing links, and show how a theme
  evolved over time. Use this skill when the user asks what they should look
  at, what happened recently, what they're forgetting, what's gone stale, what
  notes should be linked, or how something changed over time. Backs
  `brain brief`, `brain resurface`, `brain review`, `brain timeline`,
  `brain connect`, and `brain gaps`. For answering a specific question use
  `consult-brain`; for one cited multi-hop answer use `brain-ask`; for themes
  across a relationship use `brain-graph`.
  MANDATORY TRIGGERS: what should I look at, catch me up, my daily brief,
  today's digest, what did I miss, what's new in my brain, what should I
  review, resurface old notes, spaced repetition, weekly review, my week in
  review, review my week, what contradicts, contradictions in my notes,
  what's gone stale, stale notes, how did X evolve, over time, timeline of,
  suggest links, what should be linked, related notes I haven't linked,
  knowledge gaps, what am I not finding, failed searches.
---
```

Body outline: `# Brain Proactivity` → `## Which command answers which question` (a routing table: "what should I look at today" → `brief`; "what am I forgetting" → `resurface`; "what happened this week" → `review weekly`; "how did X change" → `timeline`; "what should link to what" → `connect`; "what can't my brain answer" → `gaps`) → one H2 per command with the exact invocations lifted from `docs/cli-reference.md:360-534` → `## Prerequisites and graceful degradation` (`timeline` needs `BRAIN_GRAPH_ENABLED` + `brain graphrag build` and prints a friendly note + exit 0 on an unknown entity; `review weekly --no-graph` falls back to tag clusters; contradiction scan is gated on `BRAIN_ELICIT_CONTRADICTION_ENABLED`, default off, and needs Ollama; `gaps` on a pre-migration-019 database prints a "run `brain init`" warning and searches keep working) → `## Safety rules` (`brief` surfaces **titles and todo texts only, never document bodies** — never paste a body the command did not print; `connect accept --write` **mutates a vault file** by appending under `## See Also`, so always confirm with the user before `--write`; `review dismiss` is irreversible from the CLI).

**`skills/brain-ask/SKILL.md`** (~165 lines)

```yaml
---
name: brain-ask
description: >
  Multi-hop, cited answer synthesis and audio overviews over the user's local
  second-brain. `brain ask` plans sub-queries, retrieves across iterations,
  and composes ONE answer with inline [N] citations back to the user's own
  documents; `brain audio` generates a two-host NotebookLM-style overview of a
  person or a topic. Use when the question needs several documents stitched
  together and an auditable answer, or when the user wants a listenable
  summary. For a single lookup or a quote use `consult-brain`; for themes and
  connections across a relationship use `brain-graph`.
  MANDATORY TRIGGERS: ask my brain properly, synthesize an answer, cited
  answer, with citations, multi-hop, what did we decide about, what did I
  learn about, pull it all together, connect the dots on, audio overview,
  podcast of my notes, notebooklm, two-host overview, narrate my notes,
  brain ask, brain audio.
---
```

Body outline: `# Brain Ask` → `## ask vs search vs graph` (`search` = ranked list you read; `ask` = one composed cited answer; `graph` = themes/structure) → `## brain ask` with the flag table from `docs/cli-reference.md:464-493` (`--explain`, `--no-loop`, `--mode hybrid|auto|fuse|local`, `-n`/`--limit`, `--max-iter`, `--json`) → `## Reading the citations` (inline `[N]` maps to documents; always offer `brain show <id>` for the underlying doc; **never** restate a claim the answer did not cite) → `## brain audio` (`--person` → graph `themes` mode; `--topic` → graph `global` mode, needs `brain graphrag communities build` first; exactly one required; writes `<out>.json` + `<out>.md` **before** TTS so artifacts survive a TTS failure; `--tts 'shell:…'`) → `## When it fails` (`ask` **requires** Ollama and exits non-zero with a clear message rather than returning a partial answer — do not retry in a loop, tell the user Ollama is down and offer `brain search` instead; the three graph modes require the Apache AGE image) → `## Safety rules` (only document **snippets** go to the LLM in `ask`, and only entity names + summaries in `audio` — never full bodies; **there is no MCP tool for `brain audio`**, it is CLI-only; generated audio artifacts land in the gitignored `audio/` directory (`.gitignore:71`) and must never be committed).

**`skills/brain-capture/SKILL.md`** (~130 lines)

```yaml
---
name: brain-capture
description: >
  Zero-friction quick capture into the user's local second-brain, and the
  inbox review loop that processes what was captured. Use this skill when the
  user wants to jot something down fast without deciding where it goes, dump a
  thought or idea mid-conversation, or later triage the inbox — promoting
  items into real notes, retagging, or discarding. Backs `brain capture`,
  `brain capture list`, and `brain capture review`. For ingesting an existing
  file, email, or transcript use `ingest-brain`; for authoring a full note use
  `brain-authoring`.
  MANDATORY TRIGGERS: capture this, jot this down, quick note, note to self,
  remember this, add to my inbox, dump this in my brain, capture idea, my
  inbox, review my inbox, process my inbox, triage my captures, what's in my
  inbox, brain capture.
---
```

Body outline: `# Brain Capture` → `## capture vs ingest vs note new` (capture = a thought with no home, always tagged `inbox`; ingest = existing content from a file or MCP; `note new` = a deliberate vault note with a title and a place) → `## Capturing` (`--text` and the stdin form, plus `-t/--tag` alongside the always-on `inbox` tag, from `docs/cli-reference.md:198-214`) → `## Reviewing the inbox` (`capture list`, then `capture review`) → `## Safety rules` — and this one carries a **prohibition drawn from a recorded incident**: `brain capture review` is interactive and destructive; `--limit` selects by inbox order, not by relevance, so **never pipe blind `d`/`y` responses into it**. Always show the user each item and let them decide. Doing otherwise has already deleted a real note.

#### 5.9 `docs/agent-skills.md` (~110 lines)

Tracked automatically (`.gitignore` ignores directories under `docs/`, not top-level `.md` files — §2F). Contents: (1) a table of all ten skills → the commands each covers → the sibling to prefer instead; (2) **"Two installers, two different things"** — the `bin/brain-skills-sync` vs `brain claude install-skill` distinction from §2E, with the concrete consequence that a dev-checkout user runs the former and a `pipx`/`uvx` user gets the latter; (3) "Adding a skill": create `skills/<name>/SKILL.md`, match the frontmatter contract, run `bin/brain-skills-sync`, verify with `bin/brain-skills-sync --check` — **no registration in `pyproject.toml` or anywhere else**; (4) the frontmatter contract as a checklist that mirrors `tests/test_skill_frontmatter.py` so the doc and the test cannot drift silently.

#### 5.10 Test-helper function signatures

The three new test modules share one helper each; all are pure and fully typed.

```python
# tests/test_skill_frontmatter.py
def parse_frontmatter(skill_md: Path) -> dict[str, str]:
    """Parse the leading YAML frontmatter block of a SKILL.md into a dict."""

# tests/test_ci_workflow.py
def load_workflow(name: str) -> dict[str, object]:
    """Load .github/workflows/<name> as parsed YAML."""

# tests/test_repo_hygiene_files.py
def read_repo_file(relative: Path) -> str:
    """Read a repo-root-relative text file, UTF-8."""
```

`pyyaml` is already a runtime dependency (`pyproject.toml:44`) and `types-PyYAML` a dev dependency (`pyproject.toml:50`), so `yaml.safe_load` is available and mypy-clean with no new dependency. **DRY note:** `tests/test_brain_skills_sync.py:26-27` already implements repo-skill enumeration; `tests/test_skill_frontmatter.py` imports and reuses that helper rather than duplicating the glob.

#### 5.11 SQL and error handling

**None.** This section adds no database access, no SQL, and no runtime Python — therefore no parameterized-query surface and no new `BrainError` subclass. The existing `SkillInstallError(BrainError)` at `src/brain/cli_claude.py:11` is unchanged and untouched. Failure handling lives in the workflow: every step is fail-fast (`run:` non-zero fails the job), and teardown is `if: always()` (`eval.yml:104`) so a failed suite never leaks a container.

---

### 6. Edge cases and failure modes

1. **The badge shows "no status" between merging `ci.yml` and its first run on the default branch.** Intended: the workflow's `push: branches: [main, master]` trigger fires on the merge commit, so the badge populates within ~12 minutes. Mitigation: ship the workflow and the badge edit in one commit; do not ship the badge first.

2. **GHCR pull fails (registry down, or the AGE image is still private).** `docker compose pull --quiet || true` swallows the failure; the subsequent `up -d` sees no local image and falls back to building from `src/brain/templates/docker/age/Dockerfile` per `docker-compose.age-test.yml:24-26`. The job gets slower (compiling AGE from C) but stays green. `timeout-minutes: 30` is sized for this path.

3. **Coverage lands below 85% in CI but above it locally,** because Ollama-dependent and `live_db` tests skip on the runner and their exercised lines go uncovered. Intended behavior: the job **fails**, loudly, on the first run — that is the gate working. The fix is **not** to lower `--cov-fail-under`. Before enabling required status checks, the implementer must reproduce CI conditions locally: stop Ollama (`brew services stop ollama`), stop the prod container so `prod_database_url()`'s 55432 fallback is refused, then run `pytest`. If coverage is short, the offending lines need hermetic unit tests with injected doubles (the `Embedder` Protocol and `brain.chat` already make this straightforward) — not a weakened floor.

4. **A contributor's fork has no access to GHCR or to repo secrets.** `ci.yml` uses no secrets and pulls only a public image, so `pull_request` runs from forks work unchanged. If the AGE image is still private at merge time, fork PRs silently take the build-from-source path (edge case 2) — slower, still correct. This is a reason to make the image public, tracked in §9.

5. **`docker compose … down -v` deletes the *test* volume `second-brain-age-test-postgres`.** Safe by construction: that compose file uses its own project name and a **named** volume (`docker-compose.age-test.yml:13,36,43-44`), never the prod bind-mount at `./data/postgres`. On a self-hosted runner (not used today) `down -v` would still only reach the test volume. Additionally `tests/conftest.py:57-59` aborts collection if `TEST_DATABASE_URL` ever resolves to a prod-looking host/port/dbname, so a misconfigured `env:` block fails fast instead of destroying data.

6. **Dependabot opens a PR that fails `ci.yml`.** Desired outcome — that is the entire value. The PR sits red until a human triages. `open-pull-requests-limit` (5/3/2) caps how many can pile up. A dependency whose major bump is known-breaking (e.g. `coverage<7.13`, pinned at `pyproject.toml:50`) is `ignore`d for majors with a comment naming the pin, so Dependabot does not re-propose it weekly.

7. **`bin/brain-skills-sync --check` starts failing for existing users** the moment the three new skills land, because "not installed" is drift (`bin/brain-skills-sync:53-55`). Intended contract, no code change. Mitigation: the CHANGELOG bullet and `CONTRIBUTING.md` both say "run `bin/brain-skills-sync` after upgrading," and the skills doc repeats it.

8. **A new skill's `MANDATORY TRIGGERS` collide with an existing skill's**, causing the agent to load the wrong one. Concrete risks identified: `brain-proactivity`'s "what should I review" vs `elicit-brain`'s gap queue; `brain-ask`'s "what did I learn about" vs `consult-brain`'s "what did I say about"; `brain-capture`'s "add to my brain" vs `ingest-brain`'s identical trigger. Mitigation: (a) `brain-capture` must **not** claim "add this to my brain" — that string is already `ingest-brain`'s (`skills/ingest-brain/SKILL.md` frontmatter) and is left to it; (b) every new skill's `description` names the sibling to prefer, matching the pattern at `skills/consult-brain/SKILL.md` frontmatter; (c) `tests/test_skill_frontmatter.py` asserts trigger-phrase uniqueness across all ten skills (§8) so a future collision fails CI.

9. **Overlap with the `brain-memory` skill being added by the AGENT-MEMORY section of this same release.** That skill owns the agent read/write protocol against migration `024_agent_attribution.sql` — how an agent records what it wrote and attributes it. None of my three skills mention attribution, agent authorship, or the memory protocol; `brain-capture` covers *human* quick capture into the `inbox` tag and explicitly routes writes-with-provenance elsewhere. The trigger-uniqueness test (§8) will mechanically catch it if `brain-memory` and `brain-capture` end up claiming the same phrase — whichever lands second must adjust. Recommended split if a conflict arises: `brain-memory` owns anything containing "remember"/"memory"/"what do you know about me"; `brain-capture` owns "capture"/"jot"/"note to self"/"inbox". I have already removed bare "remember this" from... — *correction:* `brain-capture`'s trigger list above **does** include "remember this". That is the highest-risk collision. **Resolution: drop "remember this" from `brain-capture` and cede it to `brain-memory`**, keeping "note to self", "jot this down", and "capture this".

10. **`SECURITY.md` links a GitHub advisory URL that 404s** because private vulnerability reporting is not enabled in repo settings. Handled the same way `.github/ISSUE_TEMPLATE/config.yml` handles the not-yet-enabled Discussions link: ship the file, but keep the URL commented with an inline instruction to enable the setting first. `tests/test_repo_hygiene_files.py` asserts the file *mentions* private reporting, not that a specific URL is live (a test cannot verify a remote setting).

11. **`pytest --collect-only` in a local sanity check reports `FAIL Required test coverage of 85% not reached. Total coverage: 24.30%`** — observed while writing this spec. Not a bug: `--cov-fail-under` from `addopts` applies to collection-only runs too, since no tests execute. Anyone debugging collection should pass `--no-cov`. Worth one sentence in `CONTRIBUTING.md` so it does not get mistaken for a broken gate.

12. **The `lint` job passes but `test` fails, or vice versa.** Two jobs mean two independent red/green signals and a partially-red badge. Accepted deliberately: the fast-fail value of a 40-second lint job outweighs the tidiness of one monolithic job, and both jobs live in one workflow so `ci.yml/badge.svg` reflects the union.

---

### 7. Security and safety

| Risk | Guard |
|---|---|
| CI destroys the production database | `ci.yml` pins `DATABASE_URL`/`TEST_DATABASE_URL` to port 5434 / `second_brain_test`; `tests/conftest.py:43-59` aborts the session if either resolves to a prod host/port/dbname; `down -v` targets only the named test volume (`docker-compose.age-test.yml:36,43`), never the `./data/postgres` bind-mount. |
| A workflow gains write access it does not need | `permissions: contents: read` declared at workflow level. No `secrets` referenced. No `pull_request_target`. |
| A fork PR exfiltrates secrets via a malicious workflow edit | `pull_request` (not `pull_request_target`) runs the fork's code with a read-only token and no secrets. Nothing in `ci.yml` echoes an environment variable. |
| A maintainer's personal email is committed as PII | `SECURITY.md` and `CODE_OF_CONDUCT.md` route through GitHub private reporting and the already-public `@mshtawythug` handle. **No email address is invented or committed.** (CLAUDE.md rule 15.) |
| A skill file leaks real personal data | All examples in the three new skills use the synthetic placeholders already allowlisted in `.pii-allowlist.txt` (`Jane Doe`, `Acme`, `Contoso`, `Northwind`) and the same synthetic corpus vocabulary the existing skills and `docs/cli-reference.md:429` use ("Project Phoenix", "platform migration"). `tests/test_skill_frontmatter.py` is **not** a PII scanner; the existing `scripts/hooks/pre-commit` gate is. |
| The pre-commit PII gate is not enforced in CI | **Deliberate, and it must stay that way.** The gate needs `.pii-denylist.local.txt`, which is gitignored by design (`.gitignore:66-68`) because it *contains* the real sensitive terms. Running it in CI would either require committing the denylist (catastrophic) or degrade to the allowlist+regex leg only. It also shells out to the `claude` CLI (`scripts/hooks/pre-commit:76`), unavailable on a runner. **Do not add a PII job to `ci.yml`;** instead `CONTRIBUTING.md` gains one line pointing at `scripts/hooks/README.md` for local installation. |
| Dependabot auto-merges a compromised dependency | No auto-merge is configured. Every Dependabot PR is a normal PR subject to `ci.yml` and (once branch protection is on) `CODEOWNERS` review. |
| A new skill instructs an agent to run a destructive command unsupervised | Each new skill ends with a `## Safety rules` H2 carrying explicit prohibitions: `brain capture review` is interactive and `--limit` picks by inbox order — never pipe blind confirmations (a real note was destroyed this way); `brain connect accept --write` mutates a vault file — confirm first; `brain audio` writes to the gitignored `audio/` dir — never commit its output. |
| `SECURITY.md` understates the real exposure of the optional wiki | The threat-model section states plainly that the Quartz/Caddy wiki has **no authentication** and that anyone who can reach its port reads the entire corpus, and tells users to bind it to loopback. |

---

### 8. Test plan

Three new test modules. All use `pathlib.Path`, full type hints, module docstrings, `pytest.mark.parametrize` over discovered files, and **no monkey-patching of production modules** — every one is a pure static read of tracked repository files, so no fixtures beyond `tmp_path` and no database at all.

#### `tests/test_ci_workflow.py` — the red-first proof

**This is the failing test that proves the gap.** Written and run **before** `ci.yml` exists; it fails with `FileNotFoundError` / an assertion naming the missing file, which is the literal evidence that the repository has no quality gate.

| Test | Asserts |
|---|---|
| `test_ci_workflow_file_exists` | `.github/workflows/ci.yml` exists. **RED before implementation** — this is the gap. |
| `test_ci_runs_ruff_mypy_and_pytest` | Flattening every `run:` string across all jobs contains `ruff check`, `mypy src/`, and a bare-ish `pytest` invocation. **RED before implementation.** |
| `test_ci_pytest_does_not_disable_coverage` | No `run:` in `ci.yml` passes `--no-cov` or `--cov-fail-under` — CI must inherit `pyproject.toml`'s floor, not override it. Guards against a future "just make it green" edit. |
| `test_ci_never_targets_the_prod_database` | Every `DATABASE_URL`/`TEST_DATABASE_URL` value in `ci.yml` uses port `5434` and a dbname ending `_test`; the strings `55432`, `:5433`, and `/second_brain"` never appear. |
| `test_ci_tears_down_with_if_always` | The compose-`down` step carries `if: always()`. |
| `test_ci_declares_least_privilege_permissions` | Top-level `permissions` == `{"contents": "read"}`. |
| `test_ci_concurrency_cancels_only_on_pull_requests` | `concurrency.cancel-in-progress` is the `github.event_name == 'pull_request'` expression, matching `eval.yml:44`. |
| `test_ci_triggers_match_the_house_pattern` | `on` has `pull_request`, `push.branches == ["main","master"]`, and `workflow_dispatch`. |
| `test_readme_ci_badge_points_at_ci_workflow` | The `[![CI](…)]` line in `README.md` references `workflows/ci.yml`, and the referenced workflow file exists on disk. **RED before implementation** (it currently references `eval.yml`). Also parameterized over *every* `actions/workflows/<f>/badge.svg` URL in the README so no badge can ever point at a non-existent workflow. |
| `test_default_suite_needs_no_ollama` | Regression guard for the "tests must skip, not fail" contract: every test module that imports `brain.chat`/`brain.embeddings` and is **not** marked `eval`/`benchmark` contains a `pytest.skip(` call. Fails if someone adds a hard Ollama dependency to the default suite. |

#### `tests/test_repo_hygiene_files.py`

| Test | Asserts |
|---|---|
| `test_security_md_exists` | `SECURITY.md` present. **RED before implementation.** |
| `test_security_md_has_required_sections` | Headings for supported versions, reporting, and expectations are present. |
| `test_security_md_uses_private_reporting_not_an_email` | The file mentions GitHub private vulnerability reporting **and** contains no `@`-bearing email address (regex `[\w.+-]+@[\w-]+\.[\w.]+`, excluding the `@mshtawythug` handle form). Enforces the no-PII rule mechanically. |
| `test_security_md_documents_the_local_first_model` | Mentions the local database, the unauthenticated wiki port, and the stdio MCP server. |
| `test_code_of_conduct_exists_and_is_covenant_21` | File present; contains "Contributor Covenant" and "2.1"; retains the CC BY attribution footer. **RED before implementation.** |
| `test_code_of_conduct_has_no_placeholder` | `[INSERT CONTACT METHOD]` does not appear. |
| `test_dependabot_config_covers_all_three_ecosystems` | Parsed YAML has `pip`, `github-actions`, and `docker` entries. **RED before implementation.** |
| `test_dependabot_docker_directory_points_at_a_real_dockerfile` | The `docker` entry's `directory` joined to the repo root contains a `Dockerfile`. Catches the easy mistake of pointing at the repo root. |
| `test_dependabot_sets_pr_limits` | Every entry has `open-pull-requests-limit`. |
| `test_codeowners_exists_and_covers_migrations_and_workflows` | File present; has a `*` default rule and explicit rules for `/src/brain/migrations/` and `/.github/workflows/`. **RED before implementation.** |
| `test_changelog_has_unreleased_section` | `## [Unreleased]` present above the newest released version heading. **RED before implementation.** |
| `test_changelog_link_definitions_resolve_to_real_tags` | Every `[x.y.z]: …/releases/tag/vX.Y.Z` link reference corresponds to a tag returned by `git tag --list`. **RED before implementation** — currently fails on the phantom `v0.1.0`. |
| `test_no_new_untracked_docs_paths` | Every `docs/*.md` file added by this release is reported tracked by `git ls-files` — catches a doc silently swallowed by `.gitignore`. |

#### `tests/test_skill_frontmatter.py`

Parameterized over every directory under `skills/` (reusing the enumeration helper from `tests/test_brain_skills_sync.py:26-27` — DRY), so the contract applies to all ten skills, existing and new.

| Test | Asserts |
|---|---|
| `test_every_skill_dir_has_a_skill_md` | Each `skills/<name>/` contains `SKILL.md`. |
| `test_frontmatter_parses_and_has_name_and_description` | Leading `---` block parses; `name` and `description` keys present. |
| `test_name_matches_directory` | `name:` equals the directory name — required for `bin/brain-skills-sync` to install it under the right name. |
| `test_description_declares_mandatory_triggers` | `description` contains `MANDATORY TRIGGERS:`. |
| `test_trigger_phrases_are_unique_across_skills` | No trigger phrase appears in two skills' `MANDATORY TRIGGERS` lists. **The routing-collision guard** (edge case 8/9). |
| `test_skill_body_has_an_h1` | Body starts with a single `#` heading. |
| `test_skill_length_is_within_house_range` | 100 ≤ lines ≤ 300 — the observed band is 140-241; the bounds catch a stub or an essay. |
| `test_new_skills_declare_safety_rules` | Parameterized over `brain-proactivity`, `brain-ask`, `brain-capture`: each has a `## Safety rules` (or `## Operational notes`) H2, matching `ingest-brain`/`brain-maintenance`. |
| `test_capture_skill_does_not_claim_ingest_triggers` | `brain-capture`'s triggers exclude "add this to my brain" and "remember this" (ceded to `ingest-brain` and `brain-memory`). |
| `test_skills_use_only_synthetic_names` | No skill body contains a token from a small list of real-looking-name heuristics beyond `.pii-allowlist.txt`'s synthetic set. Advisory belt-and-braces alongside the pre-commit gate. |

**Coverage impact:** all three modules are pure-logic tests over static files with no production-module imports beyond the shared enumeration helper, so they add test lines without adding uncovered `src/brain/` lines — coverage can only go up or stay flat.

**Manual verification the implementer must perform before enabling required status checks** (CLAUDE.md "Never declare done without running it"): (1) open a throwaway PR and confirm both jobs appear and pass; (2) confirm the run's skip count is non-zero and its coverage line reads ≥85%; (3) confirm the README badge renders green after the merge lands on `master`; (4) run `bin/brain-skills-sync` and then `bin/brain-skills-sync --check` and confirm the second exits 0 with "all 10 brain skills in sync"; (5) reproduce CI conditions locally per edge case 3 and confirm the coverage floor holds without Ollama.

---

### 9. Open questions — with the recommended answer

1. **One job or two in `ci.yml`?** → **Two** (`lint`, `test`). A 40-second lint job that fails fast is worth the extra check row; both share one workflow so one badge covers them.
2. **Should `ci.yml` also run the `e2e` / `phase3` markers?** → **No.** `e2e` needs a Node toolchain plus the live `~/brain-vault/.quartz/` workspace and is non-hermetic by construction (`pyproject.toml:136-142`); `phase3` needs optional migration 021 and is excluded precisely so v1/v2 databases stay green (`pyproject.toml:169-174`). Both would be flaky. Note the `e2e` marker comment claims "CI runs them in a dedicated workflow" — **no such workflow exists**; log that as a follow-up issue rather than silently absorbing it here.
3. **Should CI run a PII gate?** → **No.** The gate needs the gitignored `.pii-denylist.local.txt` and the `claude` CLI (`scripts/hooks/pre-commit:76`). It stays a local pre-commit hook; `CONTRIBUTING.md` gains a pointer to `scripts/hooks/README.md`.
4. **Should the pull-first optimization be back-ported to `eval.yml` and `benchmark.yml` in the same PR?** → **No.** One concern per PR. File a follow-up issue titled "flip eval/benchmark to pull-first AGE image" and reference `eval.yml:68`'s existing TODO.
5. **Should the eval badge stay in the README alongside the new CI badge?** → **No.** The README already carries six badges (`README.md:7-12`); a seventh for a harness that skips in CI is noise. Relabel, don't add.
6. **Matrix over Python 3.11 and 3.12?** (`pyproject.toml:25-26` claims both) → **Not now.** It doubles CI time on a ~12-minute suite for a single-maintainer project. Recommend a **weekly scheduled** 3.12 run as a separate follow-up so the claim stays honest without taxing every PR.
7. **Dependabot schedule — daily or weekly?** → **Weekly** (pip, actions) and **monthly** (docker). One maintainer; daily is abandonment-by-noise.
8. **`SECURITY.md` reporting channel — email or GitHub private reporting?** → **GitHub private reporting.** No maintainer email exists in the tracked tree to cite, inventing one is forbidden, and committing a personal address violates CLAUDE.md rule 15. If private reporting is not yet enabled in repo settings, ship with the link commented and the enable-instruction inline, exactly as `.github/ISSUE_TEMPLATE/config.yml` does for Discussions.
9. **Three new skills or one combined "brain-proactive" skill?** → **Three.** The existing seven are each scoped to one coherent workflow, and one nine-command skill would exceed the 140-241-line house band and blur trigger routing.
10. **Does `brain claude install-skill`'s packaged `SKILL.md` (63 lines) need updating for the new commands?** → **Out of scope for this section, but flag it.** That file is a condensed cheat-sheet for pipx users and already omits most of the command surface. Recommend a separate follow-up that regenerates it from `docs/cli-reference.md`, and note the real fix is to make `brain claude install-skill` install the full `skills/` set for pipx users too — a behavior change needing its own design.
11. **Should `bin/brain-skills-sync --check` run in CI?** → **Yes, in the `lint` job, as one extra step** using `--dest "$RUNNER_TEMP/skills"`. It is a two-second static consistency check with no side effects on the runner's home directory (`bin/brain-skills-sync:14` honors `--dest`, and `tests/test_brain_skills_sync.py:252` already proves it never touches the real `$HOME`). Cheap insurance that the shipped script still works.
12. **Fix the phantom `[0.1.0]` CHANGELOG link by creating a `v0.1.0` tag, or by dropping the link?** → **Drop the link definition.** Back-dating a tag onto a retroactively chosen commit is revisionist and would confuse the `release.yml` `v*` trigger semantics.
13. **Which release version does `## [Unreleased]` become?** → **`0.3.0`.** The sibling sections add two migrations (024, 025) and new user-facing behavior; that is a minor bump under SemVer, not a patch. Recommend the coordinator cuts `v0.3.0` once all sections land, then converts the `[Unreleased]` heading and adds the `[0.3.0]` link definition.
14. **Does this section need a migration?** → **No.** Explicitly: no schema, no columns, no SQL. `024_agent_attribution.sql` and `025_document_sensitivity.sql` belong to the AGENT-MEMORY and SAFETY sections and are not touched here.
15. **Should `.github/FUNDING.yml` be added too?** → **No.** It is absent, and adding a funding surface is a personal decision with no engineering justification. Out of scope.
