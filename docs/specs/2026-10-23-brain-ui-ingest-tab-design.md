# `brain ui` Ingest tab — design

**Date:** 2026-10-23
**Status:** Design. **Not approved for implementation**: CLAUDE.md rule 7 requires explicit approval.
**Feature id:** F14 v2 (the tab `docs/specs/2026-07-25-brain-ui-design.md` §1.1 deferred to "v2").
**Parent specs:** [`2026-07-25-brain-ui-design.md`](./2026-07-25-brain-ui-design.md) (F14) and
[`2026-08-10-wiki-to-ui-consolidation-design.md`](./2026-08-10-wiki-to-ui-consolidation-design.md).
**Backlog entry:** `docs/plans/2026-10-23-backlog-reconciliation.md` §3, items 4 and 6.
**Measured at:** `second-brain/master` `0bf3e36`. Every `file:line` below was read and re-checked at
that commit (last checked in review round 1). Line numbers drift; where a symbol is named, trust the
symbol over the number.

---

## 0. Decisions at a glance

| # | Question | Decision | Section |
|---|---|---|---|
| D1 | Where does an ingest run? | On **one dedicated worker thread**, owned by an in-process `JobRegistry`. Never in a request handler, never on the event loop, never via Starlette `BackgroundTask`. | §4.1 |
| D2 | Progress transport | **Polling** `GET /api/jobs/{id}?after=<seq>` over an append-only event log. No SSE in v1. | §4.3 |
| D3 | Cancellation | **Cooperative.** Honoured while queued, after staging/validation, after extraction, and before each embedding batch, all of which come before any write. Extraction itself is not interruptible. `enter_commit()` is an atomic check-and-close after the last batch, so a cancel accepted during any embedding path is always honoured. After that point, cancel is refused with 409. Only on the four no-embed paths can an accepted cancel go unobserved; the job then ends `succeeded` with a `cancel_too_late` event. | §4.4 |
| D4 | Server restart | Jobs are **in memory only**: no migration, no job table. Re-submitting is safe because ingest is idempotent. Staged uploads are swept at startup. | §4.6 |
| D5 | Concurrency | **One running job, at most four queued.** A fifth gets 429. One worker for every job kind, the future Agent tab included. | §4.5 |
| D6 | v1 inputs | **Paste first** (text or markdown, no new surface). **File upload second**, as a raw `application/octet-stream` body, not multipart. **No URL ingest. No server-side path ingest.** | §3, §5 |
| D7 | `python-multipart` | **Not used**, even though it is already installed (§2 C1). Multipart is the one body a cross-site `<form>` can send, and the security model is built on refusing it. | §2, §7.2 |
| D8 | Where ingest is allowed | **Loopback-bound, writable servers only.** `--read-only` refuses it (as today, in middleware). A `--host` / `--lan` bind refuses it with 403 `ingest_requires_loopback`. | §7.1 |
| D9 | Graph sync | **Not done by the job.** It passes `graph_syncer=None`, as the UI does today. The vault watcher's `GraphLane` reconciles the new mirror file. The result panel says which applies. | §5.6 |
| D10 | Mutation confinement | One **new** permitted mutator, `ui/ingest_service.py`. `notes_service.py` is near the ceiling (711 lines) and stays notes-only. The "only module permitted to mutate" rule becomes "only these two". | §1.3, §5 |

---

## 1. Inherited constraints

### 1.1 Binding, and what actually enforces each one

This document restates no global constraint. The F14 constraints (`2026-07-25-brain-ui-design.md`,
"Inherited constraints") and the consolidation spec's §2 apply unchanged. That covers PII, prod-DB
safety, the quality gates, the 85% coverage floor, no monkey-patching and the 800-line ceiling. The
table separates what a test enforces from what is only a rule, because §2 shows the difference
matters.

| Constraint | Enforced by | Kind |
|---|---|---|
| Offline front end: no `http://` / `https://` in the static tree | `tests/test_ui_static_assets.py`, which walks the tree | **Test** |
| `import brain.cli` does not load starlette / uvicorn | `tests/test_cli_ui.py` (a `sys.modules` subprocess check) | **Test** |
| Every mutation is `Content-Type: application/json` (else 415) | `RequestGuardMiddleware._check` (`ui/security.py:221-225`), pinned in `tests/test_ui_security.py:484-487` | **Test + code** |
| Origin must equal the bound origin; a missing Origin is refused; `Sec-Fetch-Site` must be same-origin when sent | `ui/security.py:207-219` | **Test + code** |
| Host allowlist (anti-DNS-rebinding), outermost | `TrustedHostMiddleware` in `build_middleware` (`ui/security.py:228-243`) | **Test + code** |
| `--read-only` blocks every non-safe method before routing | `ui/security.py:201-205` | **Test + code** |
| One `innerHTML` in the whole front end (the server-rendered note body) | `check_single_innerhtml` (`tests/test_ui_static_behaviour.py:296`) | **Test** |
| "No new runtime dependencies" | **Nothing mechanical.** No test reads `[project.dependencies]`; `tests/test_dev_dependency_pins.py` covers dev pins and the starlette floor only. | **Rule** (F14 binding decision 1; consolidation §2.1) |
| Search delegates to `hybrid_search` | `ui/queries.py` docstring + review rule | **Rule** |
| Full-corpus operations never run from a request handler | `notes_service.py:27-30` docstring + review rule | **Rule** |
| Exception logging by class only | `tests/test_log_privacy.py` G1, an AST scan of `src/brain` | **Test** |

**Correction to the brief that commissioned this spec.** The brief said the zero-new-dependency
rule "is *enforced*". What the consolidation spec §2.1 calls enforced is the **offline guarantee**
(no CDN), not the dependency list. Nothing would fail if `python-multipart` were added to
`pyproject.toml`. D7 therefore rests on the security argument in §7.2, not on a test.

### 1.2 Architecture this design must fit

- **Handlers are `async def`, and they call synchronous psycopg directly** (`ui/routes_notes.py:21-130`,
  and every `routes_*.py`). They run **on the event loop**, not in a threadpool. The one exception is
  `routes_meta.watcher`, which uses `run_in_threadpool` (`ui/routes_meta.py:68`). The `UiContext`
  docstring says "Starlette runs sync endpoints in a threadpool" (`ui/context.py:15-17`). That is
  true of Starlette, but no endpoint here is sync. **Consequence:** an ingest run inside a handler
  would freeze every other request, including the job's own status polls, for tens of seconds or
  minutes. D1 follows from this.
- **Per-request connections** from `UiContext.conn_factory`, in autocommit (`ui/server.py:373-396`).
  A job needs the same thing on its own thread.
- **No lifespan today.** `create_app` builds `Starlette(routes=…, middleware=…, exception_handlers=…)`
  (`ui/app.py:188-197`). A worker thread needs startup and teardown, which Starlette's `lifespan=`
  supplies.
- **No graph syncer.** `cli_ui.py:343-352` builds the context without `graph_syncer`, so
  `ctx.graph_syncer` is `None` and every UI write skips graph sync today. D9 keeps that posture.

### 1.3 Mutation confinement, amended

`notes_service.py` says it is "the **only** module in this package permitted to mutate anything"
(`:1`), and the F14 review rule rejects "a vault file write outside `notes_service.py`". Ingest is a
mutation that writes a mirror file. It cannot live in `notes_service.py`: at 711 lines, folding
ingest in would cross the ceiling. So this spec amends the rule in the same PR that adds the module:

> Exactly two modules in `brain.ui` may mutate: `notes_service.py` (authored notes) and
> `ingest_service.py` (ingest jobs). Both delegate to functions the CLI already owns. A route module
> that writes, or a third mutator, is a review rejection.

The same PR updates the docstrings of both modules, plus two rules stated elsewhere:

- the F14 spec's §9.3 review rule ("reject any change that … writes a vault file outside
  `notes_service.py`");
- the consolidation spec's §2.3 ("Mutation is confined to one module, which delegates rather than
  implements"), which restates the `notes_service.py`-only rule as binding.

---

## 2. Findings that change the original deferral's rationale

The 2026-07-25 spec gave two reasons for deferring this tab: the job-runner problem, and
`python-multipart`. Re-measured:

