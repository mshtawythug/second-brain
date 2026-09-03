# GraphRAG Skill + MCP Routing — Design (2026-05-25)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this spec task-by-task.

> **PII note (CLAUDE.md rule 15):** All example names/queries below are **synthetic**
> (Jane Doe, Project Atlas, Acme). The live graph holds real org/person names — never
> paste those into committed skills, docs, tests, or commit messages.

> **Related:** `docs/specs/2026-04-27-mcp-server-design.md` (original MCP server
> architecture). This spec is a distinct concern: skill discoverability + routing +
> capability completeness, not the server's transport/architecture.

## Problem

GraphRAG is built, enabled (`BRAIN_GRAPH_ENABLED=true`), and queryable from any cwd
(verified: `brain graphrag stats` works from `/tmp`; `config.py` loads `.env` from a
fixed module-relative path, not the cwd). Yet an LLM in an arbitrary conversation does
**not** reach for it. The capability + the `brain-graph` skill content are good; the
failure is **discoverability, routing, and staleness**.

## Root cause — five gaps

1. **`brain-graph` skill is repo-only** — NOT in `~/.claude/skills/`. Of the brain-skill
   family, only `consult-brain` is globally installed, so in real conversations Claude
   cannot see `brain-graph` to invoke it. (Dominant gap.)
2. **`consult-brain` (the one global entry skill) has zero graph references** — never
   hands graph-shaped questions off to `brain-graph`.
3. **Global `~/.claude/CLAUDE.md` "Second Brain" section is silent on GraphRAG** — the
   always-loaded top-level routing instruction only knows `brain search` / `brain show`.
4. **Skill + MCP are behind the code:** the skill lacks `brain graphrag entities` and
   `stats` (added this session) and still says "experimental / default-off"; MCP lacks
   `refresh` / `communities refresh` siblings and has terse, when-to-use-silent
   descriptions.
5. **Global skills are copies, not symlinks → drift** (the global `consult-brain` is a
   stale copy). Whatever we install rots without a sync mechanism.

## Decisions (locked with the user)

- **Scope: systemic** — fix the whole brain-skill global-install/sync story + routing,
  not just GraphRAG.
- **Sync mechanism: copy + re-run** (real files, predictable) with a `--check` drift
  guard. (Symlink and plugin approaches rejected.)
- **MCP is first-class** — the user explicitly wants the MCP tools *and* the skills to
  reflect ALL new GraphRAG capabilities, with full CLI↔MCP parity.
- **Out of scope:** plugin packaging.

## Design

### A. Skill sync — `bin/brain-skills-sync` (copy-based)

Bash script alongside `bin/brain-*`. Copies each repo brain skill
(`skills/{brain-graph,brain-authoring,brain-maintenance,brain-todo,consult-brain,ingest-brain}`)
→ `~/.claude/skills/<name>/`. Requirements:
- Idempotent; prints `installed / updated / unchanged` per skill.
- Touches **only** the brain-family skills enumerated from `skills/` — never the ~240
  other global skills (e.g. the open-design collection).
- `--check` (alias `--dry-run`): diff repo vs installed per skill, report drift, exit
  non-zero if any stale; copy nothing. This is the antidote to copy-rot.
- Clear overwrite semantics (replace the destination skill dir wholesale so deletions
  in the repo skill propagate).
- One-line `--help`.

### B. Capability completeness

