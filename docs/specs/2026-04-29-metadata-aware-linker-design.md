# Metadata-Aware Linker — Design Spec

**Date:** 2026-04-29
**Owner:** Pat Morgan
**Status:** Draft — pending approval (open questions in §10 must be resolved before implementation)

## 1. Goal

Add a second link-source to the vault graph: edges derived from **document metadata overlap**, not just `[[wiki-links]]`. The motivating case is Krisp call notes ↔ Gmail email threads with overlapping people/dates — they are unlinked today because neither extractor emits `[[...]]`.

The result the user should see:

- `brain backlinks <gmail-doc>` lists Krisp meetings that mention the same people on or near the same day, even when neither body contains a wiki-link.
- `brain graph` shows edges between Krisp clusters and Gmail clusters.
- A vault note's `[[wiki-links]]` continue to take precedence; metadata-derived edges are additive.

## 2. Why

The vault graph today is built entirely from `[[wiki-links]]` parsed out of Markdown bodies (`src/brain/vault/links.py:62`, materialized at `src/brain/vault/sync.py:910`). Ingested artifacts (Krisp transcripts via `src/brain/ingest/stdin.py:7`, emails via `src/brain/ingest/gmail.py:157`) never emit wiki-links, so the only edges they ever participate in are ones a vault-tier note explicitly authored against them. In practice that means **Krisp ↔ Gmail edges do not exist** even when the same person appears in both on the same day.

The DB already holds enough metadata to infer most of these edges (Gmail: `from`, `to`, `date`, `thread_id`; Krisp: `date`, `krisp_meeting_id`, optionally `participants`). What is missing is the code that joins on it.

## 3. Current state (grounded in code + live DB)

Reading the live DB (`docker exec second-brain-postgres psql -U brain -d second_brain`):

```
SELECT s.kind, jsonb_object_keys(d.metadata), count(*) ...

 gmail | date / from / to / thread_id / message_id / label_ids   (390 rows)
 krisp | date / krisp_meeting_id                                  (67 rows)
```

**Important reality check:** The 67 existing Krisp rows do **not** have a `participants` field in `documents.metadata`. The `CLAUDE.md` ingest guidance documents `participants` as a metadata key, but historical rows were ingested without it. Any participant-based matching either (a) requires a backfill or (b) only applies to rows ingested after the spec lands — see §10 Q1.

Other relevant code paths:

- `src/brain/vault/graph.py:241` (`graph_data`) reads exclusively from the `links` table. Whatever this spec inserts into `links` (or a sibling table) is what `brain graph`, `brain backlinks`, and the Quartz export will surface.
- `src/brain/vault/sync.py:1046` (`_retry_unresolved`) is the existing post-pass hook scoped to recently-touched documents. The metadata-link pass would be a sibling of it.
- The `links` schema (`migrations/003_vault_model.sql:18`) has `link_kind TEXT CHECK (link_kind IN ('wiki', 'embed'))` and a `UNIQUE (src_document_id, dst_document_id, link_text, link_kind)` constraint. A new `link_kind` value would require either widening the CHECK (additive migration) or a sibling table.

## 4. Approach (chosen)

**Option 1 from the discussion: a metadata-aware linker pass that writes derived edges into a new sibling table, `derived_links`.**

Why a sibling table rather than reusing `links`:

- `links.link_text` is the raw `[[X]]` from the body — meaningless for derived edges. Stuffing a synthetic value (`"[[derived:participant=jane]]"`) muddies the existing semantics, and the unique constraint built around `link_text` becomes awkward.
- Keeping derived edges separate makes `brain backlinks` honest — it can label the edge as "via shared participant", "via same thread", etc., without parsing it back out of `link_text`.
- Wiping and rebuilding the derived table on every sync is cheap; wiping `links` would risk a bug in derivation taking out user-authored wiki-links.
- Future widenings (e.g., adding semantic-similarity edges) plug in without touching the wiki-link path.

The cost is one extra UNION in `graph_data`'s edge query and one extra branch in `backlinks_for` / `outgoing_links_for`. Both are trivial.

