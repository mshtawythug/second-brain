# Agent Memory, Safety, Search Transparency & Local UI — Design Spec

**Date:** 2026-07-25
**Status:** Approved for implementation
**Sources:** competitive analysis of [`itsmeduncan/commonplace`](https://github.com/itsmeduncan/commonplace)
(self-hosted privacy-tiered agent memory) and a reference local notes UI ("Quicky Brain"),
evaluated against an 8-area evidence survey of this repository at commit `1cfd23d2`.

---

## 1. Release theme

**Close the write loop, make recall measurable, make the data safe, and give the brain a face.**

The survey found second-brain is, today:

- **Read-heavy from agents.** 37 MCP tools and 7 Claude Code skills exist. `grep -rn "brain capture" skills/`
  returns zero matches — nothing teaches an agent to *write*. The write step is the one agents skip,
  and nothing forces it.
- **Unmeasured.** `interactions` (migration 010) and `search_queries` (migration 019) are populated on
  every read, rating, and search — and consumed only by gap-mining. There is no answer to "am I actually
  using this?" and no latency or match-count feedback anywhere in the search path.
- **Unprotected.** There is no backup command (the repo's own `backups/` directory holds five
  hand-made `pg_dump` files under three different naming conventions — the need is proven by the
  workaround). Ingest performs zero content inspection, so a pasted credential is chunked, indexed,
  mirrored to the vault as plaintext Markdown, and published to the wiki. `draft` is a publish
  quarantine, not a confidentiality control.
- **Headless.** The only browser surface is the static Quartz wiki. Nothing can be created, edited,
  retagged, moved, or deleted from a browser.

## 2. Accepted features

| # | Feature | Effort | Migration |
|---|---------|--------|-----------|
| F1 | Session-end memory write-back — Claude Code Stop hook + `brain-memory` skill + `brain capture --json` | M | — |
| F2 | Token-budgeted recall — shared budgeter, `brain recall`, `--max-tokens`, MCP `brain_recall` | M | — |
| F3 | `brain backup` / `brain restore` | M | — |
| F4 | Ingest-time secret guard | S | — |
| F5 | Search transparency — latency, lexical match total, `--facets` | M | 024 |
| F6 | Per-document sensitivity tier + hosted-egress gate | M | 026 |
| F7 | `brain stats` — usage over time | S | — |
| F8 | `brain note move` + MCP CRUD parity | M | — |
| F9 | `documents.updated_at` + updated-range filters | M | 025 |
| F10 | Agent attribution on writes (`agent_id`) | M | 027 |
| F11 | Agent-facing skill coverage for the seven un-skilled command groups | S | — |
| F12 | The CI quality gate the PR template already promises + `SECURITY.md` + dependabot + CHANGELOG | S | — |
| F14 | `brain ui` — local single-user web app | L | — |

**Deferred from this release:** *F13 — saved searches.* Accepted in principle (it was already on the
roadmap as wave Q3-B) but cut for scope: it is the only accepted feature with no detailed design
section, its value is medium, and this release already spans twelve features plus the largest single
item (F14). It depends on F5's extracted WHERE-builder helper, which this release delivers, so the
follow-up is cheap.

### Migration allocation (central — no section may deviate)

Head at time of writing is `023_search_queries_fts_count.sql`.

| Number | File | Owner |
|--------|------|-------|
| 024 | `024_search_queries_duration.sql` | F5 |
| 025 | `025_documents_updated_at.sql` | F9 |
| 026 | `026_document_sensitivity.sql` | F6 |
| 027 | `027_agent_attribution.sql` | F10 |

> **This table overrides any migration number appearing inside a per-feature section.** The section
> authors worked in parallel and were each given a provisional number; where a section says `024` for
> agent attribution or `025` for sensitivity, the numbers above win.

### Detailed design sections

| File | Covers |
|------|--------|
| `2026-07-25-sections/F1-session-end-capture-hook.md` | F1 |
| `2026-07-25-sections/F2-F7-F10-token-budgeted-recall-agent-attribution-usage-analytics.md` | F2, F7, F10 |
| `2026-07-25-sections/F3-backup-restore.md` | F3 |
| `2026-07-25-sections/F4-F6-ingest-secret-guard.md` | F4, F6 |
| `2026-07-25-sections/F5-F8-search-transparency.md` | F5, F8 |
| `2026-07-25-sections/F11-F12-ci-gate-repository-hygiene-documentation.md` | F11, F12 |
| `2026-07-25-brain-ui-design.md` | F14 |
| `2026-07-25-sections/R1-DEFERRED-mcp-http-transport.md` | R1 — evaluated in full, then deferred |

F9 (`documents.updated_at` + updated-range filters) has no standalone section; its design is specified
inline in the implementation plan, since it is a single additive column plus two filter predicates that
mirror the existing `--after`/`--before` pair exactly.

Every migration is **additive**, and every one carries a default that preserves current behavior, so
applying them is a no-op for existing rows. No shipped migration is edited.

## 3. Rejected proposals (and why)

These were evaluated and deliberately **not** built. Recording them here so they are not re-proposed.

### Architecture mismatch

- **R1 — HTTP / streamable-HTTP MCP transport with bearer-token auth.** The installed SDK supports it
  and the transport flip is nearly one line, but the value proposition ("laptops on a private network
  reach the memory remotely") is a multi-device team need, not a single-user local one. It converts a
  stdio subprocess with *no network surface at all* into an authenticated network service; the auth,
  scope, and Host-header-hardening model is the expensive part, and the reference project had to patch
  its dependency to make it work. **F14 covers the real need** — reaching the brain from a browser — over
  loopback only. Revisit only with a concrete multi-device requirement.
- **R2 — `group_id` / project namespacing on `documents`.** A multi-tenant construct for a single-user
  corpus. `tags`, `documents.kind`, and the vault folder tree already partition; the graph layer already
  carries `tenant_id`.
- **R3 — `workspace` and `category` as first-class columns** (the reference UI's three-dropdown filter
  bar). Reserved tag namespaces get ~90% of the behavior for zero migration, and F5's `--facets` supplies
  the counts that make dropdowns useful. F14 therefore maps its three filters onto axes that already
  exist — source kind, tag, and content type — rather than inventing a parallel taxonomy.

### Cost far exceeds single-user value

- **R4 — User-configurable custom entity ontology** (Decision, Deliverable, Risk…). The 5-type set is
  hardcoded in six independent places including two DB `CHECK` constraints, and relaxing it forces an
  `EXTRACTOR_VERSION` bump and full re-extraction of ~1200 documents through a local LLM — hours of wall
  time — to gain node types one user would hand-curate anyway. The alias-merge YAML already covers the
  real entity-quality pain.
- **R5 — Typed fields on entities** and **R6 — typed semantic relationship edges** (`works_on`, `owns`).
  Same re-extraction cost. `rel_type` is the literal `'co_occurs'` at every one of its five occurrences;
  typed edges would additionally require reworking edge weighting so they are not scored as
  co-occurrence lift.
- **R7 — Fact-level contradiction store with `superseded_by` edges.** Entity-level contradiction
  adjudication and doc-vs-doc staleness already ship. The cheap increment worth doing instead —
  `brain review snooze` / `brain review resolve`, since `queries.py` already reads the `snoozed` status
  but no command ever sets it — is folded into F11.

### Actively harmful here

- **R8 — Episode / raw-content compaction** (drop raw text, keep derived facts). The reference project
  does this safely because its facts live on edges independent of the raw episode. Here, `chunks.tsv` is
  `GENERATED ALWAYS` from `chunks.content`: nulling raw text would destroy lexical search — one of the two
  retrieval legs — and permanently foreclose re-embedding, which is the documented procedure for switching
  embedder backends. Storage is not a constraint at this corpus size.

### Already exists

Spaced repetition (`brain resurface`), search-failure mining (`brain gaps`), contradiction and staleness
scans (`brain review scan`), thumbs feedback (`brain rate`), curated entity alias/merge YAML, tag
normalization at every write boundary, the advisory-locked rebuild orchestrator, post-restore planner-stat
repair (`brain analyze`), the retrieval eval harness with a `--fail-below` CI gate, document-date
`--after`/`--before` filters, and the `draft` publish quarantine. A command palette already ships (bound to
Cmd/Ctrl-P rather than the reference UI's Cmd+K — F14 uses Cmd+K in its own surface).

## 4. Global constraints

Every section below inherits these. They are not restated per feature.

- **PII:** all fixtures, examples, and commit messages use synthetic values only — invented names,
  `*.example.com` addresses, non-resolving credentials of the `AKIAIOSFODNN7EXAMPLE` form.
- **Production safety:** never target the production database (port 55432, database `second_brain`) or
  the `./data/postgres` bind mount destructively. Tests run against the test instance on port 5434.
- **Quality gates:** `ruff check` zero warnings, `mypy src/` zero errors, full `pytest` green, coverage
  at or above 85% overall. Red-first tests for every feature and every bug fix.
- **Style:** type hints on every signature, module docstring on every file, `pathlib.Path`, f-strings,
  parameterized SQL only, no bare `except:`, exceptions inheriting `BrainError`, files under 800 lines.
- **No monkey-patching production modules in tests.** Inject dependencies instead.

## 5. Empirically verified constraints discovered during design

These were established by running commands against the live system, not inferred. They are binding.

1. **The host `pg_dump` cannot dump the server.** The production Postgres is 16.14 (in Docker); the
   host's Homebrew `pg_dump` is 14.23. Verified failure:
   `pg_dump: error: server version: 16.14 …; pg_dump version: 14.23 … aborting because of server version mismatch`.
   **F3 must therefore run `pg_dump` inside the container by default**, and may use a host binary only
   after an explicit major-version check.
2. **`starlette` 1.3.1 and `uvicorn` 0.49.0 are already installed** as transitive dependencies of
   `mcp>=1.0`. **F14 therefore adds no new runtime dependency**, which removes the main objection to a
   local web surface. `fastapi` and `jinja2` are *not* installed and must not be added.
3. **The repository ships no JavaScript build chain** outside `node_modules`. F14's front end is
   therefore hand-written static assets packaged into the wheel — no Vite, no bundler, no CDN, and it
   must work fully offline.
4. **A real search takes ~6 seconds** on the live corpus (`brain search "hybrid search ranking" --limit 3`),
   dominated by the Ollama embedding call — and the user is currently shown nothing about that. This is
   what makes F5's phase-split latency genuinely useful rather than decorative.

---

*Detailed per-feature design sections follow in section 6 onward, appended as each is authored. The
implementation plan lives at `docs/plans/2026-07-25-agent-memory-safety-ui.md`.*