**C1 — `python-multipart` is already installed transitively, like starlette and uvicorn were.**
In the main checkout's venv, `mcp-2.0.0.dist-info/METADATA` lists `Requires-Dist:
python-multipart>=0.0.9` and `Requires-Dist: sse-starlette>=3.0.0`, beside `starlette` and
`uvicorn`. Promoting it would add **zero wheels** on that resolution. That is exactly how F14
justified promoting starlette. **Unverified:** whether every `mcp` release inside our declared range
(`mcp>=1.2,<3.0`, `pyproject.toml:293`) carries it. The floor `1.2` may not. So "zero new wheels"
holds for the measured venv only. **This does not revive multipart** (D7, §7.2). It does mean the
original objection was the weaker one.

**C2 — The real blocker for upload is the security model, not the dependency.** The CSRF defence's
third layer is "`Content-Type: application/json` is required. This alone defeats HTML `<form>` CSRF
(a form can only send urlencoded / multipart / text-plain)" (`ui/security.py:20-23`). A multipart
upload endpoint would accept exactly the content type a hostile page's `<form>` can submit without a
preflight, so the server would be one Origin check away from cross-site writes. An
`application/octet-stream` body is not CORS-safelisted, so the browser must preflight it, which
keeps the property the JSON gate provides (§7.2).

**C3 — The embed runs inside `ingest_document`, but before the write transaction, and the
summary runs after the commit.** The order inside `_ingest_within_transaction` is:

1. the read-only dedup decision (`_resolve_ingest_action`, which "performs only SELECTs",
   `ingest/__init__.py:929-933`); a `skip` returns at once;
2. chunking, then `_embed_chunks` (`:1201`), **before** `with conn.transaction():` opens (`:1209`);
3. the INSERT/UPDATE of the document and its chunks, committed in one transaction;
4. post-commit, in autocommit: the Krisp source hook, then the auto-summary as its own `UPDATE`
   (`_enrich_post_ingest_hook`, `:1278-1298`).

Back in `ingest_document`, the vault mirror is written after that (`regenerate_vault_file`, `:845`),
and then graph sync if a syncer was passed. The docstring of `_ingest_within_transaction` states the
same order (steps 2–4, `:1153-1162`).

**The `ingest_document` docstring is stale on one point.** It says the summary is written "inside the
same transaction" (`:690-693`). The code, and the inner docstring, run it after the commit. The
backlog lists this as residue BL-12.

**Consequence:** a cancellation raised from inside `embed()` aborts before anything is written. That
is what makes cooperative cancel (D3) safe without changing `ingest_document`. §9 pins it with a test,
so a future reordering turns the suite red.

**C4 — The pipeline is sequential-use only.** "This pipeline is safe for sequential CLI use only; a
concurrent dual-INSERT of the same key still raises IntegrityError on the partial unique indexes"
(`ingest/__init__.py:949-952`). This is a correctness reason for D5's single worker, not only a
memory one. A concurrent CLI ingest from another process can still collide, so §4.5 maps that
error.

**C5 — Two duck-typed capability flags live on the embedder.** `is_hosted_embedder` reads
`hosted_egress` via `getattr`, **defaulting to `False`** (`embeddings.py:521-547`, the `getattr` at
`:547`). `_embed_chunks`
reads `produces_embeddings` the same way (`ingest/__init__.py:513-548`). Neither flag is in the
`Embedder` Protocol (`ingest/__init__.py:66-81`). **A progress wrapper that forwards only `dim`,
`embed` and `count_tokens` would silently disable the F6 egress veto**: a confidential upload
under Voyage would have its body POSTed off-machine. §5.3 makes forwarding both flags a hard
requirement, with a test that fails if either is dropped.

**C6 — A PDF's title is its file's stem.** `extract_pdf` sets `title=Path(path).stem`
(`ingest/pdf.py:29`). An upload staged under a random temp name would be titled with that name. §5.2
stages under the sanitized original basename.

**C7 — Body caps today run after the whole body is buffered.** `json_body` does
`await request.body()` (`ui/_http.py:39`), and `MAX_BODY_BYTES = 256 * 1024` is checked afterwards
in the schema (`ui/schemas.py:70, 508`). For a 256 KiB note body that is fine. For an upload it is a
memory-exhaustion vector. §7.3 enforces the cap on the stream.

**C8 — Starlette `BackgroundTask` blocks shutdown and cannot be tracked.** Per the Starlette docs
(lifespan), teardown "runs after all connections close and background tasks finish". A background
task has no registry, no progress and no cancel, so it is the "spinner that lies" the original spec
refused to ship (§1.1 there). Rejected in §4.1.

---

## 3. Scope

### 3.1 In scope (v1 = phases 1–3 of §10)

1. **Paste ingest.** A title, a body (text or markdown), optional tags, a content type from a fixed
   list, and a sensitivity of `normal` or `confidential`. It is ingested as an **ingested-tier**
   document from source `manual`, through `ingest_document`, the pipeline every `brain ingest*`
   uses. **The call shape is new** and is not `brain ingest-stdin` (§5.1).
2. **File upload.** One file per job, of a type in the upload allow-list:
   `ui/ingest_limits.UPLOAD_EXTENSIONS`, intersected at runtime with
   `ingest.dispatch.supported_extensions()` (`ui/upload_checks.uploadable_extensions`). At RM-13
   that is `.md`, `.markdown`, `.txt`, `.pdf` and `.docx`. The allow-list is deliberately not the
   registry itself: an extractor added later (RM-33's images, RM-38's audio) makes its type
   ingestible from the CLI, not uploadable, until a ticket adds it to the constant on purpose.
   `GET /api/ingest/config`'s `extensions` reports the live list. Upload by picker or drag and drop.
   *(Amended by RM-13; this sentence used to name the registry as the allow-list.)*
3. **A job panel.** Live state, phase, chunk progress, elapsed time and cancel. The outcome links to
   the new document and opens it in the Notes tab.
4. **The job subsystem** (§4), built to be reused by the Agent tab.

### 3.2 Non-goals

- **URL ingest.** It would add an SSRF surface, a network fetch from the server process, and an HTML
  extractor the brain does not have. A browser user can paste the page text instead.
- **Server-side path ingest** (`POST {path: "/some/local/file.pdf"}`). It is evaluated in §5.5 and deferred. A
  browser cannot hand the server a local path anyway: the file input exposes no path.
- **Directory ingest, Gmail, Krisp and Slack.** `ingest-dir`, `ingest-gmail` and the MCP-orchestrated
  sources stay CLI and agent jobs. Krisp also triggers a `gws` shell-out (`cli_ingest.py:489-491`),
  which is out of place in a web request.
- **`--force` re-ingest.** On the content-hash path, force is a DELETE plus INSERT under a **new
  UUID** (`ingest/__init__.py:646-649`), which breaks every link to the old one. It stays a deliberate
  CLI act.
- **Persisting jobs across restarts** (D4). **Ingest over a LAN bind** (D8).
- **Any change to ranking, chunking, enrichment or the secret guard.** The tab calls the pipeline.
  It does not re-implement it.

### 3.3 Paste ingest is not `+ New`

`+ New` (`POST /api/notes`) creates an **authored, vault-tier** note. Its file is the source of
truth, it is editable in place, and it lives where the user put it. Paste ingest creates an
**ingested-tier** document, the same as every `brain ingest*`. Its row is the source of truth, it is
mirrored under `_ingested/manual/`, it is content-hash deduplicated, and it is not edited as a file.
The tab's help text names the difference in one line, so a user who wants a note is sent to `+ New`.

### 3.4 Definition of done (whole tab)

1. Pasting a body produces a document that appears in the tree under `_ingested/manual/`, is found
   by search, has a summary when the enricher is reachable, and opens from the job panel.
2. Pasting the same body again reports **"Already in your brain"** with a link to the existing
   document. It writes nothing, except the owed `_ingested/` file if the existing row's mirror is
   missing (§5.4, `mirror_repaired`).
3. Uploading each supported type works. An unsupported or mismatched file is refused **before**
   anything is staged on disk. An oversize file is refused on the stream.
4. Cancel works at every cancellable point (§4.4) and writes nothing. That includes a cancel that
   lands during the last or only embedding batch, which the atomic `enter_commit()` honours. Once
   `enter_commit()` has closed, cancel is refused with a clear reason. A cancel accepted on one of
   the four no-embed paths (§4.4) is reported as `cancel_too_late` on a job that succeeded. A cancel
   is never silently dropped.
5. Killing the server mid-job and restarting it leaves no partial document and no staged file. The
   client says the job's outcome is unknown and that re-submitting is safe.
6. Every security test in §9.3 passes. `--read-only` and non-loopback servers render the tab
   disabled, with the reason.
7. `ruff check`, `mypy src/` and the full suite are green. Coverage is ≥ 95% on `ui/jobs.py` and
   `ingest/outcome.py`, and ≥ 90% on the services and routes.

---

## 4. The job subsystem

### 4.1 Where work runs (D1)

`src/brain/ui/jobs.py` holds the new module. It is **Starlette-free and psycopg-free**, so it is
testable without a server or a database.

```python
JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]

@dataclass(frozen=True)
class JobEvent:
    seq: int            # 1-based, strictly increasing per job
    at: datetime
    phase: str          # "queued" | "staging" | "extracting" | "embedding" | "finishing" | …
    done: int | None    # e.g. chunks embedded so far
    total: int | None
    code: str | None    # machine code for a notable event, e.g. "secret_findings"

@dataclass(frozen=True)
class JobSnapshot:      # what a poll returns; immutable copy, never the live record
    id: str             # uuid4 hex
    kind: str           # "ingest.paste" | "ingest.file" | later "ask"
    state: JobState
    cancellable: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    events: tuple[JobEvent, ...]
    result: Mapping[str, Any] | None   # set on success
    error: Mapping[str, str] | None    # {"code", "message"} on failure

class JobHandle(Protocol):              # what a runner sees
    def progress(self, phase: str, *, done: int | None = None, total: int | None = None) -> None: ...
    def note(self, code: str) -> None: ...
    def check_cancelled(self) -> None: ...   # raises JobCancelled
    def enter_commit(self) -> None: ...      # atomic: raise JobCancelled if requested, else close (idempotent)

Runner = Callable[[JobHandle], Mapping[str, Any]]

class JobRegistry:
    def __init__(self, *, clock: Callable[[], datetime], max_queued: int, retain: int,
                 retain_for: timedelta) -> None: ...
    def submit(self, kind: str, runner: Runner) -> JobSnapshot: ...      # raises QueueFull
    def get(self, job_id: str, *, after: int = 0) -> JobSnapshot: ...    # raises JobUnknown
    def list_recent(self) -> tuple[JobSnapshot, ...]: ...
    def cancel(self, job_id: str) -> JobSnapshot: ...                    # raises NotCancellable
    def run_next(self) -> bool: ...     # run ONE queued job on the calling thread; False if none
    def start(self) -> None: ...        # spawn the worker thread (loops on run_next)
    def stop(self, *, timeout: float) -> list[str]: ...   # returns ids still running at timeout
