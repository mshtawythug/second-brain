# Derived Edges in `_ingested/` Bodies — Design Spec

**Date:** 2026-04-30
**Owner:** Pat Morgan
**Status:** Draft — pending approval
**Parent feature:** `docs/specs/2026-04-29-metadata-aware-linker-design.md`

## Goal

Make `derived_links` visible in Quartz's native graph view by materializing each ingested document's derived partners as `[[brain:<id>]]` references inside a fenced auto-section appended to its `_ingested/<source>/<file>.md` body.

After this lands, Quartz's `/graph` page reflects the full link graph — wiki-links *and* metadata-derived edges — without any Quartz-side changes.

## Why

The original linker spec treated body rewrites as a non-goal (§11):

> *"No body rewrites. Derived edges live in the DB; the Markdown files in `_ingested/` and the vault are untouched."*

That was the right call for v1: it kept `_ingested/` files clean, kept the sync engine simple, and avoided embedding noise. Once shipped to production, the user observed that **Quartz's graph view stayed disconnected** because Quartz reads Markdown files, not our DB. The 81,356 derived edges existed but were invisible to the user's primary visualization tool.

This spec reverses non-goal §11 #1, but **constrains the rewrite to a fenced auto-section** so the original concerns (embedding pollution, content_hash drift, sync ambiguity) stay solved.

## Design

### Fenced auto-section

Each `_ingested/<source>/<file>.md` body gets a single appended section, bracketed by HTML-comment markers:

```markdown
[original document body — frontmatter + content unchanged]

<!-- BRAIN_DERIVED_START -->
## Related (auto-generated, do not edit)
- [[brain:<dst-id>|<dst-title>]] *(<rule>)*
- [[brain:<dst-id>|<dst-title>]] *(<rule>)*
<!-- BRAIN_DERIVED_END -->
```

The markers are stable across relinks. Content between them is deterministic from `derived_links` — sorted, deduplicated.

Vault-tier files (user-authored, outside `_ingested/`) are **not annotated** in v1. The user's authored content stays untouched. (See §10 Q4.)

### Sync engine awareness

The sync engine recognizes the fence and treats it specially:

| Operation | Behavior |
|---|---|
| `body_hash(text)` | Strip fence + content before hashing → file with stale fence has same hash as same file with fresh fence → no re-embed loop |
| `_normalized_body(body)` for DB storage | Strip fence → `documents.content` only stores user/upstream content → embeddings + search clean |
| `parse_wiki_links(body)` | Skip fence → `[[brain:<id>]]` references inside the fence are NOT materialized into the `links` table → no double-count with `derived_links` |
| Re-sync of a file with a fenced section | Recompute fence from current `derived_links` → idempotent |
| `brain vault watch` | Debounce: ignore changes when only the fence content differs → no write-loop |

### Render path

After every successful `rebuild_derived_for(conn, doc_ids, ...)` (sync or relink-derived), the linker also calls `rewrite_derived_fences(conn, affected_doc_ids)`:

1. Compute `affected_doc_ids = doc_ids ∪ {every doc that gained/lost an edge in the rebuild}`. For each affected doc that's an `_ingested/<source>/...` mirror file:
2. Query `derived_links WHERE src OR dst = doc_id`, JOIN `documents` for partner titles, ORDER BY rule weight DESC, partner title ASC.
3. Render the fenced section from the result set.
4. Read the file, replace the fenced region (or append if absent), write atomically.
5. Skip files where the fence content is byte-identical to what's already there (no-op writes → no mtime churn → no Quartz rebuild trigger).

### Why fenced + strip-before-hash

The fence is what makes this safe:
- **Content hash stability:** stripping the fence before hashing means a file with a freshly-rendered fence still hashes the same as the original. No re-embed cascade.
- **Embedding cleanliness:** the auto-section never enters `documents.content`, so vector search doesn't surface docs by their "Related" list.
- **Wiki-link discipline:** the parser skips the fence, so `[[brain:<id>]]` inside it doesn't double-count edges that are already in `derived_links`.
- **User edit safety:** anything outside `BRAIN_DERIVED_START..END` is preserved. Users editing the upstream content of an `_ingested/` file (rare but possible) keep their changes; only the fence regenerates.

## Open questions (resolve before implementation)

**Q1. Sort order inside the fence.** Options:
- (a) Rule weight DESC, then partner title ASC. R1 (thread) edges first, R3 (same-day) next, R2 (participant) last. Visually communicates confidence.
- (b) Partner title ASC. Predictable alphabetical scan.
- (c) Date DESC (most recent first). Useful for activity views; requires `metadata->>'date'` parse per row.

Recommendation: **(a)** — weight-first matches the visual tier styling we shipped in C.2.

**Q2. Display format inside the fence.** Options:
- (a) `[[brain:<id>|<title>]] *(<rule>)*` — explicit alias + rule.
- (b) `[[<title>]] *(<rule>)*` — title-only, falls back to brain prefix only on title collision.
- (c) `[[brain:<id>]] *(<rule>)*` — id only.

Recommendation: **(a)** — robust against title collisions, gives Quartz a readable label.

**Q3. Vault-tier files.** Options:
- (a) `_ingested/` only (v1). User-authored notes stay untouched.
- (b) Vault-tier too, with a "do not edit fence" warning.

Recommendation: **(a)** for v1. Annotating user-authored files is a different trust boundary; defer.

**Q4. Per-file regenerate threshold.** Options:
- (a) Always rewrite affected files, even if the fence content is byte-identical.
- (b) Skip writes when the fence content is unchanged (mtime-stable; Quartz doesn't rebuild).

Recommendation: **(b)** — saves git churn AND saves Quartz rebuild cycles.

**Q5. Quartz reads `[[brain:<id>]]` how?** Quartz uses Obsidian-style `[[file-or-page-name]]` linking, not custom prefixes. Our `brain:<id>` won't resolve to a node by default. Options:
- (a) Use Quartz's `aliases` frontmatter — every `_ingested/<source>/<file>.md` already has `id: <uuid>` in frontmatter. We can update the export to also emit `aliases: ["brain:<id>"]` so Quartz resolves the link.
- (b) Replace `brain:<id>` with the partner's actual title (after a DB lookup at render time). Cleaner for Quartz; slightly more work in the renderer.

Recommendation: **(b)** — render the partner's title directly. The fence stores `[[partner-title|<id>-suffix]]` if you want the id visible. Resolves natively in Quartz.

## Non-goals

- No vault-tier annotation (Q3 deferred).
- No new MCP tool for "show me my derived links via Quartz" — Quartz IS the surface.
- No Quartz plugin or custom Quartz config — pure Markdown changes only.
- No new schema or migration — existing `derived_links` table is sufficient.
- No new linker rules — read-side only.

## Risk surface

- **Git churn.** Even with the byte-identical skip (Q4 recommendation), every meaningful corpus change causes some files to update. Acceptable for a hand-curated vault; might pile up if relink runs daily.
- **Watch-mode loops.** Mitigated by the body-hash strip (sync sees no body change) plus the byte-identical-skip in the renderer.
- **Quartz title resolution** (Q5). If recommendation (b) is taken, the fence references partner titles. Title collisions (two docs with the same title) would point ambiguously. Mitigation: append a short id suffix in the alias.
- **Encoding drift.** Markdown extensions (Obsidian's, Quartz's, GFM) sometimes parse HTML comments differently. The `<!-- BRAIN_DERIVED_START -->` markers are universal CommonMark — should be safe, but worth a Quartz-render smoke test in the plan.
