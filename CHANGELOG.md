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
  `pg16-v1.5.0-rc0-pgv0.8.2`). Its version string is pinned to the AGE +
  pgvector versions baked into the Dockerfile, not to the CLI version, and it is
  published by `.github/workflows/publish-age-image.yml`. Bumping the CLI does
  not rebuild the image; rebuilding the image does not bump the CLI.

## [0.2.1] - 2026-07-20

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

## [0.1.0]

Initial public release.

### Added

- Hybrid search over Postgres + pgvector (Reciprocal Rank Fusion of full-text
  rank and vector cosine similarity, with recency weighting and metadata
  filters).
- GraphRAG entity-graph layer (people, orgs, and concepts in Apache AGE) for
  themes, patterns, and connections across interactions.
- Obsidian-style vault of wiki-linked notes, publishable as a browsable Quartz
  wiki.
- `brain-mcp` MCP server for querying the brain from any MCP-compatible client.

[0.2.1]: https://github.com/mshtawythug/second-brain/releases/tag/v0.2.1
[0.2.0]: https://github.com/mshtawythug/second-brain/releases/tag/v0.2.0
[0.1.0]: https://github.com/mshtawythug/second-brain/releases/tag/v0.1.0