```

- **One lock, immutable snapshots.** Every mutation of a job record happens under one
  `threading.Lock`. Every read returns a fresh frozen `JobSnapshot`, as the immutability rule
  requires. A poll never sees a half-updated record.
- **`run_next` is the test seam.** Tests never start the thread. They call `registry.run_next()` on
  the test thread and assert the result, so there are no sleeps and no races in the unit tier. The
  watcher-absence lesson applies: assert on proof, not after a sleep. Production calls `start()` once
  from the app lifespan. The worker waits on a `threading.Condition` and calls `run_next`.
- **The worker thread is a daemon, and `stop` joins it with a timeout** (§4.6). A wedged Ollama call
  cannot then hang `brain ui`'s Ctrl-C. Integrity comes from the transaction, not the join.
- **It hangs off `UiContext`** as `jobs: JobRegistry | None`. That is a reference to a mutable
  object, the same shape as `watcher_probe` (`ui/context.py:161`). `build_context` constructs it,
  and tests construct a `UiContext` with their own instance or with `None` (in which case the
  ingest routes answer 503 `jobs_unavailable`). `create_app` gains a `lifespan` that calls
  `jobs.start()` and `jobs.stop()`.

**Rejected:**

- **Starlette `BackgroundTask`**: C8.
- **An `asyncio` task**: the work is synchronous psycopg plus httpx, so it would run on the loop
  and block it (§1.2).
- **`run_in_threadpool` per job**: it shares anyio's global 40-thread limiter with every other
  threadpool call in the process. The job would be untracked and uncancellable, and one long ingest
  would hold a slot the watcher probe (`routes_meta.py:68`) also draws from.
- **`multiprocessing`**: memory. A second process reloads tiktoken, psycopg and the extractors, which
  is the wrong trade on a swap-pressured machine (memory note on parallel-agent contention).
- **A jobs table**: D4.

### 4.2 Lifecycle

```
            submit                 worker picks            runner returns
  ─────────▶ queued ──────────────▶ running ──────────────▶ succeeded
               │ cancel              │  │ raises
               ▼                     │  └────────────────▶ failed
           cancelled ◀───────────────┘ cancel honoured at a
                                         check_cancelled()
```

- `cancellable` is true while queued, and while running **until** `enter_commit()` is called. The
  registry sets it to **false on every terminal state** (`succeeded`, `failed`, `cancelled`),
  whoever called what. So no terminal snapshot can claim it is still cancellable.
- **A cancel that is never observed.** This can happen only on a no-embed path (§4.4): on every
  embedding path, `enter_commit()` re-checks the flag. If a cancel was requested (202) and the
  runner then returns normally without passing another `check_cancelled()` or `enter_commit()`, the
  registry finalizes the job as `succeeded`. It appends one event with `code: "cancel_too_late"`, so the client can say "it
  finished before it could stop" instead of reporting either silent success or a false cancel.
- **Retention:** terminal jobs stay for `retain_for = 1 h`, at most `retain = 50`, evicted oldest
  first. Eviction runs on submit and on list, never on a timer.
- **The event log is capped at 200 events per job.** Embedding progress is coalesced to at most one
  event per batch, so a 2,000-chunk PDF at batch size 16 emits about 125 events. The cap holds even
  for larger inputs:
  - **Event kinds.** An event with a `code` (`secret_findings`, `cancel_too_late`, …) is
    *notable*. A `progress` event without one is *progress*.
  - **Never dropped:** notable events, the first event of each phase, and the latest progress event.
  - **At the cap:** the oldest *progress* event that is not the first of its phase is removed, so the
    newest event always survives.
  - **Polling across a gap.** `seq` numbers are never reused or renumbered, so a gap is allowed: a
    client polling `after=<seq>` gets every surviving event with a higher `seq`. Progress events are
    cumulative (`done` / `total`), so one dropped from the middle loses nothing a later event does
    not restate.

### 4.3 Progress transport (D2): polling

`GET /api/jobs/{id}?after=<seq>` returns the snapshot, with only the events whose `seq > after`. The
client keeps its last seen `seq`. The server keeps no per-client state.

The front end polls every **1 s while the job is running**. It backs off to 3 s after 30 s, and to
3 s whenever `document.visibilityState === "hidden"`. It stops at a terminal state.

**Why not SSE in v1:**

1. **`EventSource` cannot send a request header**, and the token must travel in `X-Brain-UI-Token`.
   The token is "a header, never a query parameter: query strings land in access logs and in
   `Referer`" (`ui/security.py:53-55`). A token-guarded server could not use `EventSource` at all.
   v1 is loopback-only (D8), so the token is never set there today. But the transport should not
   depend on that.
2. Every poll here is an **in-memory read with no DB call**, so it costs microseconds. Latency is
   bounded by the poll interval, and a 1 s granularity is honest for work measured in seconds to
   minutes.
3. **There is one caveat, and it is not ingest's to fix.** A poll still queues behind any
   synchronous handler running on the loop (§1.2), such as a search waiting on its embed. Polling
   cannot make that worse, and streaming would not make it better.

**The upgrade path, for the Agent tab.** The same event log can be served as
`GET /api/jobs/{id}/stream`, a Starlette `StreamingResponse` of NDJSON, one `JobEvent` per line. The
client reads it with `fetch()` plus a `ReadableStream` reader, **not** `EventSource`, so the token
header still works. Nothing in v1 depends on it. The polling endpoint stays as the fallback.
`sse-starlette` (C1) is not needed for this.

### 4.4 Cancellation (D3)

`POST /api/jobs/{id}/cancel` takes a body of `{}`, because the middleware requires JSON on every
mutation. It is idempotent.

| Job is… | Response | Effect |
|---|---|---|
| queued | 200, `state: "cancelled"` | Removed from the queue. It never runs. |
| running, before `enter_commit()` | 202, `state: "running"`, `cancel_requested: true` | A flag is set, under the registry lock. The runner raises `JobCancelled` at its next `check_cancelled()` **or** at `enter_commit()` itself, which re-checks the flag atomically. The job ends `cancelled`. This includes a cancel that lands while the last or only batch's `inner.embed` is in flight, which is the common case, since most pastes fit in one batch. |
| running, after `enter_commit()` | 409 `not_cancellable` | Nothing. The job finishes. The message says writing has started. |
| running, on a **no-embed path** (below), after point 3 | 202, `cancel_requested: true`. The server cannot know yet that no check is left. | The runner finishes and the job ends **`succeeded`** with a `cancel_too_late` event (§4.2). The panel says what was written, if anything. On a **skip**, the mirror repair may still write the owed `_ingested/` file of the existing row; that file is the only thing a skip can write. On the **no-chunks** path nothing is written. |
| terminal | 200, the unchanged snapshot | Nothing. |

**Cancel points in an ingest runner**, in order, each one before the write transaction (C3):

1. on pick-up;
2. after staging (upload) or validation (paste);
3. after extraction (`extract_path`). The extraction itself is **not interruptible**: pdfplumber on a
   large PDF can run for seconds, and the cancel waits for it;
4. **inside the progress embedder, before each batch** (§5.3), which covers the long phase.

**One design for when cancel stops being possible: the progress embedder's last batch.** This is
the design chosen, not the alternative of an explicit pre-write hook point. The runner calls
`handle.check_cancelled()` (point 3) and then `ingest_document` directly. It does **not** call
`enter_commit()` first: that pre-call would close the cancel window before the embed and make
point 4 unreachable. `enter_commit()` is called **only** by the progress embedder, after its last
batch returns.

**`enter_commit()` is an atomic check-and-close, under the registry lock.** If a cancel was
requested, it raises `JobCancelled`. Otherwise it sets `cancellable = false`, and from then on every
cancel gets 409. The check matters because nothing re-checks the flag after the last batch. A
cancel that arrives while the final slice's `inner.embed` is running would otherwise get 202 and
then be ignored. Raising there is safe, because nothing has been written yet: the embed returns
before the transaction opens (C3).

So the 202 window and the honour window are the same window, closed in one critical section. A
202 is never followed by a write on an embedding path. From `enter_commit()` on, `ingest_document`
opens its transaction, commits, runs the post-commit summary (an autocommit `UPDATE`, one Ollama
chat call; C3), then writes the mirror. None of that is interrupted.

**Four paths make no embed call, so point 4 never runs and `enter_commit()` is never called:**

- a dedup `skip`;
- **no chunks:** an insert whose content chunks to nothing returns
  `IngestResult(document_id=None)` before any embed (`ingest/__init__.py:1192-1196`). This is the
  `empty` outcome (§5.4);
- `produces_embeddings = False`;
- the F6 egress veto (`_embed_chunks`, `ingest/__init__.py:541-544`).

On these paths, and only on these, a cancel that arrives after point 3 is the "no-embed path" row
above: the job ends `succeeded` with `cancel_too_late`. The alternative, an extra hook inside
`ingest_document` just before its transaction, was rejected. It would change an over-ceiling
2,369-line module for a window that lasts milliseconds on these paths, since none of them makes an
Ollama embed call.

**`JobCancelled` must propagate through `ingest_document`.** It derives from `BrainError`, not from
`EmbedError`, so no handler in the pipeline that maps embed failures can swallow it. A test (§9.2)
cancels mid-embed and asserts there is no `documents` row, no `chunks` row, no `sources` row and no
mirror file.

### 4.5 Concurrency, limits and failure surfacing (D5)

| Limit | Value | Why |
|---|---|---|
| Running jobs | **1** | C4 (sequential-use pipeline), plus memory. One Ollama runner each for the embed and chat models is already resident during an ingest. |
| Queued jobs | **4** | A fifth `submit` gets 429 `queue_full`. A queue is for "drop three files", not for bulk import. `brain ingest-dir` is for bulk. |
| Staged upload bytes, total across queued jobs | **64 MiB** | Uploads wait on disk, not in memory. This bounds disk use. Over it: 429 `staging_full`. |
| Embed batch size (progress embedder) | **16 texts** | Small enough that a UI search's embed can interleave between batches at the Ollama runner, so search stays usable during an ingest. |

These are module constants in `ui/ingest_limits.py`, **not config knobs, in v1.** `config.py` is over
the ceiling, and each knob there costs a two-place growth record (CLAUDE.md ceiling section). They
can be promoted to knobs if a real need appears.

**Failure surfacing.** A job that raises ends `failed` with `error = {code, message}`. The message is
**fixed per code** and never the exception's text, for the same reason `_wrap_db_error` exposes only
`type(e).__name__` (F14 spec §3) and `describe_failure` exists (`errors.py:393`). An exception
message can quote document content.

| Raised | `code` | Fixed message (abridged) |
|---|---|---|
| `JobCancelled` | (state `cancelled`) | — |
| `SecretGuardError` | `secret_guard_rejected` | "The secret guard is set to reject and found a credential-like string. Nothing was written." |
| `SensitivityError` | (validated at submit: 400) | — |
| `ValueError` from `extract_path` | `unreadable_file` | "The file could not be read as `<ext>`." |
| `EmbedError`, `transient=True` | `embedder_busy` | "The embedding model did not answer. Nothing was written; retry." |
| `EmbedError`, permanent | `embedder_unavailable` | "The embedding model is not usable (not pulled, or a bad key). Nothing was written." |
| `psycopg.errors.UniqueViolation` | `concurrent_ingest` | "Another process ingested the same content at the same moment. Retrying is safe." |
| other `psycopg.Error` | `database_error` | "Database error (`<ClassName>`)." |
| anything else | `internal_error` | "Internal error (`<ClassName>`). The document may already have been saved; re-submit to check (identical content is a no-op)." This message never says nothing was written, because an error after the commit (for example from the post-commit summary) lands here with the row already saved. The same is true of `database_error`, whose message also makes no such claim. Every other code is raised before the write. |

The server log line is `"ui ingest job %s failed: %s", job_id, describe_failure(exc)`. That is the
class only, and it already satisfies `tests/test_log_privacy.py` G1. **No `logger.exception` is
added**: the G1 allowlist has a reason per entry, and this path does not need a traceback.

### 4.6 Server restart and shutdown (D4)

**Shutdown** (Ctrl-C, through the lifespan teardown) runs `jobs.stop(timeout=10.0)`:

1. Refuse new submits (503 `shutting_down`), and move queued jobs to `cancelled`.
2. Request cancel on the running job.
3. Join for up to 10 s. A job that is embedding exits at its next batch: one 16-text embed call,
   normally a few seconds. A job still inside `extract_path` or in its post-commit summary call
   cannot be interrupted, and either can outlast 10 s on a large PDF or a cold model. Step 4 exists
   for those.
4. If the join times out, print one line naming the job id: "its outcome is unknown. Re-submit to be
   sure; identical content is a no-op." The daemon thread dies with the process. A transaction in
   flight rolls back, so the document is atomic.

**Restart.** The registry starts empty. What the user can lose, and what recovers it:

| Killed during | State left behind | Recovery |
|---|---|---|
| queued, staging, extracting, embedding | Nothing in the DB. A staged file, maybe. | The startup sweep (§7.4) deletes staged files. Re-submit. |
| the write transaction | Nothing. It rolls back. | Re-submit. |
| the post-commit summary call | A committed row with `summary IS NULL` **and** `vault_path IS NULL`, because the mirror is written after the summary (C3) | Re-submit is a dedup `skip`. The skip returns before the summary hook, so it **never enriches**, but the orphan repair (#23, `ingest/__init__.py:829-849`) writes the owed mirror and reports `mirror_repaired`. The summary is recovered by `brain enrich --backfill`. The panel's summary status (§5.4) shows "pending" and names that command. |
| after the summary, before the mirror | A row with its summary, and `vault_path IS NULL` | Re-submit. The orphan repair writes the owed mirror and reports `mirror_repaired`. |
| after the mirror | A complete document | Re-submit reports "Already in your brain". |

Every row ends at "re-submit", and **every re-submit is safe** because both input types dedup on
content (§5.4). The one thing a re-submit does not redo is a lost summary, which has its own
backfill command. The client stores the ids of jobs it submitted in `sessionStorage`. A poll that gets
404 `job_unknown` renders the sentence above, not a generic error. `sessionStorage` is read in
`try`, as the house rule for browser storage requires. Without it, the panel simply starts empty.

**Rejected:** a `ui_jobs` table. It would need a migration (allocation and review), it would hold
titles of possibly confidential documents in a second place, and every outcome it could report is
re-derivable by re-submitting.

### 4.7 Reuse by the Agent tab (v3)

The registry is kind-agnostic, and the Agent tab plugs in with no change to `jobs.py`:

- **Runner.** `ask.ask` (`ask.py`) is driven by an injected `ChatJson`. The ask runner wraps that
  callable so each plan, retrieve, reflect and synthesize step calls `handle.progress(...)` and
  `handle.check_cancelled()` between LLM calls. Ask writes nothing to the corpus except an
  interaction log row, so `enter_commit()` is never needed. Ask is cancellable throughout.
- **One worker for everything.** Ask and ingest both load Ollama models, and this machine cannot
  afford two concurrently. An ask submitted behind an ingest waits, and the panel says "waiting for
  the ingest job to finish".
- **What the Agent tab still owes itself:** the streaming endpoint (§4.3), conversation state, and
  the model-output rendering hardening pass that the F14 spec §1.1 requires. Model text is rendered
  with `textContent` only, never `innerHTML`. That is already the rule, and `test_ui_static_behaviour`
  enforces it.

---

## 5. The ingest service

### 5.1 Paste

`POST /api/ingest/paste`, JSON (§6). Validation happens **at submit, before queueing**, so a bad
request is a 400, not a failed job:

- `title`: 1–200 characters (`MAX_TITLE_CHARS`, `ui/schemas.py:71`, reused).
- `body`: non-blank after strip, and **≤ 2 MiB UTF-8**. The paste cap is far above the note cap of
  256 KiB, because transcripts are long. The `Content-Length` precheck (§7.3) runs first.
- `tags`: normalized by `brain.tags.normalize_tags`, as at every write boundary.
- `content_type`: one of a fixed allowlist, initially `note` (the default) and `transcript`, the two
  labels `ingest-stdin`'s help names (`cli_ingest.py:407-408`). Anything else is 400. The list is a
  constant beside the limits, so extending it is a one-line change. `krisp_action_items` is excluded
  on purpose, because it needs a parent meeting id (`cli_ingest.py:463-468`).
- `sensitivity`: `normal` (the default) or `confidential`, through `sensitivity.normalize_level`
  (strict). A typo is 400, so a user never gets a document they believe is protected and which is
  not (`ingest/__init__.py:765-771`).
- `enrich`: a boolean, default `true` (§12 Q2).

The runner builds the document with `ingest.stdin.make_doc(content=…, title=…, content_type=…,
metadata={"ingested_via": "ui"})`, then calls:

```python
ingest_document(conn, embedder=<progress embedder>, doc=…, source_kind="manual",
                source_external_id=None, tags=…, vault_root=cfg.vault_path,
                enricher=…, enrich=…, enrich_min_tokens=cfg.enrich_min_tokens,
                graph_syncer=None, secret_guard=cfg.secret_guard,
                allow_secrets=False, sensitivity=…)
