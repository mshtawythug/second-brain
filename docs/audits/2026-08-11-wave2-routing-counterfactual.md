# Wave 2 — routing counterfactual (measured 2026-08-11)

Provenance for the token figures published in
`skills/consult-brain/SKILL.md`, `docs/agent-skills.md` and
`skills/ingest-brain/SKILL.md`. Plan:
`docs/plans/2026-08-10-agentic-token-reduction.md`. Raw numbers:
[`2026-08-11-wave2-routing-counterfactual.json`](2026-08-11-wave2-routing-counterfactual.json).

**What this is.** A cost comparison of the two retrieval procedures
`consult-brain` documents, priced per question against the live corpus:

- **OLD** — `brain search "<q>" --limit 20 --json`, then `brain show <id> --json`
  for each of the 20 results.
- **NEW** — `brain search "<q>" --limit 20 --json --brief`, then one
  `brain recall "<q>" --budget 4000 --json`.

**What this is not.** A production saving. Nothing in this repo measures
whether an agent actually follows a skill. Both columns are the cost of a
*documented procedure* — the claim is about what the documentation instructs,
and that is the whole of it.

**No PII.** The five questions are generic topical strings, chosen the same way
`scripts/token_payload_queries.txt` chooses its seeds. Only counts, the
retrieval config, and those strings are recorded — no titles, no bodies, no
participants, no identifiers.

## Result

| Question | OLD (search + 20×show) | NEW (brief + recall) | Δ |
|---|---:|---:|---:|
| pricing conversations | 214,265 (10,946 + 203,319) | 8,799 (3,772 + 5,027) | −95.9% |
| hiring and team growth | 150,293 (10,125 + 140,168) | 9,123 (3,936 + 5,187) | −93.9% |
| platform migration planning | 257,989 (10,142 + 247,847) | 8,886 (3,692 + 5,194) | −96.6% |
| customer onboarding process | 146,080 (9,486 + 136,594) | 9,152 (3,903 + 5,249) | −93.7% |
| engineering roadmap priorities | 151,075 (9,432 + 141,643) | 9,033 (3,911 + 5,122) | −94.0% |
| **total** | **919,702** | **44,993** | **−95.1%** |
| **mean per question** | **183,940** | **8,998** | — |

Means are reported truncated to whole tokens (183,940.4 and 8,998.6); the
totals they derive from are exact.

Every question returned the full 20 results, so the OLD column is 20 document
bodies each time — no arm was short-changed by a thin match set.

**Why the arithmetic estimate is low.** 20 × the 18,218-char corpus mean ÷ 4
gives ~91,000 tokens; measured, the same procedure costs a mean of 183,940.
Two reasons, in order of size: documents that rank top-20 are systematically
longer than the corpus mean (long transcripts win hybrid retrieval), and
`brain show --json` carries more than `content`. `consult-brain` states both
numbers and says which is which, rather than quietly replacing one with the
other.

## Corpus statistics behind the other published figures

| Figure | Published as | Measured |
|---|---|---|
| Mean document body | "a mean of 18,218 chars" | 18,218.4 chars over n=1,393 |
| Largest document | "~67k tokens on the largest doc" | 67,410 tokens (266,888 chars) |
| Krisp transcript mean | "~7,900 tokens" | 7,870.3 over n=261 |
| Krisp median / p90 / max | "7,400 / 15,300 / 33,600" | 7,414 / 15,278 / 33,570 |

`tests/test_skills_token_routing.py` previously carried **18,231** for the mean
body, inherited from the plan. It is 18,218 — the test now says so too.

## `--budget` bounds the block, not the `--json` payload

`--budget N` bounds `RecallResult.context_block()`. Under `--json`,
`cli_recall.py` emits `to_dict()` and never the block, so the payload carries
the same passage text *plus* per-passage metadata. Measured over the same five
questions:

| `--budget` | block | `--json` compact (Δ vs block) | `--json` as the CLI prints it (Δ vs block) |
|---:|---:|---:|---:|
| 2,000 | 1,811 | 2,395 (+32%) | 2,500 (+38%) |
| 4,000 | 3,752 | 4,951 (+32%) | 5,156 (+37%) |

**The percentages in that table are relative to the block, not to the budget** —
they price what `--json` adds on top of the same passage text (2,500 ÷ 1,811 =
1.38; 5,156 ÷ 3,752 = 1.37). Against the *requested budget*, which is the number
a caller actually chooses, the printed payload is smaller: 2,500 ÷ 2,000 =
**1.25×** and 5,156 ÷ 4,000 = **1.29×**, because the block itself lands under
budget (1,811 of 2,000; 3,752 of 4,000). The recall arm of the counterfactual
above corroborates the second row independently — mean `new_recall_tokens`
5,155.8 at `--budget 4000` = 1.289×.

