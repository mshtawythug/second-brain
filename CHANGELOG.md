# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Two version axes

This repository ships two independently versioned artifacts, each with its own
git tag scheme:

- **`v*` tags** — the **brain CLI** (the Python package `secondbrain-py`). These
  entries below track that axis. A `v*` tag cuts a GitHub Release and publishes
  the sdist+wheel to PyPI (`.github/workflows/release.yml`).
- **`age-image-*` tags** — the prebuilt **Apache AGE Postgres image**
  (`ghcr.io/mshtawythug/second-brain-age`, tag
  `pg16-v1.5.0-rc0-pgv0.8.6`). Its version string is pinned to the AGE +
  pgvector versions baked into the Dockerfile, not to the CLI version, and it is
  published by `.github/workflows/publish-age-image.yml`. Bumping the CLI does
  not rebuild the image; rebuilding the image does not bump the CLI.

## [Unreleased]

## [0.3.0] - 2026-08-07

### Fixed — configuration resolution and the background daemons

- **`brain` now works from any directory.** Configuration is resolved through a
  dotenv chain whose last link is `$BRAIN_HOME/.env`, but nothing ever created
  that file, so a `brain` invoked outside the source checkout found no
  configuration at all. `brain setup` and `brain init` now provision it: a real
  file on a fresh install, or — in a development checkout — a symlink to the
  repository's `.env`, so credentials are not duplicated onto disk twice and
  cannot drift. Both paths are idempotent and never clobber an existing file.
- **The error message named the wrong cause.** A missing configuration file
  reported `DATABASE_URL is not set (see .env.example)`, which sends people to
  debug a database that is working fine. It now prints every path that was
  searched with its resolution state, distinguishes "no configuration found"
  from "configuration loaded but this key is absent", and reports a dangling
  symlink as such rather than as a missing file.
- **The launchd daemons failed silently for twelve days.** launchd starts agents
  with a minimal environment and no useful working directory, so all three hit
  the same dead configuration chain. The generated property lists now export
  `BRAIN_HOME` and set an explicit `WorkingDirectory`, and the daemon entry
  points resolve configuration explicitly rather than inheriting it. The
  interpreter the plists pin is now overridable via `BRAIN_PY`, matching the
  contract `brain.bin._launcher` already documented.
- **The daemons opt out of the working-directory `.env` walk-up**
  (`BRAIN_IGNORE_CWD_DOTENV`). That leg climbs to the filesystem root, so a
  stray `.env` in any ancestor directory could silently repoint a daemon at a
  different database. The walk-up remains the default for interactive use.
- **A failed configuration load no longer publishes a stale wiki.** The build
  caught the failure, logged a warning, and carried on — republishing from the
  vault mirror with no database refresh, so the site looked healthy while
  serving twelve-day-old content. It now aborts before the blue/green swap, so
  the last good build stays live, and exits with a distinct status (`3`) meaning
  "misconfigured, a human must act" as opposed to `1`, "the build broke, a retry
  may help". The watcher logs such faults once rather than per file event.
- **Log files are now size-capped.** A crash-looping watcher produced a 496 MB
  error log with no rotation of any kind; on a smaller disk that fills the
  volume. Rotation covers the standard-error path that launchd writes to
  directly, not only Python's logging handlers.

### Fixed — `brain doctor` can now see the failure it missed

- Three new checks, all of which stayed silent through the outage above: whether
  `$BRAIN_HOME/.env` resolves *and* loads (distinguishing missing, unreadable,
  dangling-symlink and present-but-incomplete); whether each installed LaunchAgent
  is loaded and last exited zero; and whether the newest *completed* wiki build is
  recent. A signal-killed daemon that also left the wiki stale is reported as a
  failure rather than a routine restart, and these conditions move the exit code
  instead of printing a red line and exiting `0`. A fourth check warns when the
  daemons and the CLI import different installations of the package.
- **`chunks stats` reported a row count three orders of magnitude wrong.** It
  read `pg_stat_user_tables`, whose counters PostgreSQL discards on crash
  recovery, and so announced "never analyzed" for a table whose planner
  statistics were present and current. It now reads the crash-durable catalogs.
- **The vault-drift remedy could destroy data.** A mirror file whose front-matter
  id did not match any document was classed as an orphan without checking whether
  the *path* belonged to a live row — so `brain vault prune-orphans --apply`
  would have deleted the only mirror of a real document. Clobbered mirrors are now
  a distinct category with `brain vault export --force` as the remedy.

