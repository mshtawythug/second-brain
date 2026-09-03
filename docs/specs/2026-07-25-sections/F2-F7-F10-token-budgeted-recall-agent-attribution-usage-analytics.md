# F2-F7-F10 — Token-budgeted recall, agent attribution, usage analytics

> Design section of `docs/specs/2026-07-25-agent-memory-safety-ui-design.md`.
> Global constraints (PII, production safety, quality gates, style) are inherited from
> section 4 of that document and are not restated here.

## Agent memory core — token-budgeted recall, agent attribution, and usage analytics

> Owns migration **`024_agent_attribution.sql`** and nothing else. Does **not** touch `025_document_sensitivity.sql`.

---

### 1. Goal

An AI agent talking to this brain today has to ask for *five documents* and hope they fit. `brain recall "<q>" --budget 2000` inverts that: it asks for **as much relevant, cited context as fits in N tokens**, over-fetching with the existing hybrid search and greedily packing ranked passages until the budget is spent, emitting a paste-ready citation-bearing block. Alongside it, a nullable `agent_id` on the three event-recording tables answers *which* agent did something (the existing `source` enum only says *which surface*), and `brain usage` turns the already-populated `search_queries` / `interactions` tables into a straight answer to "is this brain actually being used, by whom, and is it answering?" — the feedback loop that tells the owner whether ingestion is paying off.

---

### 2. Current state

**Retrieval is count-based, never budget-based.**
- `hybrid_search()` (`src/brain/search.py:231`) takes `limit: int = 5` and returns `list[SearchResult]` (`src/brain/search.py:88`) with `snippet: str`. The snippet is hard-truncated to `SNIPPET_LENGTH = 400` chars (`src/brain/search.py:104`, applied at `:555`) or, when `snippet_context_tokens > 0`, stitched from neighbours and capped at `4 * SNIPPET_LENGTH` = 1600 chars (`src/brain/search.py:556-558`).
- `SearchResult` carries **no date field** — `coalesce(sent_at, ingested_at)` is fetched at `src/brain/search.py:500-509` but only used for the recency boost, never returned. A citation line needs it, so recall must re-fetch it.
- The CLI `search` command (`src/brain/cli.py:4102-4234`) emits a Rich table or a JSON list. Nothing anywhere accepts a token budget.

**A greedy token budgeter already exists — three times, in three different shapes.** I read all three:
1. `brain.timeline._budget_doc_summaries` (`src/brain/timeline.py:588-613`) — **linear greedy prefix packer**: iterate rendered strings, `cost = count_tokens(text)`, `if out and used + cost > max_tokens: break`, first item always included. This is exactly the shape recall needs.
2. `brain.audio.build_prompt` (`src/brain/audio.py:290-328`) — **cumulative re-render** budgeter: re-counts the *entire* candidate prompt each step because the header/instruction framing must be inside the budget. Different contract.
3. `brain.search._expand_snippet_with_neighbors` (`src/brain/search.py:626-704`) — **bidirectional window walk** outward from the matched chunk. Different contract.

`ask.py` is **not** a budgeter — I checked: it truncates by *characters* (`_truncate`, `src/brain/ask.py:455-459`) at `_REFLECT_SNIPPET_CHARS = 120` / `_SYNTH_SNIPPET_CHARS = 400` (`:57`, `:59`). The task brief's premise that `ask.py` holds the budgeting machinery is **incorrect** — stating this explicitly rather than inventing a function. The real machinery is `timeline.py:588`.

**Decision:** extract shape (1) into a new shared `src/brain/token_budget.py`; `recall` consumes it and `timeline._budget_doc_summaries` is rewritten to delegate to it (same semantics, its existing tests stay green). Shapes (2) and (3) keep their own loops — a cumulative-prompt budgeter and a bidirectional walk are genuinely different algorithms, not copies. **No second linear budgeter is written.**

**Attribution: nothing exists.** `grep -rn "agent_id" src/brain/` returns zero hits.
- `documents` (`src/brain/migrations/001_init.sql:13-26`, extended by `003`/`007`/`011`/`021`) has no actor column. Its INSERT (`src/brain/ingest/__init__.py:800-819`) already has an extension seam: `_PROMOTED_COLUMNS` (`src/brain/ingest/__init__.py:171-178`) + `_promote_metadata_to_columns` (whose first loop is a plain `str` passthrough over `("thread_id", "rfc_message_id", "in_reply_to")`).
- `interactions` (`010_interactions.sql`, generalized by `015`) has `source TEXT CHECK (source IN ('cli','mcp','wiki'))` — surface, not actor. Writer: `record_interaction` (`src/brain/interactions.py:90-205`).
- `search_queries` (`019_search_queries.sql`, `+fts_count` in `023`) has the same `source` CHECK. Writer: `record_search_query` (`src/brain/gaps.py:100-195`).

**Usage analytics: raw events exist, no read surface.** `brain gaps` reads only *failures* (`src/brain/gaps.py:351-390`); `brain brief` / `brain review weekly` read activity via `brain.activity` (`iter_activity_docs` `:95`, `iter_ingested_docs` `:136`, `recent_captures` `:172`). No command reports volume, sessions, surfaces, or zero-result rate. `src/brain/activity.py` is **207 lines**.

---

### 3. User-visible surface

#### 3.1 `brain recall`

```
brain recall "<query>" [--budget N] [--json] [--agent ID] [<all existing search filters>]
```