```

**This is a new call shape, not `brain ingest-stdin`'s**, and the differences are deliberate:

| | `brain ingest-stdin` (`cli_ingest.py:399-513`) | UI paste |
|---|---|---|
| External id | **Required** (`--external-id`, `:403-405`), so dedup keys on `(kind, external_id)` (rule 3) | **None**, so `_upsert_source` returns `None` (`ingest/__init__.py:487-488`) and dedup is **by content hash** (rule 4): an identical body is a skip |
| `source_metadata` | The `--metadata` blob | **None**: no `sources` row is written at all |
| `force` | `--force` | Never (§3.2) |
| Graph sync | `graph_syncer=_build_graph_syncer(cfg)` | `None` (D9) |
| Agent attribution | `resolve_agent_id(--agent, cfg)` into `metadata.agent_id` (F10) | **None.** The UI is a *surface*, not an agent. F10 keeps the two axes apart (`agent.py` docstring), and the surface is recorded instead |
| `gws_runner` | Only for `--source krisp` | Never |

**Document metadata, decided:** exactly `{"ingested_via": "ui"}` for a paste, plus
`original_filename` for an upload (§5.2). Nothing else is copied from the request.

### 5.2 File upload

There are two requests, so the JSON-only gate stays intact for everything except one route:

1. **`POST /api/ingest/uploads`**, raw body, `Content-Type: application/octet-stream`. The original
   filename goes in an `X-Brain-Filename` header, percent-encoded UTF-8, and it is used **only** for
   its extension and its display stem. The server streams the body to a staging file, enforcing the
   caps on the stream (§7.3), sniffs it (§7.4) and returns `{upload_id, size, kind}`. It is not a
   job: it is bounded by the cap, and it is the only part that touches the request body.
2. **`POST /api/ingest/file`**, JSON `{upload_id, tags, sensitivity, enrich, title?}`, which submits
   the job.

The runner calls `extract_path(staged_path)`. The staged file is named
`<staging-dir>/<upload_id>/<sanitized-stem><ext>`, so the extractors' `Path.stem` title (C6) is the
user's filename, not a random token. Then it builds a new `ExtractedDoc`, never mutating the old
one:

```python
replace(doc, title=spec.title or doc.title, source_path=None,
        metadata={**doc.metadata, "ingested_via": "ui",
                  "original_filename": <sanitized basename>})