**B1 — `brain-graph` SKILL.md:**
- Add `brain graphrag entities` (list/filter entities by `--type`) and `brain graphrag
  stats` (graph overview) to the CLI command table + the MCP mirror list, each with a
  one-line "when to use" (e.g. "what orgs/people/topics are in my brain", "how big is
  the graph", "list all projects").
- Verify all five modes (`auto/local/themes/global/fuse`) + flags are current.
- **De-experimentalize:** GraphRAG is enabled + built + queryable from any cwd; lead
  with that. Keep the prereq/`brain doctor` guidance only as an error-time fallback.

**B2 — MCP descriptions (`src/brain/mcp_server.py`):**
- Rewrite all `brain_graphrag_*` tool docstrings so each **signals when to use it** —
  the graph-vs-search cue, the five `search` modes, `entity` (one neighbourhood) vs
  `entities` (enumerate), `stats` (overview). Goal: an LLM reading the MCP tool list
  alone routes correctly. Mirror the skill's decision table in tool-description prose.
- Keep the existing JSON wire shapes unchanged (no behavior change — descriptions only,
  except B3's new tools).

**B3 — MCP parity (new tools):**
- Add `brain_graphrag_refresh` and `brain_graphrag_communities_refresh` mirroring the
  CLI (admin ops, like the existing `build` / `communities_build`).
- Audit `brain_rate` / `graph_retrieved` graph-target coverage; close any gap so the
  graph feedback path is reachable via MCP too.
- **Acceptance:** the set of `brain graphrag` CLI subcommands (incl. `communities *`)
  has a 1:1 `brain_graphrag_*` MCP counterpart (build/refresh/search/themes/entity/
  entities/stats/communities/communities_build/communities_refresh).

### C. Routing wiring

1. **Global `~/.claude/CLAUDE.md` "Second Brain" section** — add a concise graph-vs-search
   rule: themes / patterns / connections / recurring topics / "how my thinking evolved" /
   "what connects A & B" / "map out" → the `brain-graph` skill (`brain graphrag …`);
   lookups / facts / quotes → `brain search`. Always-loaded = highest leverage. **This
   edits the user's personal global file — minimal, additive, get explicit OK before the
   edit lands.**
2. **`consult-brain` SKILL.md** — add the reciprocal hand-off section ("themes/patterns/
   connections across interactions → use `brain-graph`"). `brain-graph` already back-links.

### D. Verification

- **Routing smoke (manual):** ~6–8 canonical prompts (theme/connection vs lookup) with
  the expected route (`brain-graph` vs `consult-brain`), run in a fresh conversation.
  Documented in this spec; synthetic names only.
- **Parity test (automated):** a pytest that asserts the `brain graphrag` CLI subcommand
  set == the `brain_graphrag_*` MCP tool set (introspect the Typer app + the MCP tool
  registry), so they never silently drift again. This is the durable guard.
- `bin/brain-skills-sync --check` returns clean after install.

## Constraints / safety

- **No PII** in skills/docs/tests/commit messages — synthetic only (rule 15).
- **CLI↔MCP parity** is the standing directive — the parity test enforces it.
- **Don't touch** the ~240 non-brain global skills.
- The **global `~/.claude/CLAUDE.md`** edit is on the user's personal file — additive,
  reviewed, explicit OK before it lands.
- **No commit/push without explicit user permission** (rule 4).
- Post-phase **rule-14 loop** (code review + completion audit) before done.

## Task breakdown (phased, for team-driven-development)

1. **T1 — `bin/brain-skills-sync`** (copy + `--check`) + a tiny test (idempotency, scope
   restricted to brain family, drift detection).
2. **T2 — `brain-graph` skill** updates (entities/stats, modes verify, de-experimentalize,
   MCP list).
3. **T3 — MCP descriptions rewrite** (B2) + **new tools** `brain_graphrag_refresh` /
   `brain_graphrag_communities_refresh` + `brain_rate` graph-target audit (B3).
4. **T4 — Parity test** (D) — CLI subcommands == MCP tools.
5. **T5 — `consult-brain` cross-link** + **global `CLAUDE.md` routing paragraph** (the
   latter gated on explicit user OK since it's their personal file).
6. **T6 — Run `bin/brain-skills-sync`** to install, then the manual routing smoke.

## Testing

- `ruff check && mypy src/ && pytest` green (new parity test + sync-script test included).
- MCP additions covered by tests mirroring existing `brain_graphrag_*` tool tests.
- No PII in fixtures.
