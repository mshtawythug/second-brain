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
   `RRULE` lines, or `Recurring series` if the master cannot be read); `Location:`; `Visibility: private` only when private; `Attendees:` with display names only
   (attendees with no display name, and resource rooms, are left out); then the description.
   - The `Visibility` line exists so that an event that turns private changes the body. The
     re-ingest then goes through the pipeline's escalate-only update path, which also applies the
     F6 hosted-egress veto, instead of a hand-written sensitivity update.
2. **One cleaner for every free-text field** (`ingest/calendar_text.py`, from the PR #96 security
   review): the title, the location, the description and each display name. Per line it applies
   NFKC, removes every URL (scheme, `www.` and scheme-less `host.tld/path` forms), drops the line
   if what is left looks like dial-in material (a PIN / passcode / code / meeting-ID /
   conference-ID / bridge / Passwort keyword, a phone-like run of 7+ digits, or a one-tap
   `,,digits#` form), and removes every email address, including obfuscated `[at]` / `(at)` /
   `at … dot` forms and `user@host` with no dot. In the description, HTML is converted to text
   first and Google's invitation block (between `-::~` delimiter lines) is dropped whole.
   `conferenceData` and `hangoutLink` are never read. A title the cleaner empties is stored as
   `(calendar event)` rather than skipped: the ticket skips events with *no* title, and such an
   event still has a time and attendees. A display name the cleaner empties is dropped.
   - **Re-check rounds (sec-96 S2–S5, rev-96 V1–V3).** Normalisation also removes Unicode format
     characters and decodes `%40` and HTML entities; dash variants (U+2010–U+2015) count as phone
     separators; `pwd` / `pw` (with a value) and `Kennung` are keywords; addresses may have spaces
     around `@` and the dots; `tel:` / `sip:` links are removed with the other links. The date
     mask covers `M/D/YYYY`, `D.M.YYYY`, `YYYY.MM.DD`, `YYYY/MM/DD`, two-digit years and year
     ranges (`2026-2028`), but masks a candidate only when it is a WHOLE number group (nothing
     numeric glued on either side, one separator throughout) and a valid date or time, with a
     four-digit year in 1900–2099. The first widening (V1) masked date-shaped slices of European
     numbers ("01.70.37.22.46", "+49 30 12-34-5678"), which then survived every field; the
     bounds and the validation close that (rev-96, HIGH), and a test table pins both directions.
   - **The directory is updated once per document per run** (aud-96 D1): attendees are recorded
     before the write when the document is new or its participants changed, and after it only
     when the body changed and they were not recorded already. A first ingest had counted every
     attendee twice, which skews `resolve_name_to_email` (it picks by summed count).
   - **Titles keep the words before the secret** (`clean_title`, sec-96 T1–T4). A title's lines
     are joined into one. A credential keyword (passcode, password, Passwort, Kennwort, Kennung,
     access code, meeting ID, conference ID, `pwd`, `pw`) truncates the title from the keyword to
     the end, whatever follows ("Weekly sync, passcode is 7Hk2pQ" keeps "Weekly sync"), unless it
     has no value: it ends the title, or only "rollout", "manager", "reset", "policy" or "card"
     follow ("Password manager rollout" and "Access code rollout" stay). A short word with a value
     (`PIN 4321`, `PIN-4321`, `PIN4321`, `P.I.N. 4321`, `code is 4321`, `code — 4321`,
     `ID: ENG-123`) truncates the same way ("Project ID: ENG-123 kickoff" keeps "Project"). A
     dial-in keyword loses its value, and digit runs and one-tap forms are removed in place.
     `skype:` links are removed with the others. Descriptions, locations and display names keep
     whole-line dropping; a location's lines are joined first too, and in a description a line
     that ends with a credential also takes the next line when it is a bare value (at most three
     tokens: "Passcode:" then "424242").
   - **Accepted residual risk.** A bare value with no keyword and under seven digits ("Sync
     123456") and digits spelled out as words ("PIN four three two one") are not recognised.
   - **The documented trade, privacy over recall (S6).** Outside a title, "Discuss the access code
     rollout", "password reset policy discussion" and "Meeting ID card" are dropped as dial-in
     lines, and "Year 2026 2027 2028 planning" as a digit run. In every field, "node.js/react" and
     "README.md/notes" go as scheme-less links (V3, a stricter host rule, was not done: requiring
     three labels would let `zoom.us/j/…` and `whereby.com/…` through, and a TLD allowlist would
     miss unlisted conference hosts), and "Team at Acme dot com launch" goes as a spelled-out
     address. A test pins the over-removal as intended.
3. **Series.** Instances come from `singleEvents=true`; the series document reads title,
   description, location, attendees and visibility from the **master** (`events get`), so the
   body does not depend on the run's window. If the master cannot be read, a NEW series falls
   back to its earliest instance in the window; a series already stored keeps its stored body and
   gets only its metadata refreshed, so one failed `events get` does not re-chunk and re-embed it
   (rev-96 R3). `CalendarBatch.fallbacks` counts these, and the CLI prints the count. A
   fallback series that turned **private** still goes through `ingest_document`, because its
   escalate-only sensitivity update (and the hosted-egress veto) matters more than the churn
   (sec-96 S1). Instances the owner declined are dropped from the
   occurrences. A series with no remaining instances, or whose master the owner declined, is
   skipped.
4. **`metadata.occurrences`** = `{"run_window_count": <instances in the most recent run's
   window>, "last": [the latest ten start times known, ISO, ascending]}`. `sent_at` comes from
   `metadata.date`: the event's start time for a single event, and for a series the latest known
   occurrence. `calendar.merge_volatile` merges each run into what is stored: `last` is the union
   of the stored and incoming times trimmed to the latest ten, and `date` is the latest of them,
   so a backfill of an earlier window never moves a series backwards (rev-96 R1). A stored start
   inside the run's window that the run did not return (cancelled or declined since) is dropped,
   so a cancelled future occurrence stops dating the series (rev-96 R6). A single event is not
   merged: a rescheduled event's start may legitimately move earlier. On a
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
   `people._doc_participant_keys` gains a `calendar` branch reading those addresses, so the
   graph's people aspect resolves a calendar event's attendees (rev-96 R2; it was first recorded
   as backlog BL-29 and then done here). The attendees are recorded in the directory before the
   write, and a metadata-only refresh passes `graph_syncer`, so a participants-only change (an
   address-only attendee) re-indexes the people too (rev-96 R5). `review.weekly._key_people`
   tallies through the same function, so the weekly review's "key people" (written to
   `<vault>/reviews/<week>.md`) now count calendar attendees' addresses, as they already count
   Gmail's. Kept by ruling (aud-96): meetings are key-people signal, and confidential documents are
   gated upstream. `tests/test_review_weekly.py` pins it. `people.py` is over the ceiling: the growth is recorded
   inline at the branch, in its module docstring and in its CLAUDE.md row.
7. **Scope** (exact message from the ticket). `--since` and `--until` are `YYYY-MM-DD`, `--since`
   is inclusive and `--until` exclusive, the same as `ingest-gmail`'s `before:`. The window must
   be ≤ 92 days, and `--until` ≤ today + 14 days (UTC). Every refusal exits 2.
8. **Options.** `--calendar` (default `primary`), `--dry-run`, `--tag`, `--enrich` (opt-in summary;
   the default is none), `--allow-secrets`. Sensitivity: `confidential` for `visibility` `private`
   or Google's legacy `confidential`, otherwise `normal`.
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