## 5. Schema

New migration `005_derived_links.sql` (additive, no changes to existing tables):

```sql
-- Edges derived from metadata overlap; sibling of `links` (which holds
-- wiki-link edges only). See §4 for why a separate table.
CREATE TABLE derived_links (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  src_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  dst_document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  rule            TEXT NOT NULL,             -- 'shared_thread' | 'shared_participant' | 'same_day_participant'
  evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"thread_id": "..."} or {"participant": "Jane Doe", "date": "2026-04-15"}
  weight          REAL NOT NULL DEFAULT 1.0, -- for future tie-breaking / Quartz styling
  CHECK (src_document_id <> dst_document_id),
  UNIQUE (src_document_id, dst_document_id, rule)
);
CREATE INDEX derived_links_src_idx ON derived_links (src_document_id);
CREATE INDEX derived_links_dst_idx ON derived_links (dst_document_id);
CREATE INDEX derived_links_rule_idx ON derived_links (rule);

-- Persistent name↔email directory used by R2/R3 to bridge Krisp speaker
-- labels (often a name) with Gmail headers (always an email). Built
-- incrementally from already-stored Gmail headers, MCP calendar events,
-- MCP contacts, and an optional override file (see §6 R2 + §10 Q2).
CREATE TABLE directory_entries (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name    TEXT NOT NULL,             -- normalized lowercase
  email           TEXT NOT NULL,             -- normalized lowercase
  source          TEXT NOT NULL CHECK (source IN ('gmail', 'calendar', 'contacts', 'people_yml')),
  occurrence_count INT NOT NULL DEFAULT 1,
  first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (display_name, email, source)
);
CREATE INDEX directory_entries_name_idx ON directory_entries (display_name);
CREATE INDEX directory_entries_email_idx ON directory_entries (email);

-- High-water mark per source so calendar / contacts refreshes are
-- incremental: each refresh fetches `(last_refreshed_at, now]` only.
-- Gmail rows update this lazily — they don't drive an MCP call.
CREATE TABLE directory_refresh_state (
  source              TEXT PRIMARY KEY CHECK (source IN ('gmail', 'calendar', 'contacts')),
  last_refreshed_at   TIMESTAMPTZ NOT NULL,
  records_seen        INT NOT NULL DEFAULT 0
);
```

Edges are **undirected** in semantics but stored as a single directed row `(min(id), max(id))` to keep the unique constraint simple and the table half-size. Read paths normalize.

## 6. The matching rules (v1 — kept deliberately small)

Each rule is a pure function `(doc_a, doc_b) -> Evidence | None`. The pass iterates over candidate pairs and inserts edges for non-None outcomes. Three rules in v1:

### R1. `shared_thread` (Gmail ↔ Gmail only)

Two Gmail documents share an edge iff they share a `thread_id`. This is the highest-confidence rule and the only purely-Gmail one. Trivially correct.

### R2. `shared_participant` (Krisp ↔ Gmail, Krisp ↔ Krisp, Gmail ↔ Gmail across threads)

A "participant" is normalized to a single canonical key — preferably an email address, falling back to a normalized name when no email mapping exists. Per-source extraction:

- **Gmail:** tokenize `metadata.from` and `metadata.to` into RFC-5322 addresses (`email.utils.getaddresses` from stdlib). Each address yields both an email and (when present) a display name — both are recorded.
- **Krisp:** parse the transcript body's speaker labels — Krisp writes them inline as `**<name-or-email> | mm:ss**`. When the captured label contains `@`, treat as email. Otherwise, look up the name in the **directory** (see below) — if exactly one email is registered, use that as the canonical key. If zero or multiple, fall back to the normalized-name form. Speaker labels of the form `Speaker_1`, `Speaker_2`, etc. are unidentified and **dropped** (they would over-link unrelated calls). The Krisp MCP's structured fields (`speakers`, `attendees`, `agenda`) intentionally **do not** feed this rule — they only carry display names with no emails (verified empirically — see §10 Q1 resolution).

