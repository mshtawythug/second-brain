# RM-19 — Near-duplicate detection (flag, never refuse)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-19 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(roadmap 3.4, backlog rank 11, the Ingest spec's risk R4). The ticket is the spec; this is the task
list, the rulings and the record of what changed from the ticket.
**Base:** `second-brain/master` `1c917de` (RM-06, RM-10, RM-11, RM-13, RM-15, RM-16 and RM-17 merged).
**Branch:** `feat/rm-19-near-duplicates`.

## Goal

The owner sees pairs of documents that are near-copies (forwarded emails, re-sent drafts, a file
ingested twice by two paths) and can confirm or dismiss each pair. Nothing is ever refused, merged
or deleted (RM-19.D1).

## Files

| File | Change |
|---|---|
| `src/brain/migrations/030_near_duplicates.sql` | **New.** `document_minhash` and `near_duplicates` exactly as the ticket gives them, plus the GIN index on `bands`; wrapped in `BEGIN`/`COMMIT`, every statement `IF NOT EXISTS`. |
| `src/brain/near_dup.py` | **New pure leaf.** `words`, `token_count`, `shingles`, `signature`, `band_hashes`, `to_signed64`, `estimated_jaccard`, `meets_cut` (the one definition of the 0.85 cut, inclusive; the store verifies every candidate through it), `same_thread`, `ordered_pair`, `compute` → `MinHash`. No I/O, no new dependency. |
| `src/brain/near_dup_store.py` | **New.** Every SQL statement: `tables_present`, `scan` → `ScanReport`, `refresh_document`, `check_document` → `list[DuplicateRef]`, `list_pairs` → `list[PairRow]`, `count_open`, `set_status`, `brief_lines`. |
| `src/brain/cli_dedup.py` | **New.** `brain dedup scan [--all] [--limit N]`, `list [--status …] [--limit N] [--json]`, `dismiss A B`, `confirm A B`. |
| `src/brain/cli_registry.py` | One import and one `EXTENSION_REGISTRARS` entry, `("dedup", register_dedup)`. |
| `src/brain/mcp_tools/dedup.py` | **New.** `brain_dedup_list(status="open", limit=50, include_confidential=False)`, read-only, the lens on both ends. |
| `src/brain/templates/bin/_brain-brief-fg.sh` | `"$PY" -m brain dedup scan --limit 500 \|\| true` before the `exec … brief --wiki` line. |
| `src/brain/brief.py` | `BriefData.possible_duplicates` (an int, default 0) and its `to_dict` key; `assemble_brief` fills it through its lens; the page and the terminal print one "Possible duplicates (N)" line when N > 0. Budget **+≤ 10**. |
| `src/brain/brief_delivery.py` | `render_delivery` sends the same count line (see ruling R4). |
| `src/brain/ui/ingest_service.py` | After a successful ingest, the job calls `near_dup_store.check_document` and fills `possible_duplicates`; a failure there is logged class-only and leaves the list empty. |
| `src/brain/ui/routes_jobs.py` | `lensed_result` lenses each `possible_duplicates` entry (ruling R5). |
| `src/brain/mcp_server.py` | `brain_brief`'s docstring names the `possible_duplicates` count among the F6-gated keys, reworded in place, **net-zero** (see Deviations). |
| `CLAUDE.md` | The `mcp_server.py` ceiling row gains its net-zero hop, bound to the code commit's SHA by the docs commit (the `bc3b868` precedent). |
| `tests/test_mcp_confidential_discovery_gate.py`, `tests/test_cli_registry_extensions.py` | `brain_dedup_list` joins the gated floor; the extension tuple is `watch`, `dedup`. |
| Tests | See "Tests" below. |
| Docs | `CHANGELOG.md`, `docs/cli-reference.md` (`brain dedup`, the MCP tool), `docs/configuration.md` (the MCP tool row), `skills/brain-proactivity/SKILL.md`, the backlog row (rank 11 / roadmap 3.4), Appendix B (follow-up commit, after the PR exists). |

The only file in the CLAUDE.md ceiling table touched is `mcp_server.py`, net-zero (a docstring
reworded in place). Its row's last hop (`e79cebc`) is already SHA-bound, so nothing is promoted;
the new hop is added, SHA-bound, by the commit after the code commit.

## The algorithm (`near_dup.py`)

- **Words:** `text.lower().split()`, i.e. lowercased with every whitespace run collapsed.
  `token_count` is that word count; the **50-token floor** (`MIN_TOKENS`) is on words, so the pure
  leaf needs no tokenizer (ruling R1).
- **Shingles:** every run of 5 consecutive words (`SHINGLE_WORDS`), joined by one space, hashed to
  64 bits with `hashlib.blake2b(digest_size=8)` (unsigned, big-endian). Fewer than 5 words give the
  empty set. RM-25 reuses `shingles`.
