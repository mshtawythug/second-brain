# RM-30 — Calendar events as documents: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: "#### RM-30 — Calendar events as documents" in
`docs/specs/2026-10-23-roadmap-completion-design.md`, with §2.4 (the new-source protocol), §2.2 (the
ceiling budgets) and the §4.2 shared-file table. Decisions: RM-30.D1 (one document per recurring
series) **yes**; RM-30.D2 (a daily daemon run) **no**.

## Files

| File | Change |
|---|---|
| `src/brain/graph_rag/extract_policy.py` | **New leaf.** `CONCEPT_SKIP_CONTENT_TYPES = frozenset({"calendar_event"})`, `CONCEPT_SKIP_POLICY_VERSION`, and `skip_watermark(content_type) -> tuple[str, str] \| None`. It reads the two module constants at call time. |
| `src/brain/graph_rag/reconcile_plan.py` | When the concept aspect is active and `skip_watermark` returns a pair, that pair replaces `(c_inputs_hash, extractor.version)` in the expected watermark and in the plan. The body is not fetched (`content=""`). `ReconcilePlan` gains `concept_skipped: bool`. |
| `src/brain/graph_rag/reconcile.py` | The extraction guard becomes `if plan.concept_stale and not plan.concept_skipped:`, with an inline reason. Every other line of the concept path runs as for any stale aspect. Budget +15 (§2.2). The file stays under 800. |
| `src/brain/source_kinds.py` | Adds `"calendar"` to `VALID_SOURCE_KINDS`. |
| `src/brain/ingest/calendar.py` | **New leaf**, pure apart from the injected runner. It covers the `gws calendar events list` call (the same call shape as `directory.refresh_calendar`, with `--calendar` as `calendarId`) and `gws calendar events get` for a series master. It applies the skip rules, groups instances by `recurringEventId`, cleans text, and builds one `ExtractedDoc` per single event or series. |
| `src/brain/cli_ingest_calendar.py` | **New command module**, `brain ingest-calendar`, registered through `cli_registry.EXTENSION_REGISTRARS`. It handles scope validation, `--dry-run`, the ingest loop, the metadata-only re-run and the directory pairs. |
| `src/brain/cli_registry.py` | Adds one import and one `EXTENSION_REGISTRARS` entry. |
| `src/brain/ui/static/js/{results,discovery}.js` | Adds `calendar: "📅"` to both `SOURCE_ICONS` maps. |
| `src/brain/ui/static/js/tree.js` | Adds `_ingested/calendar` to `MONTH_GROUPED_PATHS`. |
| Docs | `CHANGELOG.md` [Unreleased]; `docs/cli-reference.md`; Appendix B RM-30 → DONE; the `ingest-brain` skill's command list. |

No over-800 file is touched. `cli.py` is not touched (RM-06 seam). `people.py` is not touched, as
explained below.

## Design points the ticket leaves to the implementer

1. **Document body** (the deduped, chunked, embedded text). It holds the title; a `When:` line
   (single event: start–end in the event's own offset, or `(all day)`; series: the master's
   `RRULE` lines, or `Recurring series` if the master cannot be read); `Location:` with URLs
   removed; `Visibility: private` only when private; `Attendees:` with display names only
   (attendees with no display name, and resource rooms, are left out); then the description.
   - The `Visibility` line exists so that an event that turns private changes the body. The
     re-ingest then goes through the pipeline's escalate-only update path, which also applies the
     F6 hosted-egress veto, instead of a hand-written sensitivity update.
2. **Description cleaning.** HTML is converted to text. Google's inserted invitation block
   (between `-::~:~::~` delimiter lines) is dropped. URLs on conference hosts (Zoom, Meet, Teams,
   Webex, GoToMeeting, Chime, BlueJeans) are removed, and so are dial-in, PIN and passcode lines.
   `conferenceData` and `hangoutLink` are never read.
3. **Series.** Instances come from `singleEvents=true`; the series document reads title,
   description, location, attendees and visibility from the **master** (`events get`), so the
   body does not depend on the run's window. If the master cannot be read, it falls back to the
   series' earliest instance in the window. Instances the owner declined are dropped from the
   occurrences. A series with no remaining instances, or whose master the owner declined, is
   skipped.
4. **`metadata.occurrences`** = `{"count": <instances in this run's window>, "last": [the last ten
   start times, ISO, ascending]}`. `sent_at` comes from `metadata.date`: the event's start time for
   a single event, and for a series the start of the latest occurrence in the window. On a
   re-run, `update_document(metadata_patch=…)` re-promotes `sent_at` with no re-chunk.