| Flag | Type | Default | Help text |
|---|---|---|---|
| `query` | `str` (positional, required) | — | — |
| `--budget`, `-b` | `int` (`min=1`) | `cfg.recall_budget_tokens` (env `BRAIN_RECALL_BUDGET_TOKENS`, ships **2000**) | `Token budget for the assembled context block (cl100k_base).` |
| `--json` | `bool` | `False` | `Emit machine-readable JSON instead of the text block.` |
| `--agent` | `str \| None` | `None` (falls back to `BRAIN_AGENT_ID`) | `Attribute this recall to a named agent (recorded in search_queries.agent_id).` |
| `--max-candidates` | `int` (`min=1`) | `cfg.recall_max_candidates` (env, ships **25**) | `Upper bound on documents over-fetched before packing.` |
| `--source` | `str \| None` | `None` | *(identical to `brain search`, `src/brain/cli.py:4105`)* |
| `--tag` / `--has-tag` / `--without-tag` | `str \| None` | `None` | *(identical, `:4106`, `:4147`, `:4150`)* |
| `--since` | `str \| None` | `None` | *(identical, `:4107`)* |
| `--fts-only` | `bool` | `False` | *(identical, `:4116`)* |
| `--person` | `str \| None` | `None` | *(identical, `:4118`)* |
| `--after` / `--before` | `datetime \| None` | `None` | *(identical `formats=["%Y-%m-%d","%Y-%m-%dT%H:%M:%S"]`, `:4123`, `:4129`)* |
| `--kind` | `str \| None` | `None` | *(identical, `:4134`)* |
| `--thread` | `str \| None` | `None` | *(identical, `:4139`)* |
| `--draft/--no-draft` | `bool \| None` | `None` | *(identical, `:4142`)* |

Literal human output (**plain `typer.echo`, never `console.print`** — Rich would try to parse `[1]` as a style tag and raise `MissingStyle`):

```
# recall: "arctic embedder rollout" — 3 passages, 1712/2000 tokens (9 candidates, 6 dropped)

[1] 9f2c1a4e | 2026-05-02 | manual | Arctic embedder rollout notes
Snowflake Arctic Embed v2 is now the default embedding backend. The switch is
destructive: embeddings cannot be re-projected across models, so a backend
change means a full wipe and re-ingest.

[2] 3b71e0d9 | 2026-04-28 | krisp | Platform sync — embeddings backlog
Jordan Vale walked through the remaining backlog. The 4096-dim backend skips
the HNSW index because pgvector caps at 2000 dims for `vector`.

[3] c0d51a72 | 2026-04-11 | gmail | Re: embedding cost review
Moving off the paid SaaS backend removes the per-token line item entirely.
```

Empty result:

```
# recall: "nonexistent topic" — 0 passages, 0/2000 tokens (0 candidates, 0 dropped)

(no matching context)
```

Exit code `0` in both cases (a miss is data, not an error).

`--json` shape:

```json
{
  "query": "arctic embedder rollout",
  "budget_tokens": 2000,
  "used_tokens": 1712,
  "passage_count": 3,
  "candidates_considered": 9,
  "dropped": 6,
  "truncated": false,
  "fts_count": 14,
  "context_block": "# recall: …\n\n[1] 9f2c1a4e | …",
  "passages": [
    {
      "ref": 1,
      "id": "9f2c1a4e-2b7d-4f11-9a3e-7c1d5e0b8a44",
      "id_prefix": "9f2c1a4e",
      "title": "Arctic embedder rollout notes",
      "date": "2026-05-02",
      "source_kind": "manual",
      "content_type": "note",
      "tags": ["embeddings", "infra"],
      "score": 0.0312,
      "tokens": 604,
      "truncated": false,
      "text": "Snowflake Arctic Embed v2 is now …"
    }
  ]
}
```

**Backward-compatibility risk: none for `brain search`.** `recall` is a brand-new command; `hybrid_search`'s signature and `SearchResult` are read-only consumers here — **no field is added to `SearchResult`** (the date is fetched separately), so `brain search --json`, `brain explain --json`, MCP `brain_search`, and the eval harness see byte-identical output. The one shared-code edit is the `timeline._budget_doc_summaries` delegation: a *private* function whose observable behaviour is unchanged and whose existing tests are the regression gate.

#### 3.2 MCP `brain_recall`

Defined in `src/brain/mcp_server.py` immediately after `brain_search`, matching its conventions exactly (`@mcp_app.tool()`, flat scalar params mirroring CLI flag names 1:1, `_mcp_conn(state)` connection reuse, `_wrap_db_error` / `_wrap_embed_error`, ISO dates via `_parse_iso_datetime`):

```python
@mcp_app.tool()
def brain_recall(
    query: str,
    budget: int = 2000,
    max_candidates: int = 25,
    source: str | None = None,
    tag: str | None = None,
    since_days: int | None = None,
    fts_only: bool = False,
    person: str | None = None,
    after: str | None = None,
    before: str | None = None,
    kind: str | None = None,
    thread: str | None = None,
    draft: bool | None = None,
    has_tag: str | None = None,
    without_tag: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]: ...
```

Returns the `--json` payload above **minus** `context_block` duplication concerns — it returns both `context_block` and `passages`, so a client can paste one string or reason over the structure. `budget < 1` → `_mcp_error(INVALID_PARAMS, "budget must be >= 1")`, mirroring `brain_search`'s `limit < 1` guard (`src/brain/mcp_server.py:386-387`). The `tag`/`has_tag` conflict check is copied verbatim from `:389-394`.

**`brain_recall` deliberately mints NO `session_id`** (unlike `brain_search`, `:399`). Rationale, and this is load-bearing: the `no_click` gap detector (`src/brain/gaps.py:66-81`) flags any `search_queries` row with a non-NULL `session_id` that no in-session `opened`/`clicked` interaction follows. A recall's *result is the content* — an agent will essentially never call `brain_show` afterwards, so every recall would be mined as a search failure and poison `brain gaps`. Logging with `session_id=NULL` (the CLI path's existing behaviour) makes recall invisible to `no_click` while keeping the `fts_count = 0` lexical-miss signal fully live.

#### 3.3 `brain usage`

```
brain usage [--days 30] [--json] [--raw-queries] [--limit 10]
```

| Flag | Type | Default | Help text |
|---|---|---|---|
| `--days`, `-d` | `int` (`min=1`) | `30` | `Lookback window in days.` |
| `--json` | `bool` | `False` | `Emit machine-readable JSON instead of the tables.` |
| `--raw-queries` | `bool` | `False` | `Include raw query strings in --json output (default: normalized labels only).` |
| `--limit`, `-n` | `int` (`min=1`) | `10` | `Rows per top-N section (top queries, most-opened docs).` |

Literal human output:

```
Brain usage — last 30 days (2026-06-25 → 2026-07-25)

  searches           412      opens             129      docs ingested     96
  unique sessions     57      feedback events    11      zero-result   38 (9.2%)

                 Activity by day (last 14 shown)
┏━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━┓
┃ Day        ┃ Searches ┃ Sessions ┃ Opens ┃ Zero-result ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━┩
│ 2026-07-24 │       23 │        4 │     9 │           1 │
│ 2026-07-23 │       17 │        3 │     6 │           3 │
│ 2026-07-22 │        0 │        0 │     0 │           0 │
└────────────┴──────────┴──────────┴───────┴─────────────┘

        By surface                        By agent
┏━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┓   ┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┓
┃ Surface ┃ Searches ┃ Opens ┃   ┃ Agent          ┃ Searches ┃ Opens ┃
┡━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━┩   ┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━┩
│ mcp     │      301 │   118 │   │ claude-code    │      274 │   103 │
│ cli     │      108 │    11 │   │ research-agent │       27 │    15 │
│ wiki    │        3 │     0 │   │ (unattributed) │      111 │    11 │
└─────────┴──────────┴───────┘   └────────────────┴──────────┴───────┘

          Top queries                        Most-opened documents
┏━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┓   ┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┓
┃ Count ┃ Query                 ┃   ┃ ID       ┃ Title               ┃ Opens ┃
┡━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━┩   ┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━┩
│   14× │ embedding backend     │   │ 9f2c1a4e │ Arctic rollout notes│    19 │
│    9× │ hnsw dim cap          │   │ 3b71e0d9 │ Platform sync       │    12 │
└───────┴───────────────────────┘   └──────────┴─────────────────────┴───────┘

Ingested in window: 96 documents (krisp 41, gmail 33, manual 22)
```

Empty window → `No usage recorded in the last 30 days.` and exit `0`.

`--json` shape (privacy default applied):

```json
{
  "days": 30,
  "window": {"after": "2026-06-25T00:00:00+00:00", "before": "2026-07-25T00:00:00+00:00"},
  "totals": {"searches": 412, "sessions": 57, "opens": 129, "feedback": 11,
             "documents_ingested": 96, "zero_result": 38, "zero_result_rate": 0.0922,
             "read_events": 541, "write_events": 107},
  "by_day": [{"day": "2026-07-24", "searches": 23, "sessions": 4, "opens": 9, "zero_result": 1}],
  "by_surface": [{"source": "mcp", "searches": 301, "opens": 118, "feedback": 9}],
  "by_agent": [{"agent_id": "claude-code", "searches": 274, "opens": 103, "feedback": 7},
               {"agent_id": null, "searches": 111, "opens": 11, "feedback": 2}],
  "top_queries": [{"query": "backend embedding", "count": 14, "normalized": true}],
  "top_documents": [{"id": "9f2c…", "id_prefix": "9f2c1a4e", "title": "Arctic rollout notes", "interactions": 19}],
  "ingested_by_source": [{"source_kind": "krisp", "count": 41}]
}
```

**Privacy decision (firm):** raw query strings are personal. `--json` emits the **normalized canonical label** (sorted, de-duplicated, casefolded tokens) with `"normalized": true` — exactly the boundary `top_search_failures(normalize=True)` already enforces for MCP (`src/brain/gaps.py:351-390`, plan-08 §6). `--raw-queries` opts in and flips the flag to `false`. The **human table shows raw strings**, matching `brain gaps`'s CLI rendering (`src/brain/cli.py:9696`) — a local terminal is the owner's own eyes; a JSON pipe is not.

**Backward-compat:** entirely new command + new columns; no existing output changes. `by_agent` is present-but-mostly-`null` on a brain that predates migration 024 — by design (see §5.2).

---

### 4. Module layout

| Path | New/changed | Purpose | Est. lines |
|---|---|---|---|
| `src/brain/token_budget.py` | **new** | Shared linear greedy packer + protocol-only token truncation. Zero deps beyond `dataclasses`/`typing`. | ~95 |
| `src/brain/recall.py` | **new** | `RecallPassage` / `RecallResult` dataclasses, `recall()` core, block renderer. No CLI/MCP knowledge. | ~230 |
| `src/brain/cli_recall.py` | **new** | Typer function `recall_command`, registered via `app.command("recall")(recall_command)`. Mirrors `cli_connect.py`'s "thin orchestration module" precedent (`src/brain/cli_connect.py:1-8`). | ~130 |
| `src/brain/usage.py` | **new** | Usage dataclasses + all aggregation SQL. **Why not `activity.py`:** that file is 207 lines; the aggregation adds ~220, landing at ~427 — over the 400-line target. New module, one reason to change. | ~300 |
| `src/brain/cli_usage.py` | **new** | `usage_app` Typer (`invoke_without_command=True` callback, mirroring `gaps_app`, `src/brain/cli.py:9627`) + Rich rendering. | ~185 |
| `src/brain/agent.py` | **new** | `normalize_agent_id()` + `resolve_agent_id()`. ~40 lines but genuinely shared by CLI, MCP, config. | ~55 |
| `src/brain/migrations/024_agent_attribution.sql` | **new** | Additive `agent_id TEXT` on 3 tables + 2 partial indexes. | ~55 |
| `src/brain/errors.py` | changed | `+class AgentIdInvalid(BrainError)`. | +8 |
| `src/brain/config.py` | changed | 4 knobs: `recall_budget_tokens`, `recall_passage_tokens`, `recall_max_candidates`, `agent_id`. | +45 |
| `src/brain/gaps.py` | changed | `agent_id` param on `record_search_query` + `UndefinedColumn` guard; promote `_canonical_key` → public `canonical_query_key`; extract the lexical-miss predicate to a public `ZERO_RESULT_PREDICATE_SQL` constant reused by `usage.py`; extend `search_queries_schema_hint` for `agent_id`. | +45 / −10 |
| `src/brain/interactions.py` | changed | `agent_id` param + INSERT column. | +18 |
| `src/brain/ingest/__init__.py` | changed | `"agent_id"` added to `_PROMOTED_COLUMNS` (`:171`) and to the str-passthrough loop in `_promote_metadata_to_columns`. | +4 |
| `src/brain/timeline.py` | changed | `_budget_doc_summaries` delegates to `token_budget.pack_greedy`. | +8 / −20 |
| `src/brain/cli.py` | changed | Register `recall_command` + `usage_app`; `--agent` on `search` / `rate` / `ingest-stdin`. | +40 |
| `src/brain/mcp_server.py` | changed | `brain_recall` tool; `agent_id` param on `brain_search` / `brain_show` / `brain_rate` / `brain_ingest_stdin`. | +130 |
| `src/brain/format.py` | changed | `recall_json()` projection (block rendering stays in `recall.py` — it is domain logic, not Rich). | +25 |