### Fixed — safety of the tooling itself

- **The secret-scanning pre-commit hook could be walked past.** The false-positive
  allowlist contained the standard PEM header line that opens every RSA private
  key. Because the hook extracts matches rather than merely detecting them, a
  genuine key reduced to exactly that header, which was then subtracted as an
  allowlisted false positive — so a real private key would have committed
  silently. `EC` and `OPENSSH` headers were unaffected, since only the RSA form
  was listed. (This changelog entry deliberately does not reproduce the header:
  the repaired gate correctly rejects it, and the first draft of this very entry
  was blocked for containing it.)
- **Four privacy tests never ran.** Their guard compared a Typer `CommandInfo.name`
  that is `None` for commands registered without an explicit name, so the
  predicate was permanently false — including the only executed check that
  `brain usage --json` withholds raw search-query strings. The contract itself
  held; its verification was absent.
- Schema migrations now serialise on an advisory lock taken inside
  `run_migrations`, so concurrent runners — `brain init`, `brain demo`, a restore,
  or a test session — can no longer interleave a read-then-apply sequence against
  the same database.
- The two competing Quartz build timeouts (300 s from one entry point, 600 s from
  another) are unified into one overridable constant.

### Added

- **Continuous-integration quality gate** (`.github/workflows/ci.yml`) — every
  pull request and every push to `main`/`master` now runs `ruff check`,
  `mypy src/`, and the full `pytest` suite (with its 85% coverage floor) against
  the pinned Apache AGE Postgres test instance. Two jobs: a Docker-free
  lint/types job that fails fast, and the test job. The README's **CI** badge now
  reports this gate; previously it reported the marker-gated eval harness, which
  runs neither the linter, the type checker, nor the coverage floor.
- `SECURITY.md` — supported versions, private vulnerability reporting through
  GitHub's Security tab (no email address published), response expectations, and
  the security model of a local-first tool: the loopback-bound local database,
  the **unauthenticated** optional wiki port, the stdio MCP server, and what does
  and does not leave the machine per embedder backend.
- `.github/dependabot.yml` — weekly grouped updates for the Python dependencies
  in `pyproject.toml` and for GitHub Actions, monthly for the Apache AGE base
  image. Nothing auto-merges; every Dependabot PR runs the new CI gate.
- Daily repository-traffic snapshot workflow and its committed history
  (`.github/workflows/traffic-stats.yml`), plus live PyPI version, PyPI download,
  and GitHub star badges in the README.

### Changed

- **`mcp` now supports both major versions: `mcp>=1.2,<3.0`** (was `>=1.0,<2.0`).
  mcp 2.0 renamed the two names this project imports, so a compatibility layer
  resolves them at import time; 1.2.0, 1.29.0 and 2.0.0 are all exercised. Note
  the floor moved up: `mcp.server.fastmcp` did not exist before 1.2.0, so the old
  `>=1.0` claimed support for versions where `brain-mcp` could not import.
- The bundled Apache AGE image now builds on `pgvector/pgvector:0.8.6-pg16`.
  Existing local deployments keep running their current image until you rebuild
  deliberately — see `docs/graphrag.md`.

- **The `typer` dependency is now bounded on both sides: `typer>=0.16,<0.28`**
  (previously unbounded above). This is a user-visible install constraint: in a
  shared environment, an upper bound can conflict with another package that
  requires a newer Typer, and `pip install --upgrade typer` past the cap will no
  longer resolve alongside this release. The bound exists because Typer renders
  the entire CLI surface — Typer 0.26 vendored Click wholesale, which changed
  the exit code and traceback behaviour of a hand-raised usage error, and Typer
  below 0.16 pairs with a current Click in a combination that fails the CLI
  test suite. Both ends are exercised in CI; the cap is a requirement that the
  next minor be tested before users get it, not a claim that it is broken.
  Users who need a Typer outside this range should stay on 0.2.1 until the cap
  moves.

### Fixed

- `brain vault render` now invokes `npx --no -- quartz build`. Without `--no`,
  a workspace where npm cannot resolve `quartz` locally made npx fall back to
  installing a package of that name from the registry and building the vault
  with it — and such a workspace still passed the setup check, so the
  substitution of an unpinned package for the overlaid local Quartz was
  silent. It now exits non-zero instead. The `--` separator keeps npx from
  swallowing Quartz's own flags.