```

The optional `title` from `POST /api/ingest/file` therefore overrides the extracted one, validated
like a paste title (1–200 characters). Then it calls `ingest_document` exactly as in §5.1.

**Why `source_path=None`.** File-based dedup keys on `source_path` (`ingest/__init__.py:628-637`). A
staging path is meaningless after the job and would differ per upload, so dedup would never fire,
and the row would record a path that no longer exists. With `source_path=None`, an identical upload
dedups on the hash of the extracted text, which is the property §3.4 item 2 needs. The cost is in
§11 R4: a file ingested once by `brain ingest <path>` and once by upload becomes two documents.

`finally:` deletes the staging directory, whatever the outcome.

### 5.3 The progress embedder

`ui/ingest_progress.py` defines `ProgressEmbedder(inner, handle, batch_size=16)`, which conforms to
`brain.ingest.Embedder`:

- `dim`, `count_tokens`: delegate.
- **`hosted_egress` and `produces_embeddings`: forwarded as attributes read from `inner` with
  `getattr(inner, name, <the same default the reader uses>)`.** This is required, and the reason is
  C5. A test builds a fake embedder with `hosted_egress = True`, wraps it, ingests a confidential
  document, and asserts the chunk embeddings are NULL and `egress_notice` is set. A sibling test does
  the same for `produces_embeddings = False`. Deleting either forward makes one of them fail. These
  are **mutation-verified** per the memory note "prove the check can fail".
- `embed(texts, input_type=…)`: emits `progress("embedding", done=0, total=len(texts))`, then for each
  slice of `batch_size` calls `handle.check_cancelled()`, `inner.embed(slice, input_type=…)` and
  `progress(..., done=…)`. After the last slice it calls `handle.enter_commit()`, the atomic
  check-and-close of §4.4. That call raises `JobCancelled` if a cancel arrived during the final
  slice; otherwise it closes the window. Then the wrapper returns the concatenated vectors. Slicing
  does not change any vector: every backend embeds each text independently.
- **No embed call at all** happens on the four no-embed paths of §4.4: a `skip` (dedup), an insert
  with no chunks (`ingest/__init__.py:1192-1196`), `produces_embeddings=False`, and the egress veto
  (`_embed_chunks`, `ingest/__init__.py:541-544`). On these paths `enter_commit()` is never called. The runner **must not** call it before `ingest_document`, because that would close
  the cancel window before point 4 on every path (§4.4). The registry's terminal transition sets
  `cancellable=false` (§4.2), and an unobserved cancel ends as `cancel_too_late`.

The job builds **its own** embedder and enricher once per worker thread, with
`embeddings.make_embedder(cfg)` (`embeddings.py:550`) and `enrichment.make_enricher(cfg)`
(`enrichment.py:813`). The search path's `ctx.embedder` is used on the event-loop thread, and sharing
one HTTP client across threads is a question this design does not need to answer. Neither factory is
the CLI's `_build_*` helper, which lives in a Typer module the UI must not import (`test_cli_ui.py`).

### 5.4 Outcome mapping, dedup and what the user sees

The CLI already maps an `IngestResult` to a verb in `cli_ingest._ingest_outcome_verb`
(`cli_ingest.py:203-240`), but the UI cannot import a Typer module. **Extract it** into a new pure
module `brain/ingest/outcome.py`:

```python
class IngestOutcome(StrEnum):
    INGESTED = "ingested"; UPDATED = "updated"; SKIPPED = "skipped"
    EMPTY = "empty"; MIRROR_REPAIRED = "mirror_repaired"

def classify(result: IngestResult, *, force: bool = False) -> tuple[IngestOutcome, bool]:
    """(outcome, mirror_repaired)."""
