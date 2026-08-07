# w5 handoff — QA-5 / QA-6 (task #29): the two `cli.py` patches

My side is landed and green: `src/brain/review/queries.py` +
`tests/test_review_diagnostics.py` (**14 passed**), `ruff check src/ tests/`
exit 0, `mypy src/` clean on 208 files.

Both call sites are in `src/brain/cli.py`, which is coordinator-only. Here they
are exactly.

---

## Patch 1 — QA-5: say *why* the staleness scan found nothing

**Where:** `src/brain/cli.py`, the `run_stale` block (currently ~4298–4311).

**Replace this:**

```python
            if run_stale:
                findings.extend(
                    run_staleness_scan(
                        conn, embedder, cfg, tenant_id=tenant, dry_run=dry_run
                    )
                )
                if dry_run:
                    _warn_skipped_no_summary(
                        count_stale_docs_missing_summary(
                            conn,
                            tenant_id=tenant,
                            stale_age_days=cfg.review_stale_age_days,
                        )
                    )
```

**With this:**

```python
            if run_stale:
                stale_findings = run_staleness_scan(
                    conn, embedder, cfg, tenant_id=tenant, dry_run=dry_run
                )
                findings.extend(stale_findings)
                # A silent scan is indistinguishable from a broken one. When the
                # staleness leg produced nothing, say which precondition was the
                # binding one — in EVERY mode, not just --dry-run.
                if not stale_findings:
                    diagnosis = diagnose_stale_candidates(
                        conn,
                        tenant_id=tenant,
                        stale_age_days=cfg.review_stale_age_days,
                    )
                    if diagnosis.hint:
                        typer.secho(f"staleness scan: {diagnosis.hint}",
                                    fg="yellow", err=True)
```

**Import to add** alongside the other `brain.review.queries` imports (~line 4271):

```python
from .review.queries import diagnose_stale_candidates
```

### Why not the obvious one-liner

The tempting fix is to move the existing `_warn_skipped_no_summary` call out of
the `if dry_run:` branch. **That does not work, and I nearly filed it.**
`count_stale_docs_missing_summary` is itself scoped through
`graph_entity_mentions`, so on a corpus whose graph was never built it counts
zero and warns about nothing — in either mode. Verified: `--dry-run` and the
real run produced byte-identical output on a corpus with 3 aged documents and 0
entity mentions.

`diagnose_stale_candidates` counts each stage **independently**, so the empty
stage is identifiable:

```
aged=3  in_graph=0  summarized=0
reason='no_graph_entities'
hint  = no aged documents are in the entity graph, so none can be matched
        against a superseding note. Run `brain graphrag build`.
```

`hint` is `None` when candidates genuinely exist — so a healthy scan stays
quiet and `No findings.` keeps meaning good news.

**Keep `_warn_skipped_no_summary` / `count_stale_docs_missing_summary` as they
are.** They still serve the conflict-scan branch; this is additive.

---

## Patch 2 — QA-6: `--include-snoozed` on `brain review list`

**Where:** `src/brain/cli.py:4396`, `review_list`.

**Add the option:**

```python
    include_snoozed: bool = typer.Option(
        False,
        "--include-snoozed",
        help="Also show findings that are still snoozed.",
    ),
```

**And forward it** at the `list_review_queue(...)` call in that function:

```python
        rows = list_review_queue(
            conn,
            tenant_id=tenant,
            signal_kinds=kinds,
            limit=limit,
            include_snoozed=include_snoozed,   # <-- add
        )
```

The keyword defaults to `False`, so the existing call site keeps working
unchanged if you'd rather land the flag separately.

### Verified against a live fixture

```
default           : 0 row(s)
--include-snoozed : 1 row(s)   27800d1f  status=snoozed  score=0.97
```

The first check I wrote returned `1` and `1` and would have "passed" while
proving nothing — the only row present was `surfaced`, not snoozed. Creating an
actually-snoozed row is what made it a test.

### The deliberate omission

**No un-snooze verb**, per your ruling. With the flag the row is visible and
`brain review resolve <id>` is an adequate escape hatch; snoozes also self-heal
on expiry (pinned by `test_expired_snooze_reappears_without_the_flag`). The
reasoning is recorded in `list_review_queue`'s docstring so the absence reads as
a decision rather than an oversight.

---

## Tests added — `tests/test_review_diagnostics.py`, 14 passing

Every absence assertion is paired with a control:

| Test | Control it carries |
|---|---|
| `test_no_graph_entities_is_named` | asserts `aged == 1` first — the doc *is* aged |
| `test_no_summaries_is_named` | asserts `aged == 1 and in_graph == 1` first |
| `test_healthy_corpus_reports_no_reason` | the load-bearing case: no false alarm on a healthy scan |
| `test_snoozed_finding_is_hidden_by_default` | paired with `test_include_snoozed_reveals_it` |
| `test_surfaced_findings_appear_in_both_modes` | the flag widens, never replaces |
| `test_stages_narrow_...` | counts are nested subsets, so "first zero" is a valid reading |
| `test_reason_precedence` | pure, parametrized over all four stage combinations |

## Doc delta for whoever owns `cli-reference.md`

- `brain review list` gains `--include-snoozed`; note that no un-snooze verb
  exists **by design** — use `--include-snoozed` then `resolve`, or wait for
  expiry.
- `brain review scan` now explains an empty result rather than printing a bare
  `No findings.`; the three causes map to `brain graphrag build`,
  `brain enrich --backfill`, or nothing-to-do.