- `brain vault render` no longer derives its output directory from the current
  working directory. The default was `./dist`, so running the command from an
  unrelated project wrote a complete Quartz site into that project's source tree
  (and a scheduled run, which has no meaningful cwd, would do it every time).
  The default is now `<quartz-dir>/dist` — i.e. `<vault>/.quartz/dist` — derived
  from the configured vault, so the site lands in the same place from any
  directory. An explicit `--to` still overrides it and still keeps ordinary
  cwd-relative semantics. Because the Quartz build deletes its output directory
  before emitting, render now also refuses an output path that contains the
  vault, the Quartz workspace, or the current directory; in particular `--to .`
  (previously accepted, and equivalent to wiping the working directory) is
  rejected.
- The repository-traffic snapshot is now genuinely best-effort: it rebases before
  pushing and picks the latest entry by date, so a transient API failure or a
  concurrent push no longer fails the workflow.

## [0.2.1] - 2026-07-20

### Fixed

- **`brain init` on a `pip`/`pipx` install** — the SQL migrations were not
  packaged into the wheel, so a non-editable install had no migrations to apply
  and `brain init` could not build the schema. The migrations now live inside the
  `brain` package (`src/brain/migrations/`) and ship with the wheel. This is the
  fix that makes a clean `pipx install secondbrain-py` usable at all.
- `brain init` now fails loudly when no migrations are packaged, instead of
  silently reporting success against an empty schema.

### Changed

- **Distribution renamed to `secondbrain-py`** (was `second-brain`). PyPI's
  anti-typosquat rule blocks `second-brain` — its separator-collapsed form
  collides with the unrelated existing `secondbrain` project — so the published
  package name is now `secondbrain-py`. The import package stays `brain` and the
  human CLI stays `brain`; only the distribution name and the uvx convenience
  alias changed.
- The uvx alias is now `uvx secondbrain-py` (was `uvx second-brain`), matching the
  renamed distribution and the MCP-registry `server.json` package identifier.
- PyPI Trusted Publishing is now enabled in `release.yml`, so `v*` tags publish
  the sdist+wheel to PyPI (`pipx install secondbrain-py`). A `workflow_dispatch`
  trigger allows re-running a publish manually.

No functional changes to the CLI, search, ingest, or graph behavior.

## [0.2.0] - 2026-07-19

### Added

- `brain demo` taste test — a zero-config, throwaway sample corpus so a new user
  can try hybrid search in seconds before wiring up their own data.
- `BRAIN_EMBEDDER=none` FTS-only mode — run without any embedding backend
  (no Ollama / no API key), full-text search only, for the lightest install.
- `brain setup --profile minimal|standard|full` — tiered setup that provisions
  only what each profile needs.
- Prebuilt multi-arch (linux/amd64 + linux/arm64) Apache AGE Postgres image on
  GHCR, so fresh installs pull instead of compiling AGE from C source (slow and
  fragile on Apple Silicon). The compose files keep a local `build:` fallback.
- README restructure and docs split for a clearer first-run path.

### Fixed

- Install one-liner: broken placeholder/tag reference (the advertised
  `v0.2.0` install path now resolves).
- launchd default-install behavior.
- Caddy preflight abort.

## 0.1.0

Initial public release. Unbracketed on purpose: no `v0.1.0` tag was ever cut, so
there is no release page to link to, and a bracketed heading with no definition
renders as the literal text `[0.1.0]`.

### Added

- Hybrid search over Postgres + pgvector (Reciprocal Rank Fusion of full-text
  rank and vector cosine similarity, with recency weighting and metadata
  filters).
- GraphRAG entity-graph layer (people, orgs, and concepts in Apache AGE) for
  themes, patterns, and connections across interactions.
- Obsidian-style vault of wiki-linked notes, publishable as a browsable Quartz
  wiki.
- `brain-mcp` MCP server for querying the brain from any MCP-compatible client.

[Unreleased]: https://github.com/mshtawythug/second-brain/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/mshtawythug/second-brain/releases/tag/v0.3.0
[0.2.1]: https://github.com/mshtawythug/second-brain/releases/tag/v0.2.1
[0.2.0]: https://github.com/mshtawythug/second-brain/releases/tag/v0.2.0