5. **Re-run flow.** `ingest_document` decides. If the content hash is unchanged it returns
   `created=False, body_changed=False`. When the stored `occurrences` or `date` differ, the
   command then calls `update_document(document_id=…, metadata_patch={"occurrences": …,
   "date": …})` with no embedder, so there is no re-chunk and no embed call. A changed body
   follows the normal in-place update path.
6. **`--person` resolution.** The resolver reads `directory_entries`, so on create or body change
   the command upserts each attendee's `(display name, email)` with `source="calendar"`, through
   `DirectoryStore.upsert_pair`. That is the same mining `refresh_calendar` does. Addresses are
   stored lowercased in `metadata.participants`, which the search predicate matches.
   `_doc_participant_keys` (`people.py`, over the ceiling) is **not** extended, so the graph's
   people aspect for a calendar document resolves no persons. Criterion 6 asks only that the
   aspect be indexed (its watermark written). Adding calendar persons to the graph is a separate
   change to an over-ceiling file and is recorded as a follow-up.
7. **Scope** (exact message from the ticket). `--since` and `--until` are `YYYY-MM-DD`, `--since`
   is inclusive and `--until` exclusive, the same as `ingest-gmail`'s `before:`. The window must
   be ≤ 92 days, and `--until` ≤ today + 14 days (UTC). Every refusal exits 2.
8. **Options.** `--calendar` (default `primary`), `--dry-run`, `--tag`, `--enrich` (opt-in summary;
   the default is none), `--allow-secrets`. Sensitivity: `confidential` for `visibility ==
   "private"`, otherwise `normal`.
9. **Skipped.** Events the owner declined (an attendee with `self: true`, `responseStatus ==
   "declined"`), events with no title, `eventType` `workingLocation` or `outOfOffice`, and
   `status == "cancelled"`.

10. **Demo corpus: `calendar` is exempted, not seeded** (option (b), decided with the coordinator
    because RM-34 makes the same change in parallel). `tests/test_demo_corpus.py` requires the
    demo corpus to cover every source kind. It gains RM-34's shape: a named `DEMO_EXEMPT_KINDS`
    with a written reason per kind, and an assert that the exemptions are real kinds. A calendar
    event is a short sourced document of a shape the corpus already shows, and the calendar
    path (the `gws` read, series, occurrences) cannot run from a manifest seed. Seeding one
    would bump the 22-doc count pinned in the demo tests, `cli_demo.py`, `docs/cli-reference.md`
    and the recorded `docs/assets/demo.tape`, and would collide with RM-34 if both seeded. The
    cost: the demo does not show `_ingested/calendar`'s month grouping. `ADDED_KINDS` in
    `tests/test_source_kinds_identity.py` uses the same name and text as RM-34, so the merge is
    a set union.

## Tests (TDD; synthetic data only, `example.com`, made-up titles)

- `tests/test_extract_policy.py` — unit tests of the helper and the constants.
- `tests/test_ingest_calendar.py` — unit tests, from fixture constants to `ExtractedDoc`s:
  single, recurring with exceptions (a moved instance and a declined instance), declined,
  private, all-day, future, no title, working-location and out-of-office, cleaning (no URL, PIN
  or address in the body), and the call shapes.
- `tests/test_cli_ingest_calendar.py` — CLI tests: scope refusals (missing, > 92 days, > 14 days
  ahead, bad format), dry-run. DB end-to-end tests through the CLI with an injected runner: each
  fixture class lands as specified (criterion 1); a re-run is a no-op; a changed description
  updates in place; new occurrences change only `metadata.occurrences`, with no embed call and no
  re-chunk (criterion 2); `brain search --source calendar --person <synthetic>` (criterion 3); no
  attendee address in any body (criterion 5).
- `tests/test_graphrag_concept_skip_policy.py` — AGE tests (5434), criteria 6 and 7, through
  `build_graph` with a fake extractor that fails the test if called. They also assert that the plan
  does not fetch the body. Criterion 7 bumps the policy with `monkeypatch.setattr` on
  `extract_policy` (a standard test double, rule 13).
- `tests/test_source_kinds_identity.py` — the pinned set gains `calendar`.

## Risks

- A long daily series: bounded by the occurrence summary (count and last ten).
- `gws` output shape: parsed by the same `_parse_gws_pages` as `refresh_calendar`. It is private
  to `directory.py`, so it is imported with a comment, following the `gmail.py` /
  `_strip_re_fwd_prefixes` precedent.
