# CLI reference

> Part of the [Second Brain](../README.md) docs — see [docs/README.md](README.md) for the full index. The [README](../README.md#core-usage) shows the everyday `ingest` / `search` / `show` / `list` / `tag` commands; this file covers everything else — first-run `setup` and the offline `demo`, the advanced flags and diagnostics for those core commands, plus the full command surface beyond them.

## brain setup

`brain setup` is the one-command installer for the runtime: it creates
`$BRAIN_HOME`, installs the `brain` shims, starts the Postgres container via
Docker, and optionally wires the wiki (Caddy + Quartz) and the Claude Code
skill. How much it stands up is chosen with `--profile` — `minimal` (Postgres +
FTS only, `BRAIN_EMBEDDER=none`), `standard` (default; adds Ollama hybrid
search), or `full` (adds GraphRAG, the wiki, and opt-in daemons). The
[README profiles table](../README.md#quick-start) is the canonical description
of what each profile pulls in.

```bash
brain setup                              # standard profile, interactive prompts
brain setup --profile minimal            # Postgres + FTS only (no Ollama, no models)
brain setup --profile full --daemons     # + graph/wiki + launchd background daemons
brain setup --non-interactive            # take the default for every prompt
brain setup --dry-run                    # print planned actions; touch nothing
brain setup --embedder none              # non-interactive backend: arctic|voyage|qwen3|none
brain setup --reset                      # destructive wipe of $BRAIN_HOME (typed confirmation)
```

`--daemons` installs the launchd background agents and applies only to the
`full` profile (default `--no-daemons`). `--embedder` picks the backend
non-interactively — `arctic` (default hybrid), `voyage` (SaaS), `qwen3`, or
`none` (FTS-only, the `minimal` default). `--non-interactive` uses defaults for
every prompt; `--dry-run` prints every planned action without touching the
filesystem. `--reset` requires a typed confirmation that cannot be bypassed even
with `--non-interactive`.

To stand up a second, isolated stack (QA, a throwaway `$BRAIN_HOME`) alongside
the real one, export `BRAIN_COMPOSE_PROJECT=<name>` — every `docker compose`
call and the rendered `container_name` switch to that project so it never
collides with the default `brain` stack.

## brain demo

`brain demo` is a zero-Ollama taste test: it provisions a throwaway Postgres
sandbox (its own compose project `brain-demo`, container, and volume — never the
prod stack), seeds a 22-doc synthetic *Larkspur* compliance corpus with a
deterministic offline embedder, and runs a hero query inline — ranked results in
under two minutes with no personal data and no model downloads. It needs only
Docker, or an existing empty database via `--database-url`.

```bash
brain demo                               # provision + seed + run the hero query
brain demo --with-embeddings             # also build the vector leg + HNSW index (default: FTS-only)
brain demo --port 55444                  # host port for the sandbox (auto-bumps if busy; default 55433)
brain demo --database-url postgresql://…  # seed an existing empty DB instead of Docker
brain demo --json                        # emit the hero-query results as JSON

brain demo query "PCI scope creep"              # search the running sandbox
brain demo query "vendor risk" --source gmail   # filters: --source/--tag/--person/--after
brain demo status                        # is the sandbox up, and how many docs?
brain demo teardown                      # destroy the sandbox (docker compose down -v)
```

The default flow is FTS-only (no Ollama); pass `--with-embeddings` to exercise
the vector leg too. `query` accepts the same corpus filters as real search
(`--source`, `--tag`, `--person`, `--after`) plus `--json`. `--port` and
`--database-url` are shared by the sub-commands so they target the same sandbox.
`teardown` removes both the container and its volume, leaving no demo state
behind.

## Ingest options

```bash
# Single files: TXT, Markdown, PDF, DOCX.
brain ingest ~/Documents/resume.pdf --tag career --tag resume
brain ingest ./notes.md --force       # re-ingest even if the content already exists

# Directories: recursive, idempotent by content hash.
brain ingest-dir ~/Documents/career
brain ingest-dir ~/Documents/career --tag career --ext pdf,md,docx
brain ingest-dir ~/Documents/career --dry-run

# Piped content: used for Krisp / Slack / arbitrary text from agents.
echo "<transcript>" | brain ingest-stdin \
  --source krisp \
  --external-id meeting-42 \
  --title "1:1 - 2026-04-24" \
  --content-type transcript \
  --date 2026-04-24 \
  --tag one-on-one \
  --metadata '{"participants":["Alice","Bob"],"duration_min":30}'
```

All ingest paths write rows + chunks to Postgres and, when `BRAIN_VAULT_PATH`
exists, mirror ingested documents into `_ingested/<source>/` so the wiki can
show them. Re-running unchanged content is a no-op unless you pass `--force`.

## Search diagnostics and filters

```bash
brain search "exact product name" --fts-only   # skip the embedding call

# Rank diagnostics: why did these results surface, and in what order?
brain explain "platform migration tradeoffs"
brain explain "platform migration tradeoffs" --verbose --json
```

ID prefixes must be at least 6 hex characters and must uniquely identify one
document. `--json` is the machine-readable path for agents or scripts.

`brain explain` runs the same query as `brain search` but prints per-result
ranking diagnostics — FTS rank, vector cosine, RRF contributions, and the
recency boost — so you can see *why* a result surfaced and in what order. Add
`--verbose` to show which filters were active, or `--json` for the full
`SearchExplanation` payload. It accepts the same filter flags as `search`
(`--source`, `--tag`, `--since`, `--person`, `--after`/`--before`, `--kind`, …).

`--person` resolves the name or email you pass against the directory and
matches docs recorded under **any** known variant of that person — space-form
(`jane doe`), dot-form (`jane.doe`), bare email, and every `Name <email>`
combination the sources emitted — so Gmail-header and Krisp-label spellings of
the same person all count as one identity.

`--since` takes a bare number or a duration suffix — `7d` (days), `24h`
(hours), `90m` (minutes) — on the commands that share this parser: `search`,
`explain`, `todo`, `enrich`, and `gaps` (bare number = **days**), plus `brain
brief` (`--since` bare = **hours**; its `--todo-since` bare = days). Bare
numbers keep each command's existing unit, so old scripts are unaffected. The
remaining `--since` flags are unchanged and do **not** take suffixes:
`ingest-gmail` expects a Gmail `YYYY/MM/DD` date, `timeline` an ISO `YYYY-MM`
month, and `gaps push` a plain integer day count.

### Search transparency and facets

```bash
brain search "platform migration"                 # footer on stderr by default
brain search "platform migration" --no-meta       # suppress the footer
brain search "platform migration" --facets        # add a facet panel
brain search "platform migration" --json          # bare list — UNCHANGED shape
brain search "platform migration" --json --meta   # envelope with counts + timings
```

The footer (`544 matched · 3 shown · embed 5820ms · sql 214ms`) goes to
**stderr**, so `--json` stays pipeable and `> file` stays clean. `--meta` moves
the same numbers into a JSON envelope; **`--json` on its own is still a bare
list of 7-key objects and always will be**, because the list is consumed
positionally by skills and shell scripts and there is no deprecation channel
for a personal tool. `--facets --json` implies `--meta`, since facets have
nowhere else to live.

`total_documents` is **lexical-only** by construction: the vector leg may
surface near-neighbours it does not count. A vector-inclusive total would be
capped at the candidate limit and therefore meaningless as a "total".

### Filtering by when a document changed

```bash
brain search "runway" --updated-after 2026-07-01
brain search "runway" --updated-before 2026-07-31
```

Distinct from `--after` / `--before`, which filter on when a document was
**authored or received** (`coalesce(sent_at, ingested_at)`). These filter on
when it was last **changed**. Maintenance jobs — enrichment backfills, tag
normalization, `vault_path` bookkeeping — deliberately do *not* bump
`updated_at`, so a `brain enrich --backfill` across the corpus will not make
every document look edited today.

## brain recall

Retrieval sized for an agent's context window. Where `brain search` ranks
pointers for a human to read, `recall` returns the **material itself**, packed
to an explicit token budget and cited.

```bash
brain recall "platform migration runway"
brain recall "platform migration runway" --budget 800
brain recall "platform migration runway" --json
brain recall "platform migration runway" --agent research-agent
```

| Flag | Default | Meaning |
|---|---|---|
| `--budget`, `-b` | `BRAIN_RECALL_BUDGET_TOKENS` (2000) | Token budget for the **whole** emitted block, header included. |
| `--max-candidates` | `BRAIN_RECALL_MAX_CANDIDATES` (25) | Documents considered before packing. |
| `--json` | off | Machine-readable projection instead of the block. |
| `--agent` | `BRAIN_AGENT_ID` | Attribute this recall to an agent id. |

It accepts the same metadata filters as `search` (`--source`, `--tag`,
`--since`, `--person`, `--after`/`--before`, `--kind`, `--thread`,
`--without-tag`, `--fts-only`).

Output is a `# recall:` header followed by `[N]`-cited passages, one per
document — the same citation convention `brain ask` uses, so an agent that
pastes a block and cites `[2]` is speaking a vocabulary the rest of the system
understands. One passage per document keeps source diversity high, which is
what a limited budget most wants.

If the budget cannot hold even the top passage, you get **one truncated
passage** rather than nothing: an agent asked for context, and silence is a
worse answer than a shortened excerpt.

## brain usage

```bash
brain usage --days 30
brain usage --json                  # normalized query labels
brain usage --json --raw-queries    # opt in to the raw strings
brain usage --limit 20
```

Searches, opens, feedback events and ingests over the trailing window, broken
down by day, by surface (`cli` / `mcp` / `wiki`) and by agent, plus
search-latency p50/p95.

**`--json` withholds raw query strings by default.** A query log records what
you were *looking for*, including the searches that found nothing — often more
revealing than any document it returned. Counts are identical either way; only
the label changes. Human output is deliberately **not** redacted: the terminal
is inside the trust boundary, and "what do I search for most" is unactionable
if you are shown normalized labels.

A row with no `agent_id` reports as `(unattributed)`, never folded into a
surface. Every row written before migration 027 is genuinely unattributed, and
saying so is more useful than guessing.

On a database missing migration 019/023/024/027 the command **fails** with a
`brain init` hint rather than reporting confident zeroes — the opposite of the
telemetry *write* path, which swallows so that search keeps working. A silently
incomplete report is worse than no report.

## Confidentiality

```bash
brain mark-confidential <id-prefix>
brain mark-normal <id-prefix>
brain list --sensitivity confidential
brain ingest notes.md --sensitivity confidential
```

`confidential` is an **egress** control, not an access control. The document
stays fully readable from the local CLI — that is inside the trust boundary.
What changes is what leaves the machine: the body is kept off a hosted
embedder, withheld from MCP `brain_show` / `brain_search` / `brain_recall` /
`brain_resurface` unless `include_confidential=true`, and dropped from the
published wiki index.

Both commands are idempotent. **`mark-normal` is the only sanctioned
downgrade**: re-ingest is escalate-only, so it can raise a document's tier but
never lower it — otherwise a background `vault sync --watch` pass would quietly
reset anything you had marked. That also means marking a document confidential
writes the tier into its frontmatter for vault-tier notes, because for those
the file is the source of truth.

An unrecognized `--sensitivity` value is a **usage error (exit 2)**, never a
silent empty list. For a confidentiality filter, "nothing is marked
confidential" is the most dangerous possible wrong answer. The same applies to
`brain search --sensitivity` and `brain list --sensitivity`.

### `--sensitivity` on `search` is a lens, not a filter you can rely on

```bash
brain search "roadmap" --sensitivity confidential   # only confidential hits
brain search "roadmap"                              # BOTH tiers — the default
```

`brain search` accepts `--sensitivity`, but **there is no sensitivity filter on
the ranked results by default**: an unfiltered `brain search` still returns
confidential documents and their bodies. That is deliberate — the local CLI is
inside the trust boundary, and the tier governs *egress* (hosted embedders, MCP
responses, the published wiki), not local reads.

The practical consequence: `--sensitivity normal` is a convenience lens for
narrowing what you are looking at, **not** a way to make a local session safe to
screen-share. If you need output that provably excludes confidential bodies, the
MCP surfaces are where that guarantee lives.

## Agent attribution

```bash
brain search "x" --agent research-agent
brain recall "x" --agent research-agent
brain rate <id> useful --agent research-agent
brain ingest-stdin --source slack --external-id t1 --title T --agent capture-bot
BRAIN_AGENT_ID=research-agent brain search "x"     # ambient, no flag
```

Precedence is **flag > `BRAIN_AGENT_ID` > unattributed**. `--agent` exists on
the four agent-facing surfaces above; `brain ingest` / `ingest-dir` have **no**
`--agent` flag, because attaching an explicit agent to a hand-run ingest would
be a fabricated fact. The ambient env var does still attribute them — see the
warning in [configuration.md](configuration.md#brain_agent_id-who-is-doing-the-work)
before exporting it in a shell profile.

On `ingest-stdin` the flag **wins** over an `agent_id` inside `--metadata`: an
explicit flag is the more specific and more recent statement of intent.

## Tags, edits, draft hiding, and deletes

```bash
# Tags accept +name and -name modifiers. Existing vault mirrors are rewritten
# so frontmatter stays aligned with the DB.
brain tag <id-prefix> +interview +career -old-tag
brain tag <id-prefix> +career --regenerate-file   # recover a missing ingested mirror

# Ingested-tier docs: targeted updates or JSON-header editor flow.
brain edit <id-prefix> --title "New title"
brain edit <id-prefix> --metadata '{"date":"2026-04-26"}'   # shallow merge
brain edit <id-prefix> --metadata '{"date":"2026-04-26"}' --replace-metadata
brain edit <id-prefix> --content-file ./fixed.md            # re-chunks + re-embeds
cat ./fixed.md | brain edit <id-prefix> --content-stdin
brain edit <id-prefix>                                      # opens $EDITOR

# Vault-tier docs: the file is source of truth. `brain edit <id>` opens the
# Markdown file directly; mutating flags are rejected for vault docs.
brain edit <vault-id-prefix>

# Hide a doc from the rendered wiki without removing it from local CLI search.
brain mark-draft <id-prefix>
brain mark-published <id-prefix>

# Delete a document and its chunks. If a vault mirror exists, it is unlinked too.
brain rm <id-prefix>
brain rm <id-prefix> --yes
```

## Gmail ingest

`brain ingest-gmail` uses the `gws` CLI and requires at least one scope flag so
you do not accidentally ingest your entire mailbox. It excludes Gmail drafts,
groups messages by `threadId`, stores each thread as one `email_thread`
document, and updates a stable row when a thread grows.

```bash
brain ingest-gmail --label interviews --since 2026/04/01 --tag interview
brain ingest-gmail --from alice@example.com --max 25
brain ingest-gmail --query 'from:recruiting@example.com newer_than:30d' --dry-run
brain ingest-gmail --until 2026/05/01 --tag archive
```

Dates passed to `--since` / `--until` are converted into Gmail `after:` /
`before:` query terms, so use Gmail's `YYYY/MM/DD` shape. Raw `--query` is
passed through and can use any Gmail search syntax.

## Status and health

```bash
brain status          # counts and last-ingest time
brain status --json   # machine-readable: {documents, chunks, sources, by_source, last_ingest}
brain doctor          # env, Postgres/pgvector, embedder, gws, npx, mirror drift, AGE + graph health
brain doctor --json   # machine-readable [{check, status, detail, remedy}]; still exits non-zero on FAIL

# Refresh Postgres planner statistics. Fixes the "chunks stats — never analyzed"
# warning doctor reports after a pg_restore (a restore bulk-loads rows via COPY
# but never runs ANALYZE, so the planner uses bad row estimates until autovacuum).
brain analyze         # ANALYZE the chunks table (the one doctor warns about)
brain analyze --all   # ANALYZE every table in the database
```

`brain doctor` exits non-zero only for required failures: config, database, or
active embedder. Optional integrations (`gws`, `npx`, the AGE extension, the
concept-extractor model, community materialization staleness) are warnings.
`brain status --json` and `brain doctor --json` are the scripting-friendly
paths; `doctor --json` preserves the same non-zero exit on any FAIL check.

## brain capture

```bash
# Jot a thought straight into the brain — always tagged `inbox`.
brain capture --text "Follow up with the platform team about the migration cutover"
echo "Idea: batch the nightly re-embed to cut Ollama warmups" | brain capture --title "re-embed idea" -t ideas

# Review the inbox later: promote, tag, or discard each item; or just list it.
brain capture list
brain capture list --json     # machine-readable; the safe surface for an agent
brain capture review
brain capture review --auto   # non-interactive: LLM-routes items out by tag
```

`brain capture` is a zero-friction inbox: pipe text on stdin or pass `--text`,
optionally adding `-t/--tag` alongside the always-on `inbox` tag. `brain capture
list` shows what's waiting; `brain capture review` walks each item so you can
promote it into a real note, retag, or discard it.

`capture review` is interactive and its discard path deletes a document, so it
is deliberately not something an agent should drive unattended — the
[`brain-proactivity` skill](agent-skills.md) covers it with that prohibition
attached.

## brain claude install-hooks

```bash
brain claude install-hooks                 # install the session-end capture hook
brain claude install-hooks --dry-run       # show what would change, write nothing
brain claude install-hooks --target ~/.claude-alt   # non-default config root
brain claude install-hooks --force         # overwrite a differing hook script
brain claude install-hooks --uninstall     # remove the hook entry and script
```

Installs a Claude Code **Stop** hook that nudges exactly one dedupe-then-capture
pass after a session that did real work and wrote nothing back to the brain. It
writes `<target>/hooks/brain-capture-hook.sh` and merges an entry into
`<target>/settings.json`; `--target` moves both (default `~/.claude`).

Opt-in and separately reversible: `--uninstall` removes the entry *and* the
script. `--force` overwrites a hook script that differs from the shipped one,
but it **never** bypasses the refusal to touch a malformed `settings.json` —
merging into a file we cannot parse risks destroying unrelated configuration,
so that refusal has no override.

Run `--dry-run` first if you have hand-edited your Claude Code settings.

## brain backfill scan-secrets

```bash
brain backfill scan-secrets                                  # READ-ONLY report
brain backfill scan-secrets --json                           # same, machine-readable
brain backfill scan-secrets --limit 200                      # stop after N documents
brain backfill scan-secrets --apply --action mark-confidential
brain backfill scan-secrets --apply --action redact          # slow; rewrites bodies
```

Scans every stored document for credential-shaped strings — the retroactive half
of the `F4` ingest guard, which only protects documents from the moment it
shipped.

**Read-only by default, behind two independent gates.** `--apply` is required to
write anything, *and* the default `--action report` cannot write even when
`--apply` is passed. Both exist because the destructive action rewrites document
bodies across the whole corpus. To actually change anything you must name a
non-`report` action **and** pass `--apply`; either alone is a no-op report.

| `--action` | With `--apply` |
|---|---|
| `report` (default) | never writes, regardless of `--apply` |
| `mark-confidential` | raises the tier on each hit |
| `redact` | rewrites bodies — re-chunks, re-embeds, re-hashes, regenerates vault mirrors |

`redact` is by far the slowest option for that reason. An unrecognized
`--action` is a usage error (**exit 2**).

One limit worth stating plainly: this finds what is *already stored*. It cannot
un-send anything that was already transmitted to a hosted embedder before the
scan ran.

## brain enrich

```bash
# Backfill LLM auto-summaries for documents that don't have one yet (idempotent).
brain enrich --backfill
brain enrich --backfill --limit 50
brain enrich --backfill --remodel        # also re-summarize rows whose summary_model is stale

# Print the Krisp MCP request Claude should run to pull action items (the CLI
# never calls MCP itself — Claude pipes results back via ingest-stdin).
brain enrich --krisp-action-items --since 7
```

`brain enrich` has two mutually-exclusive modes. `--backfill` runs the Ollama
enricher over rows where `summary IS NULL` (add `--remodel` to also refresh
summaries written by an older `BRAIN_ENRICH_MODEL`; honors `--limit/-n`).
`--krisp-action-items` prints the MCP + `ingest-stdin` commands for Claude to
execute, then exits without contacting MCP itself.

## brain todo

```bash
brain todo                          # every open action item
brain todo --since 30               # from action-item docs ingested in the last 30 days
brain todo --closed                 # include checked-off items
brain todo --limit 100 --json
```

`brain todo` reads the `content_type='krisp_action_items'` documents that
`brain enrich --krisp-action-items` produces (stored separately from the meeting
transcript), parses `- [ ]` / `- [x]` checklist lines out of each body, and
prints one row per item — so you see what you owe without opening N transcripts.
Default is open items only.

`--since` takes a bare number as **days** (suffixes `7d` / `24h` / `90m` also
work) and filters on `documents.ingested_at`, not the meeting date: a call from
six months ago that you ingested yesterday still shows under `--since 7`.
`--source` accepts only `krisp` today and exists for a future Slack/Gmail
extension. `--json` emits a flat list of `{document_id, document_title,
ingested_at, state, text}`.

There is no `brain todo close`. To check an item off, edit the source
action-item document (`brain edit <id-prefix>`) and change `- [ ]` to `- [x]`;
the next run reflects it. Agents drive this through the `brain-todo`
[skill](agent-skills.md).

## brain rate

```bash
# Thumbs up / down a document (verdict is `useful` or `irrelevant`).
brain rate 3f9a2b useful
brain rate 3f9a2b irrelevant

# Rate a graph target instead of a document, and/or mark it as graph-surfaced.
brain rate <entity-uuid> useful --target-type entity
brain rate <community-key> useful --graph-retrieved
```

`brain rate` records relevance feedback into the `interactions` table. Ratings
append — re-rating a target creates a fresh row and the full history is
preserved. Pass `--target-type entity|community|theme` to rate a GraphRAG
target by its durable id instead of a document; `--graph-retrieved` marks the
rating as produced by a graph surface.

## brain note move

```bash
brain note move <id-prefix> projects/atlas --dry-run
brain note move <id-prefix> projects/atlas
brain note move <id-prefix> "" --yes            # "" or "." = vault root
brain note move <id-prefix> archive --no-link-refactor
```

Relocates a vault note to another folder, keeping its title **and its
document id** — which is why incoming backlinks survive. A move is a rename to
the same title in a different folder, so it reuses the whole rename machinery:
the plan phase scans the vault for path-form `[[…]]` references, and the apply
phase snapshots every file it touches and restores them on any failure.

`NEW-FOLDER` is vault-root-relative and created if missing. A leading slash is
stripped, so `/projects/atlas` means `projects/atlas` — never the filesystem
root. Paths that escape the vault are rejected (exit 2).

**Safe to run with `brain vault sync --watch` and `brain-mcp` live.** The file
is relocated with an atomic rename (same inode) and `documents.vault_path` is
repointed before the follow-up sync, so the watcher observes a *move* rather
than a delete-then-create. That distinction matters: the delete branch would
cascade every incoming link away.

There is **no `--force`**. A note already at the destination is a hard error,
because silently clobbering one is unrecoverable. Moving a note into the folder
it already occupies prints `already in <folder> — nothing to do` and exits 0.

Confirmed by default (a move rewrites links across the whole vault and you
cannot see that blast radius from the command line); `--yes` skips the prompt,
`--dry-run` skips it too since nothing will be written.

## Maintenance odds and ends

```bash
brain reembed                       # backfill NULL chunk embeddings
brain orphans                       # vault notes with no links either way
brain orphans --all                 # include ingested-tier mirrors
brain uninstall                     # remove runtime state + daemons
```

`brain reembed` backfills `chunks.embedding` for rows that have none. After
`brain init` every chunk starts NULL, so this is the second half of first-run
setup. Idempotent — only still-NULL rows are touched, so it is safe to re-run
after a crash. It also finalizes the column (`NOT NULL`, plus an HNSW index
when the active backend's dimension allows one).

`brain orphans` lists documents with **zero** incoming and zero outgoing links.
It defaults to vault-tier only: ingested-tier mirrors legitimately have no
`[[…]]` links of their own, so including them buries the signal. `--all` opts
them in.

`brain uninstall` removes launchd plists, stops the Docker compose stack and
deletes the `$BRAIN_HOME` runtime files (`.env`, `.shims/`, `Caddyfile`).
**Your database and vault are kept by default** — read its `--help` before
passing anything that widens the blast radius.

## brain backup / brain restore

```bash
brain backup                        # DB + vault, into BRAIN_BACKUP_DIR
brain backup --no-vault --label pre-migration
brain backup --json                 # manifest as JSON
brain restore <archive>             # y/N confirmation
brain restore <archive> --db-only --yes
```

`pg_dump` runs **inside the container** by default. This is not a preference:
the production Postgres is 16.x while a typical host Homebrew `pg_dump` is
14.x, and the version mismatch makes a host-side dump abort outright. A host
binary is used only after an explicit major-version check.

`--yes` skips the y/N prompt but **never** skips the pre-flight compatibility
checks — those are what stop a restore from half-applying.

## brain ui

```bash
brain ui                            # loopback only, opens a browser
brain ui --read-only
brain ui --host 0.0.0.0 --token <secret>
```

A local single-user web surface over the same corpus. Adds **no** runtime
dependency — `starlette` and `uvicorn` already ship as transitive deps of the
MCP SDK — and the front end is hand-written static assets with no bundler and
no CDN, so it works fully offline.

`--token` is **required** whenever `--host` is not loopback.

Confidential bodies follow the bind, not a flag. On a **loopback** bind they are
always served — the UI is exactly as inside the trust boundary as `brain show`
is — and `--include-confidential` is ignored. On a **non-loopback** bind they
are withheld unless you pass `--include-confidential`, since the material is
then crossing the wire. The startup banner states which of the two you got.

## brain people

```bash
brain people                 # alphabetised People Hub roster (everyone above the doc threshold)
brain people "Jane"          # one person's full document list (case-insensitive substring)
brain people --json          # machine-readable aggregation
```

`brain people` is a read-only view of the same People Hub aggregation that
renders the `<vault>/people/` pages and drives the derived-link participant
filter. The visibility threshold (`BRAIN_PEOPLE_HUB_MIN_DOCS`, default 3) and
owner filter (`BRAIN_OWNER_PARTICIPANTS`) flow through the normal config, so
flipping either env var changes the roster on the next invocation.

## Tacit-knowledge elicitation

### How it works

`brain elicit` is the part that **asks**. The brain already indexes what you
have written and said; this feature mines that corpus for knowledge that is
*implied everywhere but written nowhere* — entities referenced constantly in
transcripts, decisions never captured as a note, positions that shifted across
time. It drafts a confident guess at the unwritten rule, opens your `$EDITOR`
so you can correct it, and codifies the result as a vault note (tagged `tacit`
+ the signal kind) with a `## Source` section wikilinking the evidence docs.
Codified gaps are marked resolved and never resurface.

Four gap signals feed the queue:

| Signal | What it detects |
|---|---|
| `delta` | Entities heavily referenced in ingested docs (transcripts, Slack, mail) but never authored into a vault note. The cleanest tacit-knowledge proxy. |
| `orphan` | Graph entities with high mention-count but no written description. The graph knows *who* matters; this surfaces *why* it doesn't. |
| `contradiction` | Opposing positions across docs — flag-gated; requires Ollama + non-null summaries (`BRAIN_ELICIT_CONTRADICTION_ENABLED=true`). |
| `user_flagged` | A topic you name with `--target`; always enters the queue regardless of corpus signals. |

The interactive loop (`brain elicit`) requires **Ollama** (used to draft the
rule text). Viewing the gap queue (`brain elicit list`) is read-only and
Ollama-free (unless `--signal contradiction` is enabled).

The `list` table shows each gap's **entity name** (e.g. `"Acme Corp"`) and a
**rationale** column explaining why it was surfaced. JSON output includes the
same data as `target_name` and `rationale` fields.

### Commands

```bash
# Show the ranked gap queue — refresh detectors, list open gaps.
brain elicit list
brain elicit list --json
brain elicit list --limit 10             # show at most 10 gaps
brain elicit list --low-confidence       # include gaps below the score floor
brain elicit list --type person          # only person-type gaps
brain elicit list --type project --type org   # multiple types (repeatable)

# Run the interactive draft-then-correct loop (needs Ollama).
brain elicit
brain elicit --target "engineering culture"   # user-flagged: jump straight to this topic
brain elicit --signal delta                   # only surface delta gaps this run
brain elicit --signal orphan
brain elicit --signal contradiction           # needs BRAIN_ELICIT_CONTRADICTION_ENABLED=true
brain elicit --include-low-confidence         # include below-score-floor gaps in the loop
brain elicit --type project --type tool       # filter to specific entity types (repeatable)
```

Interactive keymap (one gap at a time):

| Key | Action |
|---|---|
| `e` | Open draft in `$EDITOR`. Save a non-empty, changed body to codify as a vault note. |
| `s` | Skip (dismiss) this gap. |
| `n` | Snooze — prompt for a number of days, suppress until then. |
| `q` | Quit the loop. |

The editor buffer opens with the drafted rule text and a comment block listing
the evidence doc IDs. The body must be **non-empty and changed from the draft**;
re-saving the draft unchanged re-prompts rather than codifying.

### Config knobs

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_ELICIT_MIN_EVIDENCE_DOCS` | `3` | Minimum distinct source docs required before a gap is draftable. |
| `BRAIN_ELICIT_MIN_GAP_SCORE` | `0.3` | Post-normalization score floor (0–1). Below-floor gaps appear in `list` but are skipped in the loop unless `--include-low-confidence`. |
| `BRAIN_ELICIT_QUEUE_LIMIT` | `20` | Max gaps surfaced per `list` run (`-n 0` uses this limit). |
| `BRAIN_ELICIT_CONTRADICTION_ENABLED` | `false` | Enable the contradiction detector (requires Ollama + non-null summaries). |
| `BRAIN_ELICIT_CONTRADICTION_MIN_DOCS` | `5` | Min entity doc-count before contradiction scan runs on that entity. |

## Proactivity and synthesis

Hybrid search and GraphRAG are reactive — you ask, the brain answers. This set
of commands is the proactive half: it surfaces what's due for review, digests
what just landed, synthesizes across time, flags contradictions, suggests
links, and answers multi-hop questions with citations. All eight are read-side
features over the corpus you've already ingested; none change ingest or search
ranking. The LLM-backed ones (`brief` suggestions, `ask`, `audio`, `timeline
--synthesize`, conflict `scan`) use the local Ollama and degrade gracefully
when it's unavailable.

Their tuning env vars are collected under
[Feature config knobs](configuration.md#feature-config-knobs) in the
configuration docs. Agents reach this whole set through the
[`brain-proactivity` skill](agent-skills.md) (`brief` / `resurface` / `review` /
`timeline` / `connect` / `gaps` / `capture`); `ask` and `audio` live in
`consult-brain`.

`brain brief` and `brain resurface` in action — the day's digest, then the
older notes ranked for another look (recorded against a synthetic corpus):

![brain brief's daily digest followed by brain resurface's spaced-repetition table, over a synthetic compliance corpus](assets/proactivity.gif)

### brain resurface

Spaced-repetition resurfacing — the brain picks older notes you haven't
revisited recently and ranks them for review. Every non-draft, non-action-item
document is scored fresh on each run from its **age**, **last-access
staleness** (via the interactions log), and **importance** (tags + summary
presence), so a doc you opened yesterday drops out automatically.

```bash
brain resurface                      # top 7 docs due for review
brain resurface -n 15                # surface more
brain resurface --min-age-days 30    # only docs older than 30 days
brain resurface --source krisp       # filter by source kind
brain resurface --json
```

### brain brief

A proactive daily digest of recent captures, open todos, pins, and best-effort
LLM next-step suggestions. Surfaces **titles and todo texts only — never
document bodies**. On macOS the installer wires a launchd agent
(`com.brain.brief`) that runs `brain brief --wiki` once at **07:00 local** each
day and writes the digest to `<vault>/daily/<YYYY>/<date>-brief.md`.

```bash
brain brief                          # today's digest to the terminal
brain brief --since 48 --todo-since 14   # widen the capture / todo windows
brain brief --wiki                   # also write the dated vault page
brain brief --no-enrich              # skip the LLM next-step suggestions
brain brief --date 2026-06-09 --json
```

### brain review

Periodic synthesis over the corpus — a weekly review page plus a
contradiction/staleness scan queue. `weekly` assembles themes (graph
communities, or tag clusters with `--no-graph`), activity, open loops, new
captures, and key people for an ISO week and writes
`<vault>/reviews/<week>.md`. `scan` surfaces findings into the same review
queue that `list` reads and `dismiss` clears.

```bash
# Weekly synthesis page.
brain review weekly                          # current ISO week → vault page
brain review weekly --week 2026-W23          # a specific week
brain review weekly --no-graph               # tag-cluster themes (no AGE)
brain review weekly --no-emit --json         # stdout only

# Contradiction + staleness scan.
brain review scan                            # run both scans
brain review scan --conflicts --dry-run      # conflicts only, no writes
brain review scan --stale                    # staleness only
brain review list --kind conflicts -n 10     # read the queue

# Three distinct verbs for closing a finding — they are not interchangeable.
brain review dismiss <id-prefix>             # "this was never real" (noise)
brain review resolve <id-prefix>             # "I acted on it" → status='resolved'
brain review snooze  <id-prefix>             # "not now" → hidden, returns later
brain review snooze  <id-prefix> --days 30   # default is 7
```

**Pick the verb by what actually happened**, because the queue's usefulness
degrades if everything is dismissed. `dismiss` is for findings that were never
real — a false contradiction between two notes that do not in fact disagree.
`resolve` is for findings you acted on: the contradiction was reconciled, the
stale note was updated. `snooze` hides the finding for `--days` (default 7) and
then brings it back, which is the honest option for "real, but not today" —
dismissing those instead is how a review queue quietly stops reflecting reality.

A snoozed finding disappears from `brain review list` until its snooze expires.
That is intended, but it does mean a quiet queue is not proof of a clean corpus.

The **contradiction** leg is gated on `BRAIN_ELICIT_CONTRADICTION_ENABLED`
(default off) and needs Ollama; the **staleness** leg needs neither. Findings
land in `elicitation_gaps` alongside the `brain elicit` signals (migration 018
widens the `signal_kind` CHECK to include `stale` + `search_failure`).

### brain timeline

How a theme or entity evolved over **time**. Buckets the documents that mention
a query by document date (`COALESCE(sent_at, ingested_at)`) into month /
quarter / year periods, showing per-period doc counts, co-topics, and
representative titles. Requires the graph layer (`BRAIN_GRAPH_ENABLED` +
`brain graphrag build`); the query is ILIKE-resolved to graph entities and an
unknown entity prints a friendly note and exits 0.

```bash
brain timeline "platform migration"                    # default quarter buckets
brain timeline "hiring" --granularity year
brain timeline "Project Phoenix" --person "Jane Doe"   # scope to a participant
brain timeline "pricing" --since 2026-01 --until 2026-06
brain timeline "pricing" --synthesize                  # best-effort Ollama narrative
brain timeline "pricing" --json
```

### brain connect

Proactive auto-link suggestions — surfaces note pairs that share entities or
semantics but aren't linked yet, then lets you accept (optionally writing the
wikilink) or reject each one. `refresh` blends an entity-graph affinity leg
with an embedding affinity leg via RRF, drops already-linked and below-threshold
pairs, and upserts the top suggestions per source doc; accepted/rejected rows
are frozen and never re-proposed. The `connect refresh` stage also runs
non-fatally inside `brain-rebuild`.

```bash
brain connect refresh                  # recompute + upsert pending suggestions
brain connect refresh --doc <id> --dry-run
brain connect list                     # pending review queue
brain connect list --all --json        # every status
brain connect accept <suggestion-id> --write   # flip to accepted + append wikilink
brain connect reject <suggestion-id>           # freeze; never re-proposed
brain connect stats                    # pending / accepted / rejected counts
```

`accept --write` appends a path-form wikilink under a `## See Also` section at
the end of the source doc's vault file (idempotent — a repeated accept never
duplicates). The default minimum blended score is **0.60**
(`BRAIN_CONNECT_MIN_SCORE`, retuned up from 0.30 on live-corpus evidence).

### brain ask

Agentic multi-hop **cited** answer synthesis. Where `brain search` returns a
ranked list you read yourself, `brain ask` plans sub-queries, retrieves across
iterations, optionally reflects to fill coverage gaps, and composes a single
answer with inline `[N]` citations into your documents. Requires a local Ollama
for the plan/reflect/synthesize steps; if Ollama is down the command exits
non-zero with a clear message (no partial answer). Only document **snippets**
are sent to the LLM — never full bodies.

![brain ask plans sub-queries over the corpus, then synthesizes one answer with inline citations back to the source documents](assets/ask.gif)

_Recorded against a throwaway synthetic corpus; regenerate with `bin/brain-ask-gif`._

```bash
brain ask "what did I learn negotiating across my job searches?"
brain ask "what did we decide about the data pipeline?" --explain
brain ask "..." --no-loop              # single retrieve+synthesize pass (faster)
brain ask "..." --mode fuse            # RRF of graph + hybrid retrieval
brain ask "..." --mode auto            # graph router  | local | hybrid (default)
brain ask "..." -n 8 --max-iter 4      # -n is shorthand for --limit (docs retrieved per iteration)
brain ask "..." --json
```

Retrieval `--mode` is `hybrid` (vector/FTS only, default) `| auto | fuse |
local`; the three graph modes require the Apache AGE image. An answer-quality
eval harness ships alongside it: `brain eval --answer` loads
`tests/eval/answer_corpus.yaml`, runs `brain ask --no-loop` per case, and
reports citation-grounding metrics (live-model gated, no baselines).

### brain audio

A NotebookLM-style **two-host audio overview** of a theme or a person.
`--person` builds a themes overview (graph `themes` mode); `--topic` builds a
community-level overview (graph `global` mode — run
`brain graphrag communities build` first). Exactly one of the two is required.
The script is grounded **only** in entity names + document summaries (never raw
bodies). Writes `<out>.json` + `<out>.md`; with `--tts` it also synthesizes
audio via a pluggable backend (artifacts are written *before* synthesis, so
they survive a TTS failure). There is no MCP tool for `brain audio` — it's
CLI-only.

```bash
brain audio --person "Jane Doe"                  # themes overview script
brain audio --topic "platform migration"         # community-level (needs communities)
brain audio --person "Jane Doe" --turns 16
brain audio --topic "hiring" --tts 'shell:/path/to/tts.sh'   # synthesize audio
brain audio --person "Jane Doe" --json           # print script JSON, write no files
```

### brain gaps

Search-failure-driven knowledge-gap detection. Mines the `search_queries` log
for queries the brain failed to answer — `zero_results` (nothing matched) and
`no_click` (results returned but never opened) — over a lookback window, then
clusters them into knowledge gaps. The read view shows the **normalized**
canonical query label (raw query strings stay server-side); `push` upserts the
resulting `search_failure` gaps into the `brain elicit` queue.

```bash
brain gaps                            # failed-query clusters over the window
brain gaps --since 60 -n 30
brain gaps --json
brain gaps push                       # upsert search_failure gaps into the queue
brain gaps push --dry-run
```

Search-failure logging is **best-effort** — it never blocks or breaks a search.
On a brain whose database predates migration 019 (the `search_queries` table),
`brain gaps` prints a clean warning to run `brain init`, and searches keep
working.