- **Signature:** 128 values (`NUM_PERM`). Hash *i* is `(a_i · x + b_i) mod p` with
  `p = 2⁶¹ − 1`, and its value is the minimum over the shingle set. `a_i ∈ [1, p−1]` and
  `b_i ∈ [0, p−1]` come from `blake2b(f"{SEED}:{i}:a")` / `…:b` reduced mod p, so they are fixed
  across runs, processes and Python versions (no `random`, no `hash()`). Every value is below
  2⁶¹, so it fits `BIGINT` as is.
- **Bands:** 32 bands of 4 rows (`BANDS`, `ROWS`). Band *j*'s hash is
  `blake2b(j.to_bytes(1, "big") + the four values as 8-byte big-endian, digest_size=8)`, converted
  with `int.from_bytes(d, "big", signed=True)` (`to_signed64`) so it fits `BIGINT`.
- **Verification:** the estimated Jaccard (the fraction of the 128 positions that agree) must be
  `≥ 0.85` (`JACCARD_CUT`). LSH only proposes candidates; nothing is recorded unverified.
- **Exclusions:** a document under 50 words is never signed or paired; two documents with the same
  non-null `documents.thread_id` are never paired (Gmail thread assembly already handles them).

## Data flow (`near_dup_store.py`)

- **Stale** = no `document_minhash` row, or its `content_hash` differs from `documents.content_hash`.
  `scan` refreshes stale documents (every document with `--all`), oldest first, at most `limit`.
  Each document is refreshed in its own transaction, so a capped or interrupted scan keeps what it
  did.
- **A short document** gets a row with empty `signature` and `bands` (ruling R2), so a scan does
  not revisit it until its content changes, and it never matches a band.
- **Refreshing a document** writes its signature, finds candidates with `bands && %s` (the GIN
  index) among rows that are themselves fresh and non-empty, drops the same-thread ones, verifies
  the rest, and then:
  - upserts each verified pair (`ON CONFLICT … DO UPDATE SET similarity`). **The status is never
    written by a scan**, so a dismissed pair stays dismissed and a confirmed one stays confirmed
    (AC5);
  - deletes this document's **open** pairs that are not in the verified set (AC3). Dismissed and
    confirmed pairs are labels the owner set, and a scan never deletes them (ruling R3).
- **Deleting a document** deletes its pairs and its signature (`ON DELETE CASCADE`, AC4).
- `check_document(conn, doc_id)` refreshes one document if stale and returns its open and
  confirmed partners, `[{document_id, title, vault_path, sensitivity, similarity, status}]`.

## Surfaces

- `brain dedup scan [--all] [--limit N]` prints what it did (examined, short, pairs new, pairs
  dropped). `brain dedup list [--status open|dismissed|confirmed] [--limit N] [--json]` lists both
  titles and ids (the terminal is inside the F6 boundary, so it is permissive). `brain dedup
  dismiss A B` / `confirm A B` take id prefixes, in either order, and set the label; deleting
  stays `brain rm`. Every command refuses with a `brain init` hint when migration 030 is absent.
- MCP `brain_dedup_list(status="open", limit=50, include_confidential=False)`: read-only. Without
  `include_confidential`, a pair is returned only when **both** ends are not confidential, in the
  SQL predicate, as `connect.iter_suggestions` gates both endpoints.
- Brief: `Possible duplicates (N) — brain dedup list`, printed only when N > 0.
- Ingest tab: the job result's `possible_duplicates`, lensed (ruling R5). The front end is not
  changed (ruling R6).

## Rulings

- **R1 — "tokens" are words.** The ticket's 50-token floor is counted on the same words the shingles
  are built from. The leaf stays pure and tokenizer-free, and the floor means one thing everywhere.
- **R2 — a short document is stored with empty arrays.** Without a row it would be stale forever,
  and `--limit 500` would spend every nightly run on the same stubs. An empty `bands` array overlaps
  nothing, so it is never a candidate.
- **R3 — a scan deletes open pairs only.** A pair that no longer verifies after an edit is dropped
  if it is open (AC3). A dismissed or confirmed pair is kept, whatever later edits do, so a label
  the owner set is never lost and a dismissed pair can never come back as open (AC5).
- **R4 — delivery sends the count.** §2.5 rule 3 allows titles and counts by default, and the line
  is a count with no title. The brief's published payload (the page and every delivered message) is
  built with `exclude_confidential=True`, and under that lens `count_open` counts only pairs whose
  **both** ends are neither confidential **nor drafts**. A pair with a confidential end is removed
  in the predicate, so the number says nothing about how many confidential pairs exist, and the
  draft rule means `brief_delivery.without_drafts` (which cannot edit a number) has nothing to
  remove. The terminal payload counts every open pair.
