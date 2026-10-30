# Documentation

Reference docs for [Second Brain](../README.md). The [top-level README](../README.md) is the quick-start-first overview; the pages below are the deep reference the README links out to.

| Doc | Covers |
|---|---|
| [cli-reference.md](cli-reference.md) | First-run `setup` and the offline `demo`; advanced flags and diagnostics for the core commands (ingest options, `explain`, `edit`, `mark-draft`/`mark-published`, `rm`), plus the full command surface beyond the README's core five: Gmail ingest, status/health, `capture` / `enrich` / `rate` / `people`, tacit-knowledge `elicit`, and the eight proactivity/synthesis commands (`resurface` / `brief` / `review` / `timeline` / `connect` / `ask` / `audio` / `gaps`). |
| [graphrag.md](graphrag.md) | Entity-graph retrieval (Apache AGE): how it works, enabling it, the five query modes, and upgrading an existing brain to the AGE image. |
| [vault.md](vault.md) | The two-tier vault model (ingested + authored notes), authoring/link-graph/maintenance commands, the `bin/` scripts that keep it in sync, and what an upgrade from the retired Quartz wiki leaves behind (`brain ui` is the reading surface). |
| [configuration.md](configuration.md) | Tech stack, installing from source, running `brain` from any directory, feature tuning knobs, Claude Desktop + Claude Code integrations, choosing/switching embedder backends, data-hygiene backfills, and uninstall. |
| [agent-skills.md](agent-skills.md) | The eight Claude Code skills bundled in `skills/`: which one covers which commands, installing them with `bin/brain-skills-sync`, how that differs from `brain claude install-skill`, and the contract for adding one. |
| [guides/brain-ui-on-your-phone.md](guides/brain-ui-on-your-phone.md) | Reading from a phone or tablet: `brain ui --lan` or `--host <LAN address> --token`, how the token travels, and the security caveats. |
| [guides/local-reranker.md](guides/local-reranker.md) | The local cross-encoder reranker: installing `llama-server` and the `bge-reranker-v2-m3` GGUF (tested version, size, measured memory), the opt-in `com.brain.reranker` launchd job, and `brain doctor`'s `reranker` line. |
| [guides/apple-notes.md](guides/apple-notes.md) | Ingesting named Apple Notes folders on macOS (`brain ingest-apple-notes`): the Automation permission prompt, what is stored and skipped, and how to revoke access. |
| [guides/claude-desktop-setup.md](guides/claude-desktop-setup.md) | Step-by-step walkthrough for wiring `brain-mcp` into Claude Desktop (symlink, boot smoke-test, config, troubleshooting). |

## Project docs

- [../CONTRIBUTING.md](../CONTRIBUTING.md) — development setup, codebase layout, running the tests, and contribution workflow.
- [../CHANGELOG.md](../CHANGELOG.md) — release history.
- [../LICENSE](../LICENSE) — MIT.