So "exactly the budget you asked for" was false for the `--json` form the cost
table prescribes. The table now says the passages honour the budget and the
`--json` envelope measures ~1.25–1.3× it — budget-relative, the same denominator
the reader is choosing.

## Reproduction

Read-only against the live corpus. Both harnesses put the connection into a
server-enforced read-only transaction and deliberately skip the CLI's
telemetry writes (`record_search_query`), which are not part of the payload an
agent pays for. Every payload is built by the constructors the CLI uses and
rendered through `brain.format.emit_json`, so the counted bytes are the CLI's
own stdout — verified byte-identical against `brain show <id> --json` on both a
232-byte and a 20,956-byte document before the run.

```bash
cd <repo>
export BRAIN_TOKEN_REPORT_ALLOW_PROD=1     # opt-in for the live corpus (port 55432)
.venv/bin/python wave2_counterfactual.py > docs/audits/2026-08-11-wave2-routing-counterfactual.json
.venv/bin/python wave2_corpus_stats.py     # the corpus block above
```

`wave2_counterfactual.py`:

```python
import contextlib, io, json, os, sys
from brain.config import Config
from brain.db import connect
from brain.embeddings import make_embedder
from brain.format import emit_json
from brain.format_search import search_results_brief_json, search_results_json
from brain.queries import fetch_document
from brain.recall import recall
from brain.search import hybrid_search

QUERIES = ["pricing conversations", "hiring and team growth",
           "platform migration planning", "customer onboarding process",
           "engineering roadmap priorities"]
LIMIT, BUDGET = 20, 4000
assert os.environ.get("BRAIN_TOKEN_REPORT_ALLOW_PROD")

def rendered(payload):                      # the exact stdout the CLI writes
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        emit_json(payload)
    return buf.getvalue()

def show_payload(doc):                      # brain show --json (cli.py)
    payload = {"id": doc.id, "title": doc.title, "content": doc.content,
               "content_type": doc.content_type, "tags": doc.tags,
               "source_path": doc.source_path, "ingested_at": doc.ingested_at,
               "source_kind": doc.source_kind}
    if doc.summary is not None:
        payload["summary"] = doc.summary
    return payload

cfg = Config.load()
embedder = make_embedder(cfg)
count = embedder.count_tokens
rows = []
with connect(cfg.database_url) as conn:
    conn.rollback(); conn.read_only = True          # server-enforced read-only
    for query in QUERIES:
        results = hybrid_search(                    # ONE retrieval per question
            conn, embedder=embedder, query=query, limit=LIMIT,
            vector_sim_floor=cfg.vector_sim_floor,
            recency_halflife_days=cfg.recency_halflife_days,
            snippet_context_tokens=cfg.snippet_context_tokens,
            sensitivity=None)                       # the CLI's lens: both tiers
        search_tokens = count(rendered(search_results_json(results)))
        brief_tokens = count(rendered(search_results_brief_json(results, cost=count)))
        show_tokens = sum(
            count(rendered(show_payload(fetch_document(conn, r.document_id))))
            for r in results)
        rec = recall(conn, cfg, embedder=embedder, query=query,
                     budget_tokens=BUDGET,
                     max_candidates=cfg.recall_max_candidates, sensitivity=None)
        rows.append({"query": query, "results": len(results),
                     "old_search_tokens": search_tokens,
                     "old_show_tokens": show_tokens,
                     "old_total_tokens": search_tokens + show_tokens,
                     "new_brief_tokens": brief_tokens,
                     "new_recall_tokens": count(rendered(rec.to_dict())),
                     "new_total_tokens": brief_tokens + count(rendered(rec.to_dict()))})
old = sum(r["old_total_tokens"] for r in rows)
new = sum(r["new_total_tokens"] for r in rows)
print(json.dumps({"limit": LIMIT, "budget_tokens": BUDGET, "queries": len(QUERIES),
                  "measurements": rows,
                  "totals": {"old_tokens": old, "new_tokens": new,
                             "old_mean_tokens": old / len(rows),
                             "new_mean_tokens": new / len(rows),
                             "delta_pct": (new - old) / old * 100.0}},
                 ensure_ascii=False, indent=2, sort_keys=True))
```

`wave2_corpus_stats.py` is the same preamble followed by three read-only
queries — `count(*)`, `avg(length(content))` and `max(length(content))` over
`documents`; `count_tokens` over the single longest body; and `count_tokens`
over every `content_type = 'transcript'` body, sorted for the median and p90.

**Drift.** Numbers move with the corpus: an ingest changes the match set and
therefore every column. Re-running on a later corpus should reproduce the
*magnitude* (two orders of magnitude between the arms), not the digits. The
digits above are what this corpus returned on 2026-08-11 under the `config`
block recorded in the JSON.