Every new file gets a one-line module docstring; every function a full type-hinted signature.

---

### 5. Design detail

#### 5.1 Feature 1 — `brain recall`

**Packing unit: expanded chunk windows, one per document.** Justification against the two alternatives:

- *Whole documents* — a live-corpus document can exceed 304 chunks (noted at `src/brain/search.py:107-110`). A single doc would blow a 2000-token budget and starve every other source. Rejected.
- *Bare best chunks* — a chunk boundary lands mid-argument; an agent gets a fragment with no surrounding context. Rejected.
- *Expanded chunk windows* — `hybrid_search(snippet_context_tokens=W)` already stitches the best chunk plus its neighbours (`src/brain/search.py:626-704`) into a coherent passage. This is the existing, tested, in-corpus definition of "a readable piece of a document". **Chosen.** One passage per document keeps source diversity high, which is exactly what an agent's limited budget wants.

Each packed unit renders as:

```
[<ref>] <id_prefix(8)> | <YYYY-MM-DD | "unknown"> | <source_kind | "manual"> | <title>
<passage text>
```

The citation marker `[N]` is the same convention `brain ask` already teaches models (`src/brain/ask.py:62`), so an agent that pastes a recall block and cites `[2]` is speaking a vocabulary the rest of the system understands.

**Data flow:**

```
1. over_fetch = min(max_candidates,
                    max(_MIN_CANDIDATES, ceil(budget / _ASSUMED_PASSAGE_TOKENS)))
                        # _MIN_CANDIDATES = 5, _ASSUMED_PASSAGE_TOKENS = 120
2. results = hybrid_search(conn, embedder=…, query=…, limit=over_fetch,
                snippet_context_tokens=cfg.recall_passage_tokens,
                vector_sim_floor=cfg.vector_sim_floor,
                recency_halflife_days=cfg.recency_halflife_days,
                diagnostics=SearchDiagnostics(), **metadata_filters)
3. dates  = _fetch_doc_dates(conn, [r.document_id for r in results])   # 1 query
4. blocks = [_render_passage(i, r, dates.get(r.document_id)) for i, r in …]
5. packed = pack_greedy(blocks, cost=embedder.count_tokens,
                        budget=budget - _ENVELOPE_TOKENS, always_include_first=False)
6. if not packed.indices and blocks:      # budget smaller than the smallest block
       head = truncate_to_token_budget(blocks[0], cost=…, budget=budget - _ENVELOPE_TOKENS)
       → single truncated passage, truncated=True
7. renumber refs 1..N over the SURVIVING passages (a dropped candidate must not
   leave a gap in the citation sequence)
8. record_search_query(conn, query=…, result_count=len(results),
                       fts_count=diagnostics.fts_count, session_id=None,
                       source="cli"|"mcp", agent_id=…, tenant_id=cfg.graph_tenant_id)
```

`_ENVELOPE_TOKENS = 48` reserves room for the `# recall:` header line so the *whole emitted block*, not just the passages, honours the budget.

**Dataclasses** (`src/brain/recall.py`, all frozen — immutability rule):

```python
@dataclass(frozen=True)
class RecallPassage:
    ref: int
    document_id: str
    title: str
    date: datetime | None
    source_kind: str | None
    content_type: str
    tags: list[str]
    score: float
    text: str
    tokens: int
    truncated: bool

@dataclass(frozen=True)
class RecallResult:
    query: str
    budget_tokens: int
    used_tokens: int
    candidates_considered: int
    dropped: int
    truncated: bool
    fts_count: int | None
    passages: list[RecallPassage]

    def context_block(self) -> str: ...
    def to_dict(self) -> dict[str, Any]: ...
```

**Signatures:**

```python
# src/brain/token_budget.py
TokenCost = Callable[[str], int]

@dataclass(frozen=True)
class Packed:
    indices: list[int]
    used_tokens: int
    dropped: int

def pack_greedy(
    rendered: Sequence[str],
    *,
    cost: TokenCost,
    budget: int,
    always_include_first: bool = False,
) -> Packed: ...

def truncate_to_token_budget(text: str, *, cost: TokenCost, budget: int) -> str: ...
```

`truncate_to_token_budget` binary-searches on **character length** (never on bytes — Python `str` slicing is codepoint-safe), calling `cost` O(log n) times. It needs only the `count_tokens` half of the `Embedder` Protocol (`src/brain/ingest/__init__.py`, `Embedder.count_tokens`), which is exactly why it works under `BRAIN_EMBEDDER=none`.

Each item's cost is floored at `max(1, cost(text))` so a degenerate counter returning `0` cannot admit unbounded items.

```python
# src/brain/recall.py
def recall(
    conn: psycopg.Connection[Any],
    cfg: Config,
    *,
    embedder: Embedder,
    query: str,
    budget_tokens: int,
    max_candidates: int,
    source_kind: str | None = None,
    tag: str | None = None,
    since_days: int | None = None,
    fts_only: bool = False,
    person_keys: list[str] | None = None,
    person_display_name: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    content_type: str | None = None,
    thread_id: str | None = None,
    draft: bool | None = None,
    without_tag: str | None = None,
) -> RecallResult: ...
```

`recall()` takes `person_keys`, not `--person` — the resolver (`brain.queries.resolve_person_to_keys`, `src/brain/queries.py:210`) raises `PersonNotFound` / `PersonAmbiguous`, and the CLI/MCP boundary maps those to its framework's error type, exactly as `hybrid_search` documents at `src/brain/search.py:293-298`.