The participant set for each document is materialized into `documents.metadata->'_participant_keys'` (a JSONB array of normalized lowercase strings) on every sync. Reading it back is a single SELECT; the join is a Python set-intersection at personal-corpus scale (~500 rows). An edge fires when both documents share at least one key. Evidence stores the matched key plus the original token form.

**The directory** (§5 `directory_entries`) bridges Krisp's name-only speaker labels with Gmail's email-only headers. It is **derived from data we already have** and refreshed incrementally:

- **Gmail headers** (primary): every Gmail ingest parses `metadata.from`/`metadata.to` and upserts `(display_name, email)` rows with `source='gmail'`. No MCP call — purely local. This alone solves the user's own bridge: "Pat Morgan ↔ redacted@example.com" appears in the `from` of every email the user has sent.
- **Google Calendar via MCP** (secondary): each Krisp ingest triggers an incremental `mcp__claude_ai_Google_Calendar__list_events` call with `(timeMin=last_refreshed_at, timeMax=now)` — keeps the directory current with people on the user's invites without re-scanning the whole year. **First-run look-back is YTD** (Jan 1 of current year → today) — caps the initial scan to a few hundred events while comfortably covering all 67 historical Krisp transcripts (oldest is 2026-02-13). Attendee `email` + `displayName` upserted with `source='calendar'`. The high-water mark is tracked in `directory_refresh_state`.
- **Google Contacts via MCP** (tertiary): rate-limited to one refresh per 24h (cheap on the user's side, lower-signal than Gmail/Calendar). `source='contacts'`. Refreshes lazily on any ingest that finds the state table older than the threshold.
- **`_people.yml` override** (`~/brain-vault/_people.yml`, alongside `_templates/`, `_attachments/`, `_ingested/`): optional flat file mapping `Display Name: person@example.com`. Wins over derived data when present. Stays empty for most users; exists for the rare case where automated sources disagree or miss someone.

**Conflict resolution** (one name → multiple emails): default policy is **skip ambiguous names** — the lookup returns no canonical key and the linker falls through to the normalized-name form. The `_people.yml` override is the explicit way to disambiguate (e.g., `Jane Doe: jane.doe@example.com` pins the canonical key). R3's same-day date constraint already cuts most cross-cluster noise, so a missed weak edge is a cheap cost compared to a false-positive cluster.

**Refresh triggers** (per Q2 resolution):

| Trigger | Gmail entries | Calendar entries | Contacts entries |
|---|---|---|---|
| Gmail ingest (`brain ingest-gmail`) | parse + upsert from this row | — | — |
| Krisp ingest (`brain ingest-stdin --source krisp`) | — | incremental MCP call from `last_refreshed_at` | only if state >24h stale |
| `brain vault relink-derived` | — (Gmail rows already covered) | YTD on first run; incremental thereafter | full refresh |
| `brain vault directory refresh` (manual) | full rebuild from all Gmail rows | YTD scan | full refresh |

### R3. `same_day_participant` (Krisp ↔ Gmail)

Strictly stronger than R2 by also requiring the documents' `metadata.date` to fall within ±1 calendar day of each other (timezone-naive — matches how the data is stored today). This rule is the user's canonical case: "the Krisp call I had with Jane Doe on Tuesday and the email exchange with Jane Doe around the same day."

Implementation note: R3 subsumes R2 for Krisp ↔ Gmail. We still emit R2 separately when only the participant matches but the dates do not — those edges are weaker and the formatter can dim them.

### Out of scope for v1 (deferred)

- Subject-line ↔ transcript-title fuzzy match. Too noisy without further filtering.
- Calendar event ↔ Krisp meeting matching. Calendar isn't ingested today. Note: the Krisp MCP's `list_upcoming_meetings` exposes `is_external`, `organizer`, `is_recurring`, `participants` (names only), and conferencing URL for future calendar entries — a reasonable input to a future "calendar ingest" feature, but separate from this spec.
- Slack thread ↔ Gmail thread matching. Slack isn't ingested in any volume yet.
- Semantic-similarity edges (vector cosine over chunks). Different feature; do not bundle.

## 7. Where the code lives

```
src/brain/vault/
  derived_links/
    __init__.py            (new) — public surface
    rules.py               (new) — pure rule R1/R2/R3 implementations
                                  + Evidence dataclass
    participants.py        (new) — normalize_participant(token) -> str | None
                                  + extract_krisp_speakers(body) -> set[str]
                                    (parses ``**name-or-email | mm:ss**`` labels;
                                     drops Speaker_N placeholders)
    directory.py           (new) — DirectoryStore: read/upsert directory_entries,
                                  resolve_name_to_email(name) -> str | None,
                                  refresh_calendar(conn, since, until),
                                  refresh_contacts(conn),
                                  load_people_yml(vault_path) -> dict[str, str]
    pass_runner.py         (new) — _rebuild_derived_for(conn, doc_ids)
  sync.py                  — call pass_runner after _retry_unresolved in
                             sync_vault() and after the per-file retry in
                             sync_one_file(). On Gmail ingest path, also
                             upsert directory_entries from the new row.
                             On Krisp ingest path, trigger incremental
                             calendar refresh (if `last_refreshed_at` < now)
                             and contacts refresh (if state >24h stale).
  graph.py                 — extend GraphEdge with optional `rule: str | None`
                             (None for wiki-links); UNION derived_links into
                             graph_data() and outgoing_links_for() / backlinks_for()
                             behind a flag include_derived: bool = True
  graph_format.py          — render derived edges as dotted/dashed in Mermaid
                             and DOT outputs; JSON includes the rule + evidence

src/brain/queries.py       — derived_link_count_for(document_id) helper used
                             by `brain status` (cheap visibility)
                           — directory_size() helper (per-source counts)

src/brain/cli.py           — `brain vault relink-derived` (full corpus rebuild)
                           — `brain vault directory refresh` (manual rebuild)
                           — `brain vault directory show` (debugging — print
                             entries grouped by source)

migrations/
  005_derived_links.sql    (new) — derived_links + directory_entries
                                   + directory_refresh_state (see §5)

tests/
  test_derived_links.py    (new) — unit tests per rule, fakes for DB rows
  test_sync_derived.py     (new) — integration: ingest fixtures, sync, assert
                                   edges exist; assert no false positives
                                   from a "Jane-vs-Jane-Doe" name collision
                                   case
  test_graph_derived.py    (new) — graph_data + backlinks_for include
                                   derived edges and label them
```

## 8. Algorithm — the pass itself

After every `sync_vault` and `sync_one_file`, run `_rebuild_derived_for(conn, doc_ids)`:

1. **Refresh the directory** for any source that needs it (per the §6 R2 trigger table). Gmail ingests upsert their own row's pairs eagerly; Krisp ingests fire an incremental calendar `list_events(timeMin=last_refreshed_at, timeMax=now)` and a contacts refresh if state is >24h stale. Bump `directory_refresh_state.last_refreshed_at` after each successful refresh.
2. For each `doc_id` in the just-touched set, fetch its `(kind, source.kind, metadata, content)` and compute its **participant set** (using the directory to bridge name → email when possible) + **date**. Persist to `documents.metadata->'_participant_keys'`.
3. For each touched doc, query candidates:
   - **R1:** other Gmail docs with the same `thread_id` (cheap, indexed).
   - **R2/R3:** other Gmail/Krisp docs whose participant set intersects (Postgres `?|` operator over `metadata->'_participant_keys'` — or, for v1, a Python-side hash join after pulling all metadata rows; corpus is ~500 rows, fits in RAM).
4. Open a transaction. `DELETE FROM derived_links WHERE src_document_id = ANY(%s) OR dst_document_id = ANY(%s)`. Then bulk-insert the new edges.
5. Commit.

Key correctness properties:

- **Deterministic ordering** — store `(LEAST(a, b), GREATEST(a, b))` so the same pair always lands as the same row.
- **No self-edges** — `CHECK (src <> dst)` plus a guard in the rule functions.
- **Rebuild scope is the touched set** — incremental sync stays incremental. A full `brain vault sync` rebuilds derived edges only for the docs it processed in that run, plus their newly-discovered partners (which it processes via the same loop).
- **Tier independence** — derived edges work for both `kind='vault'` and `kind='ingested'`. The graph view's existing `vault_only` / `include_ingested` flags continue to govern what the user sees.

A separate one-shot CLI command — `brain vault relink-derived` — runs the pass over the **entire** corpus. Needed once after this lands (to populate edges for the existing 457 ingested rows) and useful when the matching rules change. Idempotent.

## 9. CLI surface (minimal)

- `brain backlinks <id>` — already exists. Add a `[derived: rule]` annotation per row when the edge came from `derived_links`.
- `brain links <id>` — same treatment for outgoing.
- `brain graph [--no-derived]` — opt-out flag for users who want the pure wiki-link view.
- `brain vault relink-derived` — one-shot full rebuild (whole corpus). Triggers a full directory refresh (Gmail rescan + YTD calendar + contacts).
- `brain vault directory refresh` — manual directory rebuild without re-running the linker.
- `brain vault directory show [--source S]` — print directory entries (debugging).

No new top-level command groups. No config knob in v1; matching rules are baked in.

## 10. Open questions (for user decision before implementation)

**Q1. Krisp participants backfill — RESOLVED 2026-04-29.** The 67 existing Krisp rows have no `participants` in metadata. After exhausting the Krisp MCP surface (`search_meetings`, `list_upcoming_meetings`, `list_action_items`, `get_user_preferences`, `get_multiple_documents` — see §3 reality-check), I confirmed that **no MCP tool exposes participant emails as structured metadata**. `search_meetings` returns `speakers: ["Pat Morgan", "Jordan"]` (names only, sometimes truncated); `list_upcoming_meetings` returns `participants: ["Pat Morgan", "Jordan Vale"]` (names only); agenda documents are empty (`""`) for every meeting tested, including calendar-driven ones. The **only** place external-participant emails appear is **inline in the transcript body's speaker labels** — Krisp writes them as `**jordan.vale@example.com | 00:31**` when the speaker is identified via calendar/contact match but is not a workspace user.

**Decision:** Option **(c′)** — parse speaker labels out of the stored transcript bodies. Regex `^\*\*(.+?) \| \d+:\d+\*\*$` over the multiline body yields the participant set per row. No MCP calls. Idempotent. Works on all 67 historical rows.

**Why this is strictly better than the originally-proposed (b):** (i) zero MCP rate-limit / network risk, (ii) the transcript labels carry emails for external participants — exactly the form Gmail's `from`/`to` are keyed on, which is precisely what cross-source linking needs, (iii) the data is already in our DB (`documents.content`), (iv) `Speaker_1` / `Speaker_2` / etc. fall out naturally as "unidentified" and are dropped, avoiding false positives from calls where Krisp couldn't ID anyone.

**Implementation:** the parser is a small pure helper in `derived_links.py` (or a sibling `transcript_parser.py`). The participant set lands in `documents.metadata->'_participant_keys'` and is rebuilt during sync for any Krisp document whose body has changed. The first run of `brain vault relink-derived` does the historical backfill in the same pass.

**Q2. Name ↔ email bridging — RESOLVED 2026-04-29.** Krisp gives names ("Jordan Vale"); Gmail gives addresses (`<jordan.vale@example.com>`). R1 of Q1's resolution narrowed the gap (transcript bodies carry emails for external participants), but the user side stays as a name. We need a directory.

**Decision:** **derived directory, not hand-maintained**, with optional override. Four sources, in confidence order:

1. **Gmail headers** (primary): every ingested Gmail row's `metadata.from`/`metadata.to` is parsed via `email.utils.getaddresses` into `(display_name, email)` pairs and upserted into `directory_entries` with `source='gmail'`. **Zero new MCP calls — the data is already in our DB.** Solves the user's own bridge automatically (every email they've sent has `From: "Pat Morgan" <redacted@example.com>`).
2. **Google Calendar via MCP** (secondary): incremental `mcp__claude_ai_Google_Calendar__list_events(timeMin, timeMax)` calls populate `source='calendar'`. **First-run look-back: YTD** (Jan 1 of current year → today). Comfortably covers the existing Krisp corpus (oldest transcript: 2026-02-13).
3. **Google Contacts via MCP** (tertiary): rate-limited to one refresh per 24h.
4. **`~/brain-vault/_people.yml`** (override): optional flat YAML, `Display Name: person@example.com`, lives at vault root next to `_templates/`/`_attachments/`/`_ingested/`. Wins over derived data when present. Stays empty for most users.

**Refresh triggers** (per user direction):

- Every Gmail ingest → upsert that row's pairs (purely local, no MCP).
- Every Krisp ingest → incremental calendar refresh `(last_refreshed_at, now]` + contacts refresh if state >24h stale.
- `brain vault relink-derived` → full refresh (Gmail rescan + YTD calendar + contacts).

State table `directory_refresh_state` tracks `last_refreshed_at` per source (see §5).

**Conflict resolution:** when a name maps to multiple emails, default policy is **skip ambiguous names** (lookup returns no canonical key; linker falls through to normalized-name form). The `_people.yml` override is the explicit way to disambiguate. R3's same-day constraint already cuts most cross-cluster noise, so a missed weak edge is cheap; a false-positive cluster is more annoying.

**Why this beats the original (b)/(c) framing:** the user shouldn't maintain 50 lines of YAML when 390 Gmail rows already encode the mappings. The Calendar MCP fills the "people you've met but never emailed" gap (Krisp records meetings; calendar invites have their emails). Contacts is a cheap third source. The YAML file shrinks to a 0-3 line override file, used only for edge cases the automated sources miss.

**Q3. Edge weights / styling — RESOLVED 2026-04-29.** Edges from different rules render visually distinct in `brain graph`, not just in JSON.

**Tier mapping** (assigned at insert time into `derived_links.weight`):

| Rule | weight | Mermaid | DOT |
|---|---|---|---|
| (wiki-link, from `links` table — for reference) | n/a | `-->` solid black | `solid`, black |
| R1 `shared_thread` | `1.0` | `==>` bold | `bold`, black |
| R3 `same_day_participant` | `0.7` | `-->` solid | `solid`, gray |
| R2 `shared_participant` | `0.4` | `-.->` dotted | `dotted`, light gray |

The rationale: wiki-links remain the user's authoritative thinking surface (clean black solid). R1 (shared_thread) is the strongest derived signal and earns the bold treatment. R3 (same_day_participant) is medium-confidence and reads as a normal solid edge but in gray. R2 (shared_participant only, no date proximity) is the noisiest and renders as dotted/light to visually subordinate it. JSON output always includes `rule` and `weight` for downstream consumers regardless.

**CLI annotation in `brain backlinks` / `brain links`:** rule name only — `[derived: same_day_participant]`. The numeric weight is noise for a human reader; the rule name already conveys the tier. JSON output (`--json`) carries the weight for programmatic use.

**Q4. Including derived edges in `brain graph` by default — RESOLVED 2026-04-29.** Derived edges are **on by default** in every read path; opt-out via `--no-derived` on `brain graph`.

- `brain graph` includes derived edges, styled per Q3's tier mapping (visually subordinated when low-confidence).
- `brain backlinks <id>` and `brain links <id>` always include derived rows, annotated `[derived: <rule>]`. No `--include-derived` flag — derived rows are first-class answers to "what's connected to this doc?"
- `brain graph --no-derived` and (deferred) per-source filters give the user an escape hatch.

The visual tiering from Q3 is what makes on-by-default safe: noisy R2 edges render as faint dotted lines, not as solid edges indistinguishable from wiki-links. If R2's noise floor turns out to be too high in practice (after running on the live corpus), the right fix is to tighten the rule (e.g., require an `@` in the participant token, downgrading name-only matches), not to gate the whole feature behind a flag.

## 11. Non-goals

- ~~No body rewrites. Derived edges live in the DB; the Markdown files in `_ingested/` and the vault are untouched.~~ **Reversed by Phase D (2026-04-30).** Each `_ingested/<source>/<file>.md` body now gets a fenced auto-section (`<!-- BRAIN_DERIVED_START --> … <!-- BRAIN_DERIVED_END -->`) re-rendered on every relink so Quartz's native `/graph` view picks up derived edges as wiki-links. The fence is contractually NOT authored body — the sync engine's `body_hash`, `_normalized_body`, `_legacy_body_hash`, and `_materialize_links` all strip it before hashing/storing/parsing, so the rewrite is invisible to embeddings, search, and the wiki-link table. Vault-tier (user-authored) files remain untouched in v1 (Q3=a). See `docs/specs/2026-04-30-derived-edges-in-bodies-design.md` for the full Phase D design and `docs/plans/2026-04-30-derived-edges-in-bodies.md` for the implementation plan.
- No new wiki-link syntax for derivation. Users continue to author `[[X]]` only.
- No graph-database substrate. Postgres + a sibling table covers personal-corpus scale.
- No real-time inference at search time. Edges are materialized on sync; reads are SELECTs.
- No user-tunable thresholds in v1 (rules are pure boolean predicates).
- No Slack matching in v1 (no volume of Slack data to validate against).

## 12. Test plan

Per `CLAUDE.md` testing standards (≥85% coverage, real-Postgres integration tests, regression test for any bug fix):

- **Unit (`test_derived_links.py`):** each rule against constructed metadata dicts. Cover normalization edge cases: `"Pat Morgan <redacted@example.com>"` → email key `redacted@example.com`; bare `"Jane"` → name key `jane`; mixed case; missing fields.
- **Integration (`test_sync_derived.py`):** seed two Krisp + two Gmail docs in a real test DB, run sync, assert exactly the expected edges exist, with the right `rule` and `evidence`.
- **Negative tests:** two docs sharing only a common-name participant ("John") should not over-link. R3 must require date proximity.
- **Idempotence:** two consecutive `relink-derived` runs produce identical row sets.
- **Regression hooks:** any future bug fix on a rule lands with a test reproducing the prior false-positive / false-negative.

## 13. Phasing

If approved, implementation breaks into ~3 phases. Per `CLAUDE.md` Team Mode Override, execution would be via `team-driven-development`.

- **Phase A — Schema + pure rules + unit tests.** Migration 005, `derived_links.py`, comprehensive unit tests. No sync integration yet.
- **Phase B — Sync integration + `brain vault relink-derived`.** Wire the pass into `sync_vault` and `sync_one_file`. Add the one-shot CLI command. Backfill the existing corpus.
- **Phase C — Read paths.** Extend `graph.py`, `backlinks_for`, `outgoing_links_for`, formatters, and CLI annotations. Update `MEMORY.md` files (`schema.md`, `cli.md`, `types.md`).

A separate small phase if Q2 = (b): **Phase B' — `_people.yml` loader** (small file parser + alias merge into normalize_participant).

## 14. Risk surface

- **False positives.** A common name (e.g., "Sarah") in two unrelated docs would link them. Mitigation: prefer email keys over name keys; R3's date constraint cuts most cross-cluster noise; user can disable derived edges via `--no-derived`.
- **Over-eager rebuild.** A full-vault sync that touches every doc would `DELETE` and re-INSERT every derived edge. Cost is bounded by edge count, not doc count squared, but worth measuring once on the live corpus.
- **Schema migration on a populated DB.** Migration 005 is purely additive (new table, new indexes). Per `feedback_db_safety.md` rule: additive-only, no destructive ops. Backup before running.
- **Tight coupling to current metadata shape.** Gmail's `from` / `to` parsing has to handle every RFC-5322 wart. Mitigation: lean on `email.utils.getaddresses` from stdlib; unit-test the gnarly cases.