```

`cli_ingest` keeps its wording by mapping the enum onto its verbs. `cli_ingest.py` is over the
ceiling, but this change **shrinks** it, so it owes no growth record. Its row in CLAUDE.md is
promoted per the table's rule.

| Outcome | The panel shows |
|---|---|
| `ingested` | "Added", plus title, a link that opens it in Notes (`?id=<uuid>`, the URL state `store.js:209-212` already reads), its `_ingested/manual/…` path, chunk count and summary status. |
| `skipped` | "**Already in your brain**", plus a link to the existing document. `_resolve_ingest_action` returns the existing id on a skip (`ingest/__init__.py:1013-1016`). |
| `empty` | "Nothing to index: no text could be extracted." For example an image-only PDF; see backlog item 3.5. |
| `mirror_repaired` | "Already in your brain. Its vault file was missing and has been rewritten." |
| `updated` | Not reachable from v1, which has no external id and no force. Rendered anyway. |

Plus three notices, which can stack:

- **Secret findings** under `warn` / `redact`: `IngestResult.secret_findings`, each one's kind,
  line, column and **masked** preview (F4). Never the matched text. Under `redact`: "stored with the
  matches redacted".
- **The egress notice** (`IngestResult.egress_notice`), when the veto fired: "findable by full-text
  search only".
- **Summary status**, read back from `documents.summary` after the job. `IngestResult` does not
  report enrichment, and enrichment failure is deliberately non-fatal: it runs after the commit, and
  "a post-commit enrichment failure leaves the committed document with `summary IS NULL`"
  (`ingest/__init__.py:1159-1162`).
  It shows "summary: written", "skipped (short or disabled)", or "pending: run
  `brain enrich --backfill`".

### 5.5 Server-side path ingest, evaluated and deferred

The option: `GET /api/ingest/inbox` lists the files in one configured drop folder (say,
`BRAIN_UI_INGEST_INBOX`), and `POST /api/ingest/inbox {name}` ingests one by name. It needs no upload
body, it keeps natural `source_path` dedup, and the memory cost is zero.

It is **deferred** to phase 4 (optional). It adds a config knob (a `config.py` growth), a second
traversal surface (symlinks out of the folder, names containing `/`, TOCTOU between listing and
open), and a lifecycle question nobody has answered (is the file moved after ingest?). Upload covers
the need. If it is built, it reuses `assert_within_vault`'s resolve-then-compare pattern
(`vault/paths.py`) against the inbox root, refuses symlinks (`lstat`), and accepts a **name**, never
a path.

### 5.6 The vault mirror, the watcher and the graph (D9)

- **The mirror.** `ingest_document` writes `_ingested/manual/<…>.md` **outside** its transaction and
  logs a mirror failure without raising (`ingest/__init__.py:807-873`). The UI changes nothing here:
  this is what `brain ingest` does today.
- **The watcher** sees the new mirror file as a create event and runs `sync_one_file`. For an
  ingested-tier file, sync treats the DB as authoritative (`vault/sync.py:14`, and the sensitivity
  rule at `:1205-1227`), so it cannot clobber what the job just wrote. Every CLI ingest already
  produces this exact interaction, so it is not new.
- **The graph.** The job passes `graph_syncer=None`, as every UI write does today (§1.2). Concept
  extraction runs about 8–10 minutes per new document against Ollama, under the per-tenant graph
  write lock (PR #53). Running it in the job would make the job minutes long, uncancellable after
  commit, and a direct competitor of the watcher's `GraphLane` for that lock. When the watcher is
  running, its `sync_one_file` on the new mirror calls `graph_syncer.reconcile` through the watcher's
  lane (`vault/sync.py:449-455`). The result panel shows one of three states:
  - **"graph: disabled"** when `cfg.graph_enabled` is false. That is the default
    (`graph_enabled: bool = DEFAULT_GRAPH_ENABLED`, `config.py:1126`). The watcher then builds no
    `GraphLane` (`vault/watch.py:285`) and nothing will sync the graph.
  - **"graph: queued (watcher)"** when the graph is enabled and the existing watcher probe
    (`GET /api/watcher`) reports the watcher running.
  - **"graph: not synced; run `brain graphrag build --backfill`"** when the graph is enabled but no
    watcher is running.

  **Phase 1 must verify** with a test that a `sync_one_file` over a freshly written `_ingested/`
  mirror reaches `reconcile`, rather than inheriting this claim.

---

## 6. HTTP API contract

The conventions are F14's §3, unchanged: JSON responses, the `{"error": {"code", "message"}}`
envelope, `Cache-Control: no-store` on `/api/`, and the security headers on every response. Every
route declares its methods explicitly, so Starlette answers 405 for anything else (`ui/app.py:1-7`).

| Method | Path | Body | Success | Notes |
|---|---|---|---|---|
| GET | `/api/ingest/config` | — | 200 `{enabled, reason, extensions, limits, content_types, sensitivity_levels}` | `reason` is one of `read_only`, `requires_loopback`, `jobs_unavailable`, or `null`. The tab renders from this, and hard-codes no limits. |
| POST | `/api/ingest/paste` | JSON §5.1 | 202 `JobSnapshot` | 400 on validation, 413 `body_too_large`, 429 `queue_full`. |
| POST | `/api/ingest/uploads` | **octet-stream** + `X-Brain-Filename` | 201 `{upload_id, size, kind}` | 400 `bad_filename`, 413 `upload_too_large`, 415 `unsupported_file` (extension or sniff), 429 `staging_full`. **The only route that accepts a non-JSON body** (§7.2). |
| POST | `/api/ingest/file` | JSON `{upload_id, tags?, sensitivity?, enrich?, title?}` | 202 `JobSnapshot` | 404 `upload_unknown` (also for an expired upload: staged files live 15 min), 429 `queue_full`. |
| DELETE | `/api/ingest/uploads/{upload_id}` | JSON `{}` | 200 `{upload_id, deleted: true}` | Discards a staged file the user removed before submitting. It takes a JSON body because the middleware refuses any non-safe method without one (415, `ui/security.py:221-225`), and it answers with JSON because `api.js` treats an unparseable 2xx as `malformed_response`: "there is no 204 and no empty-body 200" (`js/api.js:35-42`). Same shape as cancel. An unknown id, including one already deleted or expired, is 404 `upload_unknown`. |
| GET | `/api/jobs` | — | 200 `{jobs: [JobSnapshot without events]}` | Recent jobs, newest first. |
| GET | `/api/jobs/{id}?after=<seq>` | — | 200 `JobSnapshot` | 404 `job_unknown`. |
| POST | `/api/jobs/{id}/cancel` | JSON `{}` | 200 / 202 | 409 `not_cancellable` (§4.4). |

`GET /api/health` gains `"ingest_enabled": bool`, so the shell can grey the tab without a second
request.

**New `UiError` subclasses** in `ui/errors.py` (110 lines today): `UiPayloadTooLarge` (413),
`UiTooManyRequests` (429). Each carries a `code`, as the existing ones do.

**The GET-cannot-mutate table** (F14 §3.12) gains these rows. Every GET above is a pure in-memory or
config read. `DELETE /api/ingest/uploads/{id}` removes a staged file, which is not corpus data, and it
still passes the full mutation guard.

---

## 7. Security model

### 7.1 Who may ingest (D8)

Ingest is refused unless **all** of these hold:

1. **Not `--read-only`.** This is already enforced in middleware for every POST and DELETE
   (`ui/security.py:201-205`). Nothing new is needed.
2. **The server is loopback-bound**, meaning `UiContext.serve_confidential_bodies` was derived from
   `loopback` rather than from `--include-confidential`. Concretely, `build_context` sets a new field
   `ingest_enabled = loopback and not read_only` (`ui/server.py:371` already computes
   `loopback`). The ingest routes check it and answer 403 `ingest_requires_loopback`. The check is
   in the route, not the middleware, because it is about which routes exist on this bind, not about a
   request's shape. A test pins it on **every ingest mutation route** (every POST and DELETE under
   `/api/ingest/` and `/api/jobs/`). The GETs stay readable: `GET /api/ingest/config` answers 200 with
   `reason: "requires_loopback"`, which is how the tab knows to render disabled, and the job GETs are
   pure reads.
3. **The registry exists**, which it always does in production.

**Why loopback-only.** On a `--lan` bind the only guard is the token, and ingest is the largest cost
surface the server has: disk (staging), memory (PDF parsing), and minutes of Ollama time per
request. A leaked token on the LAN would let any device spend all three. Note-writing over LAN is
already allowed, but a note is bounded at 256 KiB and costs one embed. §12 Q1 records the decision
and when to revisit it.

### 7.2 The one Content-Type exception (D7)

`RequestGuardMiddleware._check` gains exactly one branch. The media type must be
`application/json`, **except** when `method == "POST"` and `path == "/api/ingest/uploads"`, where it
must be `application/octet-stream` (and JSON is refused there). Every earlier layer still applies to
that route unchanged: the token, read-only, the exact Origin, and `Sec-Fetch-Site`.

Why this preserves the CSRF argument:

- `application/octet-stream` is **not** a CORS-safelisted request content type, and the
  `X-Brain-Filename` header is not safelisted either. A cross-origin `fetch` must preflight, and the
  server answers no CORS headers, so the browser never sends the real request. If a non-browser
  client skips the preflight, it still meets the Origin check.
- An HTML `<form>` cannot produce `application/octet-stream` at all.
- The exception is a **literal path and method match**, so it does not extend to `/api/ingest/*` or
  to future routes. `tests/test_ui_security.py` gains a parametrized test over **every** POST, PUT and
  DELETE route in `create_app`'s table, read from `app.routes` rather than listed by hand. It asserts
  that `application/octet-stream` gets 415 everywhere except that one route. A new route is then
  covered the moment it is added.

**Rejected:**

- **`multipart/form-data` via `python-multipart`**: C2. It is the form-sendable type, and admitting
  it reopens form CSRF behind a single check.
- **Base64 inside JSON**: it needs no middleware change, which is attractive. But the server must
  buffer the whole body and parse a ~33% larger string, then decode it. That is about 3× the file in
  memory at the peak, on a memory-constrained machine, and it cannot be capped on the stream (C7).
- **A token in the query string** for the upload: `ui/security.py:53-55`.

### 7.3 Size limits, enforced on the stream

| Input | Cap | Where enforced |
|---|---|---|
| `/api/ingest/paste`, **on the wire** | **6 × 2 MiB + 4 KiB** (12 MiB + 4 KiB) | `Content-Length` precheck (413 before reading), **then** a byte counter over `request.stream()` that aborts at the cap. The route never calls `request.body()` (C7). The multiplier is the worst case of JSON escaping: one control character becomes `\u00XX`, six bytes for one, so a legitimate body of just under 2 MiB can be up to 6× larger on the wire. Each `"` and `\` costs 2×, and a UTF-16 surrogate pair escaped as two `\uXXXX` sequences costs 12 bytes for 4 (3×). A wire cap of 2 MiB + 4 KiB would refuse legitimate pastes heavy in newlines or quotes. |
| `/api/ingest/paste`, **decoded body** | **2 MiB** of UTF-8 | After `json.loads`, the `body` string's UTF-8 length is checked, giving 413 `body_too_large`. This is the cap the user-facing limit names. |
| Upload, `.pdf` / `.docx` | 25 MiB | Same: the `Content-Length` precheck, then a counter while streaming to the staging file. The partial file is deleted on abort. |
| Upload, `.md` / `.markdown` / `.txt` | 5 MiB | Same. |
| Total staged | 64 MiB | Checked before streaming starts, from `Content-Length`. Re-checked by the counter. |

A missing `Content-Length` (chunked transfer) is allowed. The stream counter alone enforces the cap.

### 7.4 Files on disk

- **Staging root.** `tempfile.mkdtemp(prefix=f"brain-ui-ingest-{os.getpid()}-")` once per server
  process, mode `0700`, with one subdirectory per `upload_id` (uuid4 hex, server-generated). Nothing
  the client sends becomes a directory name. The **pid in the prefix** is what makes the startup
  sweep safe (Lifetime, below).
- **Filename.** `X-Brain-Filename` is percent-decoded, reduced to its final component (anything up to
  the last `/` or `\` is dropped), stripped of control characters and leading dots, and capped at 120
  characters. Its extension must be in the upload allow-list (§3.1: `UPLOAD_EXTENSIONS` ∩
  `ingest.dispatch.supported_extensions()`), else 415 `unsupported_file`. An empty or dot-only result is
  400 `bad_filename`. *(Amended by RM-13, which made the allow-list an explicit constant.)* The stored name is `<sanitized-stem><ext>` **inside** the per-upload directory.
  A test asserts `../../x.pdf`, `..\\x.pdf`, `/etc/passwd.txt`, a NUL byte and a 300-character name
  each land inside the staging root or are refused, and that nothing is written outside it.
- **Sniffing.** The client's declared type is never trusted. The first bytes must match the
  extension: `%PDF-` for `.pdf`; `PK\x03\x04` for `.docx`, whose central directory must also hold
  `word/document.xml`, checked with `zipfile` (stdlib) before extraction; for text and markdown, the
  whole file must decode as strict UTF-8 and contain no NUL. A mismatch is 415 `unsupported_file`,
  and the file is deleted.
- **Zip bombs.** `.docx` is a zip. Before `extract_docx` runs, the sum of the members'
  `file_size` must be ≤ 200 MiB and no member's ratio may exceed 100:1. Both are checked from
  `zipfile.ZipInfo`, without extracting.
- **Lifetime.** A staged file is deleted when its job reaches a terminal state, when
  `DELETE /api/ingest/uploads/{id}` is called, or after 15 minutes unsubmitted (checked on each
  upload and submit). **At startup, the sweep chooses roots by pid, not by age.** For each
  `brain-ui-ingest-<pid>-*` root in the temp directory, the sweep checks whether `<pid>` is alive
  (`os.kill(pid, 0)`; `ProcessLookupError` means it is dead). It removes the root only when the pid is
  dead and is not this process's own. A live second `brain ui` instance on another port keeps its
  root, however old its uploads are. An age gate was rejected: a long-lived instance's root is older
  than any age threshold you could pick.
  
  **Failure direction.** If the OS has reused a dead instance's pid for an unrelated process, the
  stale root survives until that pid dies too. Leftover files are left behind; live uploads are never
  deleted. A root whose name carries no parseable pid is left alone.

### 7.5 Content handling

- **Rendering.** Nothing ingested is rendered by the Ingest tab: it shows titles, counts and fixed
  strings, all through `textContent`. Opening the document goes through the Notes tab's existing
  hardened server-side renderer (`ui/render.py`).
- **The secret guard runs unchanged**, in the configured mode (`cfg.secret_guard`). The UI exposes
  **no** `allow_secrets` switch. A note that genuinely quotes a credential format is a CLI act
  (`--allow-secrets`) or uses its own frontmatter opt-out.
- **Sensitivity.** The default is `normal`, matching the CLI (`DEFAULT_SENSITIVITY`). The selector
  makes `confidential` one click away. The F6 egress veto applies through the progress embedder
  (§5.3). Because ingest is loopback-only, the job panel may show confidential titles. That is
  consistent with `serve_confidential_bodies=True` on loopback (`ui/server.py:410-415`).
- **Logging.** INFO lines carry the job id, kind, outcome and document id only, never the title or
  filename. CLAUDE.md allows titles at INFO, but a filename is often more revealing than a title, and
  ids are enough to correlate. Failures are logged with `describe_failure` (§4.5). Access logging
  stays off (`access_log=False`, `ui/server.py:547-548`).
- **CSP is unchanged.** The tab needs no new directive: `connect-src 'self'` covers fetch, and the
  file input and drag and drop need nothing extra.

---

## 8. Front end

Constraints carried over from F14 §4: no framework, no bundler, no CDN, single-responsibility ES
modules, one `innerHTML` in the whole app (`check_single_innerhtml`,
`tests/test_ui_static_behaviour.py:296`), and an offline tree.

| Path | Purpose | Est. LOC |
|---|---|---|
| `static/js/jobs.js` | Kind-agnostic job client: `submit`, `poll(id)` with backoff and visibility, `cancel`, `sessionStorage` of the ids this tab submitted. Reused by the Agent tab. | ~140 |
| `static/js/ingest.js` | The tab: mode switch (Paste / File), the form, the drop zone, rendering the job panel. | ~260 |
| `static/js/upload.js` | `uploadFile(file)`: a raw `fetch` with `Content-Type: application/octet-stream`, `X-Brain-Filename`, and the token header. It sends the `File` object directly, never `FileReader` into memory. | ~60 |
| `static/css/ingest.css` | Built on the existing tokens; no new palette. | ~150 |
| `index.html` | `#tab-ingest`'s "Deferred" panel (`index.html:158-169`) is replaced by the tab's markup: semantic `<form>`, `<fieldset>`, a `<label>` on every control, and an `aria-live="polite"` job list. | ±60 |

- **`api.js` today always sets `Content-Type: application/json` when there is a body**
  (`js/api.js:21`). `upload.js` therefore does not go through `api()`. **Today `api.js` has no token
  accessor:** `let token` is module-private (`:12`) and the module's only export is `api` (`:18`).
  Phase 3 adds and exports `getToken()` from `api.js`. `upload.js` imports it, so the token is still
  read from the fragment, held and stripped in exactly one module.
- **Packaging needs no change.** `static/js/*.js` and `static/css/*.css` are already declared, and
  `test_ui_static_assets.py::test_every_shipped_asset_matches_a_declared_glob` proves the new files
  ship.
- **`tests/test_ui_tabs_static.py`** keeps its `["notes", "ingest", "agent"]` pin, because the tab
  strip does not change. Its expectations for the deferred-panel copy move to the Agent tab only.
- **Keyboard and accessibility.** The drop zone is a real `<button>` that opens the file input, so it
  is never a drag-only affordance. Cancel is a button with `aria-describedby` pointing at the job row.
  Progress uses `<progress max=total value=done>` during the embedding phase and an indeterminate
  `<progress>` during "finishing". Every state change is announced through the live region once, not
  on every poll. A user with reduced motion gets no animated stripes.
- **Disabled states** come from `GET /api/ingest/config`. Read-only says "This server is read-only."
  Non-loopback says "Ingest is available only when `brain ui` is bound to this machine," and names
  `brain ingest`.

---

## 9. Testing

All tests follow the existing UI pattern (`tests/test_ui_*.py`). They use a real Postgres at
`postgresql://brain:brain@localhost:5434/second_brain_test`, `FakeEmbedder` (`class FakeEmbedder`,
`tests/conftest.py:927`),
a `tmp_path` vault, and `create_app(UiContext(...))` as the only injection seam. There is **no
monkey-patching of production modules** (CLAUDE.md rule 13). **No test, reviewer or auditor connects
to port 55432**, including for reads (the memory note on audits).

### 9.1 Pure tier (≥ 95%)

| File | Covers |
|---|---|
| `tests/test_ui_jobs.py` | State machine; `run_next` ordering (FIFO); `queue_full` at 5; cancel in each state (§4.4 table); `enter_commit` makes cancel return `not_cancellable`; **`enter_commit` after a requested cancel raises `JobCancelled`**, and the job ends `cancelled`. A runner that calls `registry.cancel(job_id)` and then `handle.enter_commit()` must never reach the line after it. **`cancellable` is false in every terminal snapshot**, including a runner that never called `enter_commit`; **`cancel_too_late`**: a runner that calls `registry.cancel(job_id)` on itself (202) and then returns without another `check_cancelled()` ends `succeeded` with exactly one `cancel_too_late` event; the event-cap policy of §4.2 (notable events and each phase's first event survive, `seq` is never reused, `after=` across a gap returns every surviving later event); `after=` slicing; the 200-event cap; retention by count and by age using an injected clock; snapshots are frozen and are copies (mutating a returned tuple is impossible, and a later event does not appear in an old snapshot); `stop()` with a runner blocked on a `threading.Event` returns its id after the timeout. The last case is the only one that starts a thread, and it waits on the event, never on a sleep. |
| `tests/test_ingest_outcome.py` | `classify` over every `IngestResult` shape `_ingest_outcome_verb` handles today, and the CLI's verbs re-derived from it. The existing CLI ingest tests stay green, unmodified, which proves the extraction changed nothing. |
| `tests/test_ui_upload_names.py` | The filename sanitizer table from §7.4, plus the extension allow-list: `UPLOAD_EXTENSIONS` asserted to be a **subset** of `ingest.dispatch.supported_extensions()`, and a registered extension outside it shown not to be uploadable (amended by RM-13). |

### 9.2 Integration tier (real DB, ≥ 90%)

| File | Covers |
|---|---|
| `tests/test_ui_ingest_service.py` | Paste → row, chunks and a mirror under `_ingested/manual/`; outcome `ingested`. Same paste again → `skipped` with the same id and no new rows. **Mid-embed cancel, three cases, all through the `run_next` seam on the test thread with no second thread.** In each, a fake embedder calls `registry.cancel(job_id)` from inside one of its own `embed()` calls, before returning. Each case ends `cancelled`, with no `documents`, `chunks` or `sources` row and no mirror file (C3). The fixture's chunk count is asserted in the test's setup by running `chunk_text` with the fake's `count_tokens`, never assumed. (a) **Two batches, cancel in the 1st call:** a body that chunks into exactly 20 chunks (16 + 4). Batch 2's `check_cancelled()` raises. (b) **Two batches, cancel in the 2nd (last) call:** the same 20-chunk body. No `check_cancelled()` is left, and `enter_commit()`'s atomic check raises. This is the race the check exists for. (c) **Single batch, cancel in the only call:** a body of 3 chunks, the common paste. `enter_commit()` raises. Without the atomic check, (b) and (c) would end `cancel_too_late` with a written document, so they are the mutation-proof cases for §4.4. **A cancel too late, on a no-embed path:** a `produces_embeddings = False` fake whose `count_tokens`, called by the chunker after point 3, calls `registry.cancel(job_id)`. This is a no-embed path, so no later cancel point exists, and the job ends `succeeded` with `cancel_too_late` and the document written (§4.4, the "no-embed path" row). The skip variant of that row (the mirror repair may still write) is covered in two halves: the registry's `cancel_too_late` finalization is tested in `test_ui_jobs.py`, and a skip over a row with `vault_path` set to NULL reports `mirror_repaired` here. A dedup skip calls nothing on the injected embedder, so no in-thread seam can land a cancel inside it. **A crash during the summary:** a fake enricher whose `summarize` raises `RuntimeError`. It must be an exception the hook does **not** catch: `_enrich_post_ingest_hook` catches `OllamaUnavailable` and `EnrichmentError`, logs them and returns (`ingest/__init__.py:1641-1654`), and the mirror is then written as usual. The `RuntimeError` instead escapes `ingest_document` before the mirror step. Expected end of the first job: `failed` / `internal_error`, with a **committed** row, `summary IS NULL` and `vault_path IS NULL`. The panel's message for that code must not claim nothing was written (§4.5). A re-submit is a skip that reports `mirror_repaired` and leaves `summary` NULL (§4.6). **The no-chunks path is covered in two halves,** like the skip variant. The first half is the registry's `cancel_too_late` finalization, tested in `test_ui_jobs.py`. The second half is here: an uploaded `.txt` containing only whitespace ends `succeeded` with the outcome `empty`, no `embed()` call and `enter_commit()` never called. A whitespace-only *paste* is refused at submit with 400 (§5.1), so it never reaches this path. No in-thread seam can land a cancel on it: `extract_text` strips the file to `""`, and `chunk_text` returns `[]` (`ingest/chunker.py:50-52`) before its first `count_tokens` call (`:58`), so nothing on the path touches the injected embedder. **Hosted fake + confidential** → NULL embeddings + `egress_notice` (C5). **`produces_embeddings=False` fake** → NULL embeddings, no `embed()` call. Secret guard: `warn` → masked findings; `reject` → `secret_guard_rejected` and nothing written. A whitespace-only extraction → `empty`. A mirror that fails (a read-only vault dir) → the job succeeds, and a re-submit → `mirror_repaired`. `sync_one_file` over the new mirror reaches `reconcile` on a recording `GraphSyncTarget` (§5.6 verification). |
| `tests/test_ui_ingest_upload.py` | Each extension round-trips. A sniff mismatch for each type → 415 and the file deleted. The zip-bomb guard. Stream caps: a body over the cap with an honest `Content-Length` → 413 before reading; with a lying or absent `Content-Length` → 413 mid-stream and the partial file deleted. The staging budget → 429. Expiry and `DELETE` (JSON `{}` body, 200 JSON response). The startup sweep removes a root whose pid is dead, and keeps this process's own root, a root whose pid is alive (the test uses its own parent's pid), and a root with no parseable pid. |
| `tests/test_ui_ingest_routes.py` | The §6 contract: status codes, envelopes, `ingest/config` in each mode, and `health.ingest_enabled`. |

### 9.3 Security tier (extends `tests/test_ui_security.py`)

- The parametrized Content-Type test over **every** mutating route in `app.routes` (§7.2):
  octet-stream gets 415 everywhere but `/api/ingest/uploads`, and JSON gets 415 there.
- Cross-origin, missing-Origin and `Sec-Fetch-Site: cross-site` requests on both upload and paste
  get 403. A foreign Host gets 400.
- `--read-only` → 403 on every ingest POST/DELETE. GETs are allowed.
- Non-loopback context → 403 `ingest_requires_loopback` on **every ingest mutation route** (every
  POST and DELETE under `/api/ingest/` and `/api/jobs/`), even with a valid token. The routes are
  enumerated from `app.routes`. On the same context, `GET /api/ingest/config` answers 200 with
  `reason: "requires_loopback"`, and the job GETs answer normally.
- The GET-cannot-mutate table, extended with the new routes.
- **Mutation proof.** Each new guard is mutated (the exception widened to a prefix; the loopback
  check removed; the `hosted_egress` forward dropped) in a `git archive` copy, never the live
  worktree, and the matching test must go red. Per the memory note "no mutations during a run of
  record".

### 9.4 Browser tier (playwright, CI job "UI browser harness")

`tests/test_ui_browser_ingest.py`: paste → the panel shows progress → "Added" → the link opens the
note in Notes. Paste again → "Already in your brain". Upload a small `.md` through the file input.
Cancel a job held by a gated fake embedder. This is the one cancel test that uses the real worker
thread: the browser clicks Cancel while the fake blocks inside `embed()`, then the test releases it.
The read-only server renders the disabled reason.
Keyboard only, both themes. **The new module must be added to the explicit, byte-ordered path list
in `.github/workflows/ci.yml`** ("Run the browser harness"). `tests/test_ci_workflow.py` discovers
the roster by marker and fails if it is missing, which is the intended trip-wire.

### 9.5 What is deliberately not tested

Real Ollama and real PDF corpora are out of CI, as for the rest of the suite. A **manual** check
against the `brain demo` sandbox (port 55433, synthetic corpus, throwaway vault) is part of each
phase's exit. Never against the live vault: see the memory note "no real data in screenshots".

---

## 10. Phased implementation

Each phase is one PR. Each ends green on `ruff check && mypy src/ && pytest` and on the CI browser
job, and each runs the rule-14 review plus audit loop until both pass clean.

| Phase | PR contents | Exit criterion | Est. size |
|---|---|---|---|
| **1. Jobs + paste backend** | `ui/jobs.py`; `ui/ingest_limits.py`; `ui/ingest_progress.py`; `ui/ingest_service.py` (paste only); `ui/routes_jobs.py`; `ui/routes_ingest.py` (`config`, `paste`); the lifespan in `app.py`; the `UiContext.jobs` / `ingest_enabled` fields; `brain/ingest/outcome.py`, with `cli_ingest` switched to it; the new `UiError`s; the mutation-confinement docstring amendments (§1.3). Tests §9.1, §9.2 (paste rows), §9.3 (paste rows). | Paste works end to end over HTTP against the test DB. All of C3, C5 and §5.6 are verified by tests. | ~900 src / ~1,100 tests |
| **2. Paste front end** | `jobs.js`, `ingest.js` (paste mode), `ingest.css`, the `index.html` panel, the `tests/test_ui_tabs_static.py` copy update, the browser tests (paste rows), the `ci.yml` roster entry. | §3.4 items 1, 2, 4, 5 and 6 hold for paste. Manual pass on the demo sandbox. | ~450 src / ~300 tests |
| **3. File upload** | The middleware exception (§7.2); upload staging, sniffing and sweep; `getToken()` added to and exported from `api.js`; `upload.js` plus file mode in `ingest.js`; tests §9.2 (upload) and §9.3 (Content-Type parametrized), browser upload rows. **Its own security review**, dispatched with `ecc:security-reviewer` on opus, in addition to the rule-14 loop. | §3.4 item 3. The parametrized Content-Type test proves the exception is exactly one route. | ~600 src / ~700 tests |
| **4. Optional: inbox folder** | §5.5, only if asked for. | — | — |
| **After** | Docs: `docs/cli-reference.md` (the `brain ui` section), README, and `docs/guides/brain-ui-on-your-phone.md` (ingest is unavailable over LAN). Auto-memory: `cli.md` for the endpoints and `types.md` for `JobSnapshot`, `JobRegistry`, `IngestOutcome` and `ProgressEmbedder` (CLAUDE.md rule 11). | — | — |

Phase 1 is first because it carries every correctness property (C3, C4, C5) and every one of them is
testable without a browser. Phase 3 is separate because it is the only phase that changes the
security middleware.

**Ceiling check.** Every new module is planned under 400 lines. Existing files touched: `ui/app.py`
(199 lines), `ui/security.py` (243), `ui/context.py` (427), `ui/server.py` (572), `ui/errors.py`
(110), `ui/static/index.html` (218) and `cli_ingest.py` (870, shrinks). None of these crosses 800,
and only `cli_ingest.py` is in the ceiling table. **No change to `ingest/__init__.py`,
`config.py`, `cli.py` or `mcp_server.py`.**

---

## 11. Risks

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| R1 | A future edit drops the `hosted_egress` forward from the progress embedder, and confidential text reaches a hosted embedder. | Low, catastrophic | The C5 tests, mutation-proven. A comment at the forward naming `is_hosted_embedder`'s default. |
| R2 | A future change moves the embed inside the write transaction (C3), so a mid-embed cancel rolls back instead of never writing. That is still safe, but not what the docs say. | Low | The "cancel writes nothing" test passes either way. The docstring of `ProgressEmbedder.embed` names C3 so the assumption is visible. |
| R3 | UI search gets slower during an ingest, because the embedding model is shared. | Certain, mild | Batch size 16 lets searches interleave. The job panel says "ingesting: search may be slower". |
| R4 | A file ingested by `brain ingest <path>` and again by upload becomes two documents (§5.2). | Medium | Documented in the tab's help text. It is the strongest argument for backlog item 3.4 (near-duplicate detection), and that item names this. |
| R5 | A concurrent CLI ingest of the same content races the job. | Low | `concurrent_ingest` maps the `UniqueViolation` (§4.5), and a retry is safe. |
| R6 | Memory spike parsing a large PDF (pdfplumber). | Medium | The 25 MiB cap, one running job, and staging on disk rather than in memory. If it is still too much, lower the PDF cap first. It is a constant. |
| R7 | The polling list grows unbounded in a long-open tab. | Low | Server retention (50 jobs / 1 h). The client renders the 20 most recent. |
| R8 | The watcher is not running, so the new document never reaches the graph. | Medium | The panel says so and names the command (§5.6). `brain doctor` already warns that the watcher is down (`doctor_watcher.py`). |
| R9 | The upload route becomes a precedent for "just one more" non-JSON route. | Medium | The parametrized test makes each exception an explicit, reviewed line, and the §1.3 amendment wording names the two mutators. |

---

## 12. Open questions, each with a recommended default

| # | Question | Recommended default |
|---|---|---|
| Q1 | Allow ingest over a `--lan` / `--host` bind (capture from a phone)? | **No, in v1.** Revisit when there is a concrete phone workflow. Then allow **paste only** over LAN (bounded at 2 MiB, no staging), never upload. Phone capture today is `+ New` over `--lan`. |
| Q2 | Should enrichment default to on? | **On**, matching the CLI (`--no-enrich` is opt-out there), with a "skip summary (faster)" checkbox. A failed summary never fails the job, and `brain enrich --backfill` repairs it. |
| Q3 | Should the job sync the graph itself? | **No** (D9). Rely on the watcher's lane and say so. Revisit only if the watcher stops being the norm. |
| Q4 | Persist jobs across restarts? | **No** (D4). Every outcome is recoverable by re-submitting, and a table would duplicate confidential titles. |
| Q5 | Default sensitivity? | **`normal`**, matching the CLI, with `confidential` one click away. A per-tab "remember my last choice" in `localStorage` is fine as a convenience, wrapped in `try`. |
| Q6 | Upload caps? | **25 MiB** PDF/DOCX, **5 MiB** text, **2 MiB** paste, **64 MiB** total staged. Constants, not knobs. |
| Q7 | Expose `force` re-ingest? | **No.** On this path it mints a new UUID and breaks links (§3.2). |
| Q8 | Offer source kinds other than `manual` (Slack, Krisp) for paste? | **No.** They imply an external id and, for Krisp, a `gws` refresh. That workflow belongs to the agent-orchestrated `ingest-stdin`. |
| Q9 | Show CLI or MCP ingests in the job panel? | **No.** The registry is per process. A "recently ingested" view already exists (`GET /api/recent`). |
| Q10 | URL ingest? | **No** (§3.2). Reconsider only with an HTML extractor and an SSRF design of its own. |
| Q11 | Promote `python-multipart` anyway, for a nicer form? | **No** (D7). It changes the CSRF argument for no user-visible gain. |
| Q12 | Agent tab transport? | **Polling first, then the NDJSON `fetch` stream** (§4.3). Never `EventSource` while the token is a header. |
| Q13 | Should one worker serve ingest *and* ask? | **Yes** (§4.7). Revisit only on a machine with headroom for two Ollama models plus parsing. |
| Q14 | Should the inbox folder (§5.5) be built? | **Not unless asked.** Upload covers the need. |

---

## 13. Rejected alternatives (summary)

| Alternative | Why not | Where |
|---|---|---|
| Run ingest inside the request handler | It blocks the event loop and every other request (§1.2). It is also the "spinner that lies". | §4.1 |
| Starlette `BackgroundTask` | Untracked and uncancellable, and it delays shutdown (C8). | §4.1 |
| `asyncio` task / `run_in_threadpool` per job | The task blocks the loop. The threadpool call shares anyio's global 40-thread limiter, and the job would be untracked and uncancellable. | §4.1 |
| Subprocess running `brain ingest` | The F14 rule: "never shells out to the CLI". It also cannot report progress or be cancelled cleanly. | F14 §9.1 |
| SSE via `EventSource` | Cannot send the token header. | §4.3 |
| Multipart upload | Reopens form CSRF behind a single check (C2). | §7.2 |
| Base64 in JSON | ~3× memory at the peak, and no stream cap. | §7.2 |
| A persistent jobs table | Needs a migration, duplicates confidential titles, and adds no recoverable outcome. | §4.6 |
| Ingest inside `notes_service.py` | It would cross the 800-line ceiling, and it is a different responsibility. | §1.3 |
| Changing `ingest_document` to take a progress callback | Not needed: the embedder seam gives progress and cancel without touching an over-ceiling 2,369-line module. | §5.3 |
| An explicit pre-write cancel hook inside `ingest_document` | It would change the same over-ceiling module for a window measured in milliseconds on the four no-embed paths. `cancel_too_late` reports that window honestly. On embedding paths no hook is needed: the atomic `enter_commit()` is the last check, and it runs before the transaction. | §4.4 |
| A plain `enter_commit()` that only closes the window | A cancel landing during the last or only batch would get 202 and then be ignored, with the document written. That is the common case for a paste. The check-and-close must be one critical section. | §4.4, §5.3 |
| Calling `enter_commit()` before `ingest_document` | It closes the cancel window before the embed, which makes the only long cancellable phase uncancellable. | §4.4, §5.3 |