**SQL** — the only SQL recall owns (everything else is `hybrid_search`'s):

```sql
SELECT id::text, coalesce(sent_at, ingested_at)
FROM documents
WHERE id = ANY(%s)
```
bound as `(doc_ids,)`. Parameterized, no interpolation.

**Error handling.** `recall()` raises nothing of its own; it propagates `psycopg.Error` and `EmbedError`. CLI maps them via the existing `_load_config_or_exit` / `typer.secho(..., err=True)` + `typer.Exit(1)` idiom; MCP maps via `_wrap_db_error` (`src/brain/mcp_server.py:183`) and `_wrap_embed_error` (`:214`). `record_search_query` stays best-effort by its own contract (`src/brain/gaps.py:110-141`) — a logging failure never costs the agent its context.

#### 5.2 Feature 2 — agent attribution

**Which tables get `agent_id`: all three.**

| Table | Gets it? | Why |
|---|---|---|
| `search_queries` | **Yes** | The headline question — which agent is querying, and is its hit rate worse than another's. Wired this release. |
| `interactions` | **Yes** | Which agent opened / rated what; makes `no_click` and rating data per-agent. Wired this release. |
| `documents` | **Yes** | "Which agent wrote this into my brain" is a real provenance question, and the write seam already exists (`_PROMOTED_COLUMNS`, `src/brain/ingest/__init__.py:171`). Wired this release for the `ingest-stdin` path only (CLI `--agent` + MCP `brain_ingest_stdin`) — that is the *agent-driven* ingest path (Krisp/Slack orchestration); `brain ingest` / `ingest-dir` are human paths and stay unattributed. Not a dead column. |

`024_agent_attribution.sql` (pure SQL, additive, idempotent, references no Python):

```sql
-- 024_agent_attribution.sql — record WHICH agent produced an event.
-- ``source`` (cli|mcp|wiki) already records the SURFACE; agent_id records the
-- ACTOR. Free-form TEXT with no CHECK: the set of agents is open-ended and
-- user-defined; the shape gate lives at the Python boundary
-- (brain.agent.normalize_agent_id). NULL = unattributed, which is what every
-- pre-024 row is and what any un-configured surface keeps writing.
--
-- All three ALTERs are nullable-TEXT-no-default, which PostgreSQL 11+ applies
-- as a catalog-only change — no table rewrite even on ``documents`` with its
-- STORED generated ``tsv`` column (contrast migration 021, which added a
-- GENERATED column and DID rewrite). NEVER edit shipped migrations 001-023.

BEGIN;

ALTER TABLE documents      ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE interactions   ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE search_queries ADD COLUMN IF NOT EXISTS agent_id TEXT;

-- Partial indexes: the ``brain usage`` by-agent rollups scan only attributed
-- rows, and on a brain that never sets BRAIN_AGENT_ID both indexes stay empty.
CREATE INDEX IF NOT EXISTS search_queries_agent_at_idx
    ON search_queries (tenant_id, agent_id, at DESC)
    WHERE agent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS interactions_agent_at_idx
    ON interactions (agent_id, at DESC)
    WHERE agent_id IS NOT NULL;

COMMIT;
```

**Default is `NULL`, not `'cli'`.** Three reasons: (a) every pre-024 row is genuinely unattributed and a literal backfill would be a fabricated fact; (b) `source` already carries `cli`/`mcp`/`wiki`, so `agent_id='cli'` would duplicate the surface in the actor field and make "which agent" unanswerable for the exact rows it claims to answer; (c) `NULL` lets `brain usage` render an honest `(unattributed)` bucket instead of a false `cli` agent. `brain usage` therefore reports `agent_id: str | None` and never `coalesce`s in SQL.

**Config knob** (`src/brain/config.py`, following the module's existing eager-validation idiom):

```python
DEFAULT_AGENT_ID: str | None = None      # module constant, alongside the other DEFAULT_*
agent_id: str | None = DEFAULT_AGENT_ID  # Config field
```
Loaded from `BRAIN_AGENT_ID`: unset / blank → `None`; otherwise passed through `normalize_agent_id`, which raises `ConfigError` at load time so a typo surfaces at startup, matching `_parse_positive_int_env`'s contract (`src/brain/config.py:442-462`).

```python
# src/brain/agent.py
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

def normalize_agent_id(raw: str | None) -> str | None:
    """Strip + validate an agent identifier. Blank → None; malformed → AgentIdInvalid."""

def resolve_agent_id(explicit: str | None, cfg: Config) -> str | None:
    """Precedence: explicit flag/param > cfg.agent_id (BRAIN_AGENT_ID) > None."""
```

**Writer changes** (both keep parameterized SQL):

```python
# gaps.py — additive kwarg, default None keeps every existing call site valid
def record_search_query(conn, *, query, result_count, session_id, source,
                        fts_count=None, agent_id=None, tenant_id="default") -> None
# interactions.py — same shape
def record_interaction(conn, *, document_id=None, action, source, query=None,
                       session_id=None, target_type=None, target_id=None,
                       graph_retrieved=False, agent_id=None) -> str
```

`record_search_query`'s `UndefinedColumn` guard (`src/brain/gaps.py:175-188`) is widened from `"fts_count" not in str(exc)` to a two-name membership test so a pre-024 DB *also* gets the swallow-with-`brain init`-hint treatment. Search is the daily driver; a binary upgrade landing before `brain init` must never break it. `search_queries_schema_hint` (`:198`) gains the matching third branch.

**Surface wiring:** CLI `--agent` on `search`, `recall`, `rate`, `ingest-stdin`. MCP `agent_id: str | None = None` on `brain_search`, `brain_recall`, `brain_show`, `brain_rate`, `brain_ingest_stdin`. `brain ingest-stdin --agent X` merges `{"agent_id": "X"}` into the `--metadata` dict with the flag winning over a same-named metadata key.

#### 5.3 Feature 3 — `brain usage`

Reuses, rather than reimplements:
- `brain.activity.iter_activity_docs` (`src/brain/activity.py:95`) → the "most-opened documents" table. It counts *all* interaction kinds per doc in a window, so the column is honestly labelled **Interactions**, not "Opens".
- `brain.activity.iter_ingested_docs` (`src/brain/activity.py:136`) → the ingested-doc detail (not the count; see below).
- `brain.gaps.ZERO_RESULT_PREDICATE_SQL` — the lexical-miss predicate currently inlined at `src/brain/gaps.py:47` (`fts_count = 0 OR (fts_count IS NULL AND result_count = 0)`), promoted to a module constant so `usage.py`'s zero-result rate and `gaps.py`'s detector can never drift apart.
- `brain.gaps.canonical_query_key` — `_canonical_key` (`src/brain/gaps.py:232`) made public for the `--json` normalization.

New SQL in `src/brain/usage.py`, all parameterized:

```sql
-- daily rollup
SELECT date_trunc('day', at)::date AS day,
       COUNT(*)                                                    AS searches,
       COUNT(DISTINCT session_id) FILTER (WHERE session_id IS NOT NULL) AS sessions,
       COUNT(*) FILTER (WHERE {ZERO_RESULT_PREDICATE_SQL})         AS zero_result
FROM   search_queries
WHERE  tenant_id = %s AND at >= NOW() - make_interval(days => %s)
GROUP  BY 1 ORDER BY 1 DESC

-- searches by surface / by agent (agent variant swaps the GROUP BY column)
SELECT source, COUNT(*) FROM search_queries
WHERE tenant_id = %s AND at >= NOW() - make_interval(days => %s)
GROUP BY source ORDER BY 2 DESC

-- interactions by surface / by agent, split read vs feedback
SELECT source,
       COUNT(*) FILTER (WHERE action IN ('opened','clicked'))                        AS opens,
       COUNT(*) FILTER (WHERE action IN ('rated_useful','rated_irrelevant','pinned')) AS feedback
FROM   interactions
WHERE  at >= NOW() - make_interval(days => %s)
GROUP  BY source ORDER BY 2 DESC

-- top queries
SELECT query, COUNT(*) AS n FROM search_queries
WHERE tenant_id = %s AND at >= NOW() - make_interval(days => %s)
GROUP BY query ORDER BY n DESC, query LIMIT %s

-- ingested count by source kind
SELECT s.kind, COUNT(*) FROM documents d
LEFT JOIN sources s ON s.id = d.source_id
WHERE d.ingested_at >= NOW() - make_interval(days => %s)
GROUP BY s.kind ORDER BY 2 DESC
```

The `{ZERO_RESULT_PREDICATE_SQL}` interpolation is a **module constant, never user input** — the CLAUDE.md ban is on concatenating *user input* into SQL; every runtime value here is a `%s` placeholder bound as a tuple. `make_interval(days => %s)` is the existing project idiom (`src/brain/search.py:334`, `src/brain/gaps.py:48`).

Dataclasses (`src/brain/usage.py`, all frozen): `UsageTotals`, `DailyUsage`, `SurfaceUsage`, `AgentUsage`, `QueryCount`, `SourceCount`, and the aggregate `UsageReport` with `to_dict(*, raw_queries: bool) -> dict[str, Any]`. `read_events = searches + opens`; `write_events = documents_ingested + feedback`. Public entry point:

```python
def build_usage_report(
    conn: psycopg.Connection[Any],
    *,
    days: int,
    tenant_id: str = "default",
    limit: int = 10,
) -> UsageReport: ...
```

Error handling: a pre-019/023/024 DB raises `UndefinedTable`/`UndefinedColumn`; `cli_usage.py` runs it through `search_queries_schema_hint` and, on a hit, prints the hint in red and `typer.Exit(1)` — byte-for-byte the pattern at `src/brain/cli.py:9666-9671`. Anything else re-raises.

---

### 6. Edge cases and failure modes

1. **Budget smaller than the smallest passage** (`--budget 20`). `pack_greedy` returns zero indices (`always_include_first=False` — an agent's budget is a hard ceiling, unlike the timeline/audio budgeters where an empty bundle is worse than an overflow). Recall then binary-search-truncates the top-ranked block to the budget, emits it as a single passage with `truncated=True`, and sets `RecallResult.truncated=True`. Never returns nothing when something matched.
2. **`--budget 0` or negative.** Rejected at the boundary: Typer `min=1`; MCP `_mcp_error(INVALID_PARAMS, "budget must be >= 1")`. `recall()` additionally clamps `max(1, budget_tokens)` defensively, mirroring `hybrid_search`'s `effective_limit = max(1, limit)` rationale (`src/brain/search.py:609-615`).
3. **`BRAIN_EMBEDDER=none` (`NullEmbedder`).** `count_tokens` works (`src/brain/embeddings.py:387-389`) but `embed()` raises `EmbedDisabledError`. `hybrid_search` auto-degrades to `fts_only` via the duck-typed `produces_embeddings` check (`src/brain/search.py:317-324`), so recall works with lexical-only ranking. **Recall must never call `embedder.embed()` directly** — enforced by a test with a stub whose `embed()` raises.
4. **Zero candidates.** Header line + `(no matching context)`, `passages: []`, exit `0`. The `search_queries` row is still written with `result_count=0` — that miss is precisely the `brain gaps` signal.
5. **Document deleted between the search and the date fetch.** `hybrid_search` already skips orphaned docs at `src/brain/search.py:514-521`, but the race can also land between step 2 and step 3. `dates.get(document_id)` returns `None` → the citation line renders `unknown` and the passage is kept (its text is already in hand). No `KeyError`.
6. **Degenerate token counter returning 0.** Per-item cost is floored at `max(1, cost(text))`, and the candidate list is already bounded by `max_candidates`, so packing terminates with at most `max_candidates` passages.
7. **A passage can never exceed ~400 tokens.** `hybrid_search` hard-caps the stitched snippet at `4 * SNIPPET_LENGTH` = 1600 chars (`src/brain/search.py:556-558`). Consequence, documented in `--help`: a large `--budget` yields *more passages*, not longer ones, and the achievable ceiling is `max_candidates × ~400` tokens (~10k at the default 25). Raising `--max-candidates` is the lever; note also that `hybrid_search`'s `CANDIDATE_LIMIT = 50` chunks with `PER_DOC_CHUNK_CAP = 3` (`src/brain/search.py:103`, `:110`) means at most ~17-50 distinct documents can ever surface.
8. **Migration 024 not applied but the binary writes `agent_id`.** `record_search_query` swallows `UndefinedColumn` with the actionable `brain init` hint (extended guard, §5.2) — search and recall keep working. `record_interaction` propagates by its documented contract (`src/brain/interactions.py:157-159`); its two callers already swallow (`src/brain/mcp_server.py:627-631`, CLI `rate`). `brain usage` fails loudly with the hint.
9. **Malformed `--agent` value** (empty, 500 chars, `\n` for log injection, `'; DROP …`). `normalize_agent_id` rejects anything outside `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$` → `AgentIdInvalid` → CLI `typer.BadParameter`, MCP `INVALID_PARAMS`, config `ConfigError` at load.
10. **Rich markup collision.** A title containing `[bold]`, or the `[1]` markers themselves, would break `console.print`. The recall block goes through `typer.echo` (plain stdout); `brain usage`'s Rich tables pass titles as *cell values*, which Rich does not parse as markup.

---

### 7. Security and safety

| Risk | Guard |
|---|---|
| SQL injection via `--agent`, `--query`, `--tag` | Every runtime value is a `%s` placeholder bound as a tuple. The one interpolated fragment (`ZERO_RESULT_PREDICATE_SQL`) is a module constant. Recall adds exactly one query, fully parameterized. |
| Unbounded / hostile `agent_id` from an MCP client | Regex-gated to 64 chars of `[A-Za-z0-9._:-]`, first char alphanumeric. Blocks newline log-injection and unbounded TEXT growth. |
| Raw query strings (personal) leaking through a JSON pipe | `brain usage --json` normalizes by default (`canonical_query_key`); `--raw-queries` is explicit opt-in. `brain recall --json` echoes back only the query the caller already supplied. The existing "never log a raw query at INFO or above" contract (`src/brain/gaps.py:139-141`) is preserved — recall logs `query=%r` at DEBUG only. |
| **Bulk content exposure via MCP `brain_recall`** — it returns document *bodies*, not snippets, to a remote client | Same trust boundary as the existing `brain_show` (which returns the full body), but higher volume. Three guards: (a) passages come only from `chunks.content` via `hybrid_search`, so every metadata filter — including `draft` — applies; (b) the token budget is a hard ceiling on volume per call; (c) **cross-section dependency:** once `025_document_sensitivity.sql` (SAFETY section) lands, `recall()` MUST pass its sensitivity filter through to `hybrid_search` like any other filter. Recall's filter kwargs are already a pass-through list, so this is a one-line addition — flagged here so it is not forgotten. |
| Destructive DB operations | None. Recall is read-only plus one best-effort `INSERT` into `search_queries`. `brain usage` is 100% read-only. No test or code path touches port 55432 or `./data/postgres`. |
| Migration rewriting a large table | All three `ALTER`s are nullable-TEXT-no-default → PG 11+ catalog-only, no rewrite, even on `documents` with its STORED generated `tsv`. Explicitly contrasted in the migration comment with `021`'s generated-column rewrite caveat. |
| PII in code/tests/fixtures | All fixtures use invented names (`Jordan Vale`, `Rowan Pike`) and `*.example.com` addresses. Sample output in this spec is synthetic. |

---

### 8. Test plan

**Red-first proof of the gap** — `tests/test_recall_budget.py::test_search_result_size_is_unbounded_by_tokens`: run `hybrid_search(limit=5, snippet_context_tokens=200)` against a seeded corpus of long synthetic docs, sum `count_tokens(r.snippet)` across results, assert the total exceeds 2000. This test **passes today** and documents the defect (there is no way to ask for a token budget). Immediately after it, `test_recall_never_exceeds_token_budget` **fails red with `ModuleNotFoundError: brain.recall`** — that is the gate implementation must turn green.

| File | Behaviour asserted |
|---|---|
| `tests/test_token_budget.py` (pure logic, 95% target) | `pack_greedy` stops before overflow; returns `indices`/`used_tokens`/`dropped` consistently; `always_include_first=True` admits an oversize first item, `False` returns empty; empty input → empty `Packed`; zero-cost items are floored at 1 and cannot loop; `truncate_to_token_budget` returns text within budget, is a no-op when already under, returns `""` for `budget<=0`, and never splits a codepoint (parameterized over an emoji + CJK fixture). |
| `tests/test_recall_budget.py` (`test_db` + `fake_embedder`) | The red-first pair above; packed total ≤ budget across budgets `[50, 200, 2000, 100_000]`; `--budget 20` yields exactly one `truncated=True` passage; refs are `1..N` contiguous after drops; passage order is descending `score`; one passage per document (no duplicate `document_id`). |
| `tests/test_recall_filters.py` | Every filter reaches `hybrid_search` unchanged — `--source`, `--tag`, `--without-tag`, `--since`, `--after/--before`, `--kind`, `--thread`, `--draft/--no-draft`, `--person` (resolved keys) — verified by seeding docs that only the filter can discriminate. `PersonNotFound` surfaces as `typer.Exit(1)` / `INVALID_PARAMS`. |
| `tests/test_recall_null_embedder.py` | With a stub exposing `produces_embeddings=False` and an `embed()` that raises, `recall()` returns passages and **never calls `embed`** (call-count assertion via the existing `CountingEmbedder` pattern, `tests/conftest.py:451`). |
| `tests/test_recall_render.py` (pure logic, 95%) | Block header line exact-match; citation line format `[1] 9f2c1a4e | 2026-05-02 | krisp | Title`; missing date → `unknown`; `source_kind=None` → `manual`; empty result block; `to_dict()` key set frozen. |
| `tests/test_timeline_budget_delegation.py` | `_budget_doc_summaries` output is byte-identical to the pre-refactor behaviour for: empty rows, NULL summary → title fallback, first-row-exceeds-budget inclusion. (Regression gate for the extraction.) |
| `tests/test_migration_024_agent_attribution.py` (`@pytest.mark.fresh_schema`) | Column exists and is nullable on all three tables; pre-existing rows survive with `agent_id IS NULL`; both partial indexes exist; re-running the migration is a clean no-op. |
| `tests/test_agent_id.py` (pure logic, 95%) | `normalize_agent_id`: `None`/`""`/`"  "` → `None`; valid passthrough with strip; 65 chars, leading `-`, `"a b"`, `"a\nb"`, `"a;DROP"` → `AgentIdInvalid`; `resolve_agent_id` precedence flag > env > `None`. |
| `tests/test_agent_attribution_writes.py` (`test_db`) | `record_search_query(agent_id="research-agent")` persists it; omitted → NULL; `record_interaction(agent_id=…)` persists on both the document row and the graph-target row; `ingest-stdin --agent` lands in `documents.agent_id` via metadata promotion; the explicit flag beats a same-named `--metadata` key. |
| `tests/test_agent_attribution_degraded.py` (`@pytest.mark.fresh_schema`) | On a schema with the `agent_id` columns dropped, `record_search_query(agent_id=…)` swallows `UndefinedColumn`, logs the `brain init` hint, and the caller's open transaction survives; a genuinely-unknown column still propagates. |
| `tests/test_usage_report.py` (`test_db`) | Seeded `search_queries` + `interactions` + `documents` produce exact totals; window boundary is exclusive of older rows; zero-result rate uses the shared lexical-miss predicate (a row with `fts_count=0, result_count=5` counts as zero-result); legacy `fts_count IS NULL` rows fall back to `result_count=0`; empty window → all-zero report, no exception; `by_agent` emits `agent_id=None` for unattributed rows (never `"cli"`). |
| `tests/test_usage_privacy.py` | `to_dict(raw_queries=False)` (the default) emits normalized labels with `"normalized": true` and the raw string appears **nowhere** in the serialized JSON; `raw_queries=True` emits raw with `false`. |
| `tests/test_cli_recall.py` / `tests/test_cli_usage.py` (85% target) | `CliRunner` exit codes; `--json` parses and matches the documented key set; no-results messages; a pre-024 DB prints the hint in red with exit 1; `--budget 0` → exit 2 (Typer). |
| `tests/test_mcp_recall.py` | `brain_recall` returns `context_block` + `passages` and **no `session_id`** key; `budget < 1` → `INVALID_PARAMS`; conflicting `tag`/`has_tag` → `INVALID_PARAMS`; a `psycopg.Error` maps to `INTERNAL_ERROR` without leaking SQL; the logged `search_queries` row has `session_id IS NULL` (the `no_click`-poisoning regression guard); `agent_id` round-trips to the row. |
| `tests/test_search_output_unchanged.py` | Golden-JSON regression: `brain search --json` and MCP `brain_search` return the exact pre-change key set (`id`, `title`, `source_kind`, `snippet`, `score`, `content_type`, `tags`) — proves recall added no field to `SearchResult`. |

Gates: `ruff check && mypy src/ && pytest`, ≥85% overall with `token_budget.py` / `recall.py` renderers / `usage.py` projections at ≥95%. No monkey-patching of production modules — the embedder, token counter, and `chat` seams are all constructor/kwarg injected.

---

### 9. Open questions (each answered)

1. **Should recall pack multiple passages from the *same* document when it dominates the ranking?**
 → **No, one per document in v1.** `hybrid_search` already groups to document granularity and returns one best chunk (`src/brain/search.py:488-494`); changing that means bypassing the grouping and re-plumbing chunk-level results, which risks the `brain search` contract. Diversity is also usually what a bounded budget wants. Revisit only if eval shows single-document questions under-recalling.

2. **Should `brain recall` write to `search_queries` at all?**
 → **Yes, with `session_id = NULL`.** It is a real retrieval and belongs in the volume and lexical-miss statistics. NULL session keeps it out of `no_click` mining, which would otherwise mark every recall a failure (§3.2).

3. **Should `search_queries` distinguish a recall from a search?**
 → **Not in this release.** The obvious mechanism — a new `source` value — would require altering the shipped `CHECK` in `019` (`src/brain/gaps.py`'s and `interactions`' enum sets are kept in lockstep, `src/brain/interactions.py:50-61`), and migration 024 is scoped to attribution only. Recommended future path: a separate migration adding `search_queries.command TEXT`. Documented as a known limitation in `brain usage --help`.

4. **`agent_id` on `documents` — wire it or reserve it?**
 → **Wire it, narrowly.** Only `ingest-stdin` (CLI + MCP), via the existing `_PROMOTED_COLUMNS` seam. That is a 4-line change, is the genuinely agent-driven ingest path, and avoids shipping a column no writer touches (which an audit would rightly call dead schema). `brain ingest` / `ingest-dir` / `note new` / `capture` stay unattributed and are explicitly out of scope.

5. **Should there be an MCP `brain_usage` tool?**
 → **Not this release.** `brain usage` is an owner-facing introspection command, and exposing it over MCP widens the query-string exposure surface for marginal agent value. If added later it MUST hard-code the normalized projection (no `raw_queries` parameter), exactly as `brain_gaps` does (`src/brain/mcp_server.py:523`).

6. **Default `--budget`: 2000 or something larger?**
 → **2000.** It is a meaningful fraction of a small context window, and with the ~400-token-per-passage ceiling (§6.7) it yields 4-6 diverse passages — a useful default block that no reasonable agent will choke on. Configurable via `BRAIN_RECALL_BUDGET_TOKENS` for anyone with room to spare.

7. **Should `recall()` force-include the top passage when it alone exceeds the budget (timeline/audio behaviour) or truncate it?**
 → **Truncate.** For a prompt-assembly budgeter, an overflowing first item is a cosmetic problem; for an agent's context window it is the exact failure this feature exists to prevent. The two behaviours are selectable via `pack_greedy(always_include_first=…)`, so both callers get what they need from one implementation.

8. **`brain usage` "most-opened documents" — new `opened`-only SQL, or reuse `iter_activity_docs`?**
 → **Reuse `iter_activity_docs`** (`src/brain/activity.py:95`) and label the column **Interactions**. Writing near-duplicate SQL for a marginally narrower definition violates DRY; the honest label costs nothing. If opens-only becomes necessary, `iter_activity_docs` grows an `actions: tuple[str, ...] | None` filter rather than spawning a sibling query.