- **R5 — the nested lens.** `routes_jobs.lensed_result` reads top-level keys only. Each entry of the
  new `possible_duplicates` list now has `document_id`, `title` and `vault_path` withheld (`None`,
  with `withheld: true`) unless the server serves confidential titles or the entry says it is
  `normal`. It fails closed: an entry with no `sensitivity`, or a value that is not a mapping, is
  withheld. When the job's own document is withheld, **every** entry is withheld too, because
  "this withheld document is a near-copy of X" would disclose its content through X. `similarity`
  and `status` stay. Any `possible_duplicates` value that is not a list becomes `[]`.
- **R6 — the Ingest tab front end is unchanged.** The ticket scopes the key (RM-10's
  always-present `possible_duplicates`), not its rendering. No browser test runs.
- **R7 — the UI check never fails the ingest.** `check_document` runs after `ingest_document` has
  committed, inside a savepoint; a `psycopg.Error` (or migration 030 being absent) is one
  class-only WARNING and an empty list.

## Tests

| File | What |
|---|---|
| `tests/test_near_dup.py` | Pure: words, shingles, signature determinism and range, band index salt, signed conversion, estimate vs exact Jaccard on synthetic pairs, verification cut, floor, same thread, ordering (AC1 pure half, AC2, AC6). |
| `tests/test_migration_030_near_duplicates.py` | DDL: CHECKs, cascade from both FKs, GIN index, clean re-apply (AC4). |
| `tests/test_near_dup_store.py` | Scan, stub skip, stale recompute, edit drops a pair (AC3), dismissed never reopened (AC5), confirmed kept, same thread, `--all`, limit, lens on both ends, gated count excludes drafts, `check_document` (AC1 DB half). |
| `tests/test_cli_dedup.py` | CLI scan / list / dismiss / confirm, JSON, prefix resolution, missing migration. |
| `tests/test_mcp_dedup.py` | Registration, both-end lens, status/limit validation, missing migration. |
| `tests/test_brief_near_dup.py` | The brief line, terminal vs gated payloads, page, delivery count (R4). |
| `tests/test_brief_fg_dedup_line.py` | The daemon script's line order and `\|\| true`, run with a fake interpreter whose `dedup` fails (AC7). |
| `tests/test_ui_ingest_near_dup.py` | The job fills `possible_duplicates`; the nested lens (R5); a failing check leaves the ingest done (R7). |

**AC8** (a full first scan over 2,000 synthetic documents in under 5 minutes) is measured, not
asserted in CI: a throwaway seeder (not committed) fills a separate database on the 5434 test
instance with 2,000 synthetic documents, and `brain dedup scan` runs over it through the real CLI.
The database is dropped afterwards.

**Measured 2026-10-30** (Apple M1 Pro, the 5434 test instance): 2,000 synthetic documents, a mean
of 2,048 words (200 to 4,000, plus 40 stubs of 10 to 45 words and 100 near-copies with 1% of their
words changed). The full first scan took **88.0 s** wall clock (73.9 s user): "Examined 2000
documents (43 under 50 words); 97 new pairs, 0 dropped." A second scan examined 0. The nightly
`--limit 500` is about a quarter of that.

## Mutation proofs (in a `git archive` copy only)

Band-index salt dropped; unsigned conversion; the Jaccard cut; the 50-token floor; the same-thread
exclusion; dismissed reopened by a scan; the lens on one end only; the nested `possible_duplicates`
lens; `|| true` dropped.

## Risks

- Long shared boilerplate (meeting transcripts) can pair falsely: the 0.85 bar, the floor and
  `dismiss` mitigate it (ticket).
- Pure-Python MinHash is O(words × 128) per document; AC8 measures it.

## Deviations

1. **`mcp_server.py` is touched**, though the ticket's scope does not list it: `brain_brief`'s
   docstring, which names every F6-gated key, now names the `possible_duplicates` count. Reworded
   in place, net-zero, with the ceiling row's hop recorded.
2. **The gated count also leaves out draft ends** (R4). The ticket says the brief "counts open
   pairs through the gated lens"; the gated lens here is confidential on both ends **and** draft
   on both ends, because the delivered count cannot have drafts removed after the fact
   (`brief_delivery.without_drafts` edits lists, not numbers). It can only make the number
   smaller.
3. **"Tokens" are words** (R1): the 50-token floor counts whitespace-separated words, the unit the
   shingles are built from, so the pure leaf needs no tokenizer.
4. **A short document is stored with empty `signature` and `bands` arrays** (R2). The ticket's
   column comments say 128 and 32 values; a stub's row carries none, so it is not re-examined by
   every capped scan and never matches a band.
5. **`brain_dedup_list` takes `status` and `limit`** besides the ticket's `include_confidential`,
   with the same defaults as `brain dedup list` (`open`, 50; `limit` at most 200). `brain dedup
   list` also takes `--limit`.
6. **The Ingest tab's check finds partners among documents already signed.** It signs the new
   document, not the corpus; a near-copy ingested since the last nightly scan is found by the next
   scan, not by the tab. The front end is not changed (R6): the key is filled and lensed.
