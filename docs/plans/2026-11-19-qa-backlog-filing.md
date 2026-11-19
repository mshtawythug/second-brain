# Plan: file the unfixed 0.4.0 QA items in the backlog (BL-59 to BL-98)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

- Base: `8e9478e1` (`second-brain/master`). Branch `docs/qa-backlog-0.4.0`.
- Docs only. No `src/` or `tests/` change, no database, no CHANGELOG entry. The hygiene tests do
  not require one for a docs change: `tests/test_repo_hygiene_files.py` checks the CHANGELOG's
  shape, not that a change adds to it.
- Target: `docs/plans/2026-10-23-backlog-reconciliation.md`, §2.3, which holds BL-1 to BL-58.
  New rows continue after BL-58 in the same three-column shape:
  `| BL-n | **Title.** body with file:line at a SHA | Source; severity |`.
- Sources: the 0.4.0 manual QA report, which is local only and is cited by finding id (`F31`,
  `N7`) and QA row (`B26`), never quoted; and the "Not in scope" sections of the seven fix-program
  plans. Every file:line was re-derived on `8e9478e1`, or on the named PR head for unmerged work.

## What is filed, and how it is grouped

One row per finding, except where the doc's own precedent (BL-48, BL-50, BL-52) groups small items
that share a module or a symptom class.

| BL | Item | Source |
|---|---|---|
| BL-59 | MCP `brain_ingest_stdin` never enriches; its `content_type` default differs from the CLI | F47, N12 |
| BL-60 | `brain eval --answer` ignores `-n`, `-c` and the ranking flags | F48 |
| BL-61 | A model timeout is reported as "Ollama is not available" | F42 |
| BL-62 | `brain setup` re-run fails preflight on the brain's own Postgres port | F21 |
| BL-63 | MCP `brain_search`: a bad stage knob gives an opaque tool error | F30 (explain half added to BL-57) |
| BL-64 | `brain gaps` counts filter-narrowed zero-hit searches | F31 |
| BL-65 | `brain tag --regenerate-file` refuses after it has committed the tag | F38 |
| BL-66 | `brain enrich --backfill` skips below-floor docs silently | F43 |
| BL-67 | `brain search --rewrite`: the Score column contradicts the row order | F44 |
| BL-68 | `brain review scan --json` prints nothing when there are no findings | F45 |
| BL-69 | GraphRAG retrieval flags silently ignored or accepted | F46, F33, F36 |
| BL-70 | Three pieces of help or status text that mislead | F5, F14, F40 |
| BL-71 | `brain mcp token --show` prints the token from a too-open file | F6 |
| BL-72 | `brain show` never shows sensitivity | F8 |
| BL-73 | The secret notice says "stored UNCHANGED" for a file then skipped | F9 |
| BL-74 | `brain ingest-gmail --since` is not validated | F11 |
| BL-75 | Exit codes and flag formats that disagree across `review`, `dedup`, `gaps` | F26, N7 (two bullets) |
| BL-76 | Vault notes render as "manual" but `--source manual` excludes them | F32 |
| BL-77 | `brain connect refresh` double-counts, and its floor is undocumented in practice | F35 |
| BL-78 | Default ingest costs chat calls; a concept pass stored no mentions | N1 |
| BL-79 | `ingest-apple-notes`: fixed `osascript` path; `--dry-run` reads Notes | N2 |
| BL-80 | Byte-identical files at two paths are two documents; CLAUDE.md says otherwise | N4 |
| BL-81 | `ingest-notion --sensitivity confidential` does not raise stored pages | N5 |
| BL-82 | The secret guard has no AWS secret access key pattern | N6 |
| BL-83 | Ingest fidelity: HTML entities, frontmatter tags, frontmatter-less notes, capture `.txt` | N7 (four bullets) |
| BL-84 | People graph: a receipts sender becomes a person; `metadata.participants` ignored | N7 (two bullets) |
| BL-85 | MCP `serverInfo.version` is empty; a rename to a duplicate title is not warned | N7 (two bullets) |
| BL-86 | `brain-rebuild`'s in-flight guard sees every `brain ingest` on the machine | N8 |
| BL-87 | Three by-design behaviours the user cannot see | N9, N10, N11 |
| BL-88 | `brain-install-launchd`: an unresolvable `~user` vault path is a traceback | shims plan |
| BL-89 | `brain setup`: a failed `ollama list` / `ollama pull` is a traceback | cli-boundaries plan |
| BL-90 | Capture import: take the title from a leading heading (F13 option b) | cli-boundaries plan |
| BL-91 | `EmbedError` still escapes eight ingest loops | PR #137 plan |
| BL-92 | Stdin metadata: `date` inside `--metadata` and MCP `date` are warn-only; MCP `metadata` unchecked | PR #137 plan |
| BL-93 | Two DSN describers and two refused-connect lines | PR #137 plan |
| BL-94 | Restore residue, round 2: staging collision, `--json` errors, loopback aliases | PR #139 plan |
| BL-95 | `brain demo status --port N` opens the demo DB unguarded | PR #139 plan |
| BL-96 | The package-relative `.env` stays in the read chain on an installed `brain` | PR #140 plan |
| BL-97 | An opt-in relocation of the stray nested mirror directories F15 left | vault-mirror-path plan |
| BL-98 | The wheel omits `aliases/default.yml.example`, which a CLI message points at | team-lead |

**Folded into existing rows** (an "Added 2026-11-19" sentence, the precedent BL-25 and BL-43 set):

- F39 (C31): BL-27 is the same defect. Add the QA sighting and the call sites re-derived at `8e9478e1`.
- F30, the `brain explain` half: BL-57 already covers it. Add the QA sighting.
- N7, "MCP `brain_search source=bogus` returns empty": the same gap as BL-17 (`since_days`). Add it there.

## Deliberately not filed

- **Harness-only notes** (H1 to H10) and **L1, I1, E1**: test-harness issues, a status-report
  correction, a cleanup of QA databases, and documented secret-guard behaviour. No product defect.
- **N3, matrix drift**: each bullet is a QA-matrix expectation (`watch list`, `tags list` have no
  `--limit`; the brief is a page, not a row; no ingest-time auto-tags; capture does not enrich
  inline; synthesis is themes-only). No doc or help text claims otherwise. The synthesis bullet is
  covered by BL-69(a).
- **Uninstall plan, "Not in scope"**: `DEFAULT_VAULT_PATH` bound at import is a design note
  (`resolve_install_vault` reads it at call time), and the memory-file update is coordinator work.
- **F1–F4, F7, F10, F12, F13, F15–F20, F22–F25, F27–F29, F34, F37, F41**: fixed in this program.

## Corrections found while re-deriving

- The shims plan says a `ConfigError` from `resolve_install_vault` escapes `install_main`. On
  `8e9478e1` that function raises no `ConfigError`. The real escape is `RuntimeError` from
  `Path.expanduser()` on a `~nosuchuser/...` value. BL-88 is filed as found.
- N7 "capture import ignores `.txt`": QA row A19's own dry run listed a `.txt` share. BL-83(d)
  is filed as "re-verify first".
- F45 asks for a JSON array, but `review scan --json` is documented as newline-delimited JSON, and an
  empty stream is valid NDJSON. BL-68 proposes a stderr count, not an array.

## Verification

Docs only, so no new tests. Run `tests/test_repo_hygiene_files.py` (the `docs/*.md` trackability
check and the bidi scan) under `.claude/full-suite.lock`, with the `df` check first. Then scan both
changed files for raw bidi characters, and for any absolute home path or real name.
