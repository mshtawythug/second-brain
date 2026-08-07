---
name: brain-ask
description: >
  Multi-hop, cited answer synthesis and audio overviews over the user's local
  second-brain. `brain ask` plans sub-queries, retrieves across iterations,
  and composes ONE answer with inline [N] citations back to the user's own
  documents; `brain audio` generates a two-host NotebookLM-style overview of a
  person or a topic. Use when a question needs several documents stitched
  together into an auditable answer, or when the user wants a listenable
  summary. For a single lookup, a quote, or drafting in the user's voice use
  `consult-brain`; for themes and connections across a relationship use
  `brain-graph`; for what is due for review use `brain-proactivity`.
  MANDATORY TRIGGERS: brain ask, brain audio, ask my brain properly,
  synthesize an answer, synthesize across my notes, cited answer, with
  citations, an answer with sources, multi-hop, what did we decide about,
  what did I learn about, pull it all together, connect the dots on, stitch
  my notes together, deep dive across my documents, walk me through what I
  know about, notebooklm, audio overview, two-host overview, podcast of my
  notes, narrate my notes.
---

# Brain Ask

Two commands that both produce a **composed artifact** rather than a list of
hits: `brain ask` writes one cited answer, `brain audio` writes a two-host
script (and optionally synthesizes it). Both are grounded strictly in the
user's own corpus, and both require a local Ollama.

## ask vs search vs graph

| Deliverable | Command | Skill |
|---|---|---|
| A ranked list of documents you read yourself | `brain search` | `consult-brain` |
| One composed answer with inline `[N]` citations | `brain ask` | this one |
| Themes, patterns, who-connects-to-what | `brain graphrag …` | `brain-graph` |
| A listenable two-host overview | `brain audio` | this one |

Reach for the manual search-then-read loop in `consult-brain` when you need
raw documents *in your context* — voice mimicry, verbatim quoting, drafting.
Reach for `brain ask` when the deliverable is the answer itself and the user
wants to see where each claim came from.

`consult-brain` also documents these two commands as part of its own workflow;
that is deliberate overlap, not a conflict. Use this skill when synthesis is
the point of the request.

## `brain ask`

```bash
brain ask "what tradeoffs did we weigh on the retention policy"
brain ask "how did the migration plan change" --explain
brain ask "what is my position on on-call rotation" --no-loop
brain ask "who owns the ingest pipeline" --mode fuse -n 8
brain ask "summarize the pricing debate" --max-iter 2 --json
```

| Flag | Purpose |
|---|---|
| `--mode` | `hybrid` (default, vector/FTS only) \| `auto` (graph router) \| `fuse` (RRF of graph + hybrid) \| `local` (graph, entity-centric). |
| `--no-loop` | Skip plan/reflect — one retrieve + synthesize pass. Faster, shallower. |
| `--limit` / `-n` | Max documents retrieved **per iteration**. |
| `--max-iter` | Hard cap on loop iterations. |
| `--explain` | Print the sub-query plan and the iteration trace. |
| `--json` | Structured output instead of prose. |

Default to plain `brain ask`. Add `--explain` when the user questions *how* an
answer was reached, and `--no-loop` when they want speed over coverage. The
three graph-backed modes (`auto`, `fuse`, `local`) need the Apache AGE image
and a built graph; on a plain install, stay on `hybrid`.

## Reading the citations

The answer carries inline `[N]` markers, each mapping to one of the user's
documents. Two rules:

- **Never restate a claim the answer did not cite.** An uncited sentence is the
  model's connective tissue, not something the corpus says. Do not promote it
  to a fact in your reply.
- **Always offer the underlying document.** When the user pushes on a specific
  `[N]`, hand them `brain show <id>` (see `consult-brain`) rather than
  re-summarizing what you already summarized.

An answer that says "the corpus does not cover this" is a correct answer. Do
not re-run with broader flags until something appears — that manufactures
confidence the documents do not support.

## `brain audio`

```bash
brain audio --person "Jordan Alvarez"
brain audio --topic "platform migration" --turns 12
brain audio --topic "hiring" --out /tmp/hiring-overview
brain audio --person "Jordan Alvarez" --tts 'shell:/path/to/tts.sh'
brain audio --topic "pricing" --json
```

Exactly one of `--person` or `--topic` is required:

- `--person` builds a themes overview (graph `themes` mode).
- `--topic` builds a community-level overview (graph `global` mode) and needs
  `brain graphrag communities build` to have run first.

`--turns` caps the dialogue (positive even integer). `--out` sets the artifact
base path, defaulting under `$BRAIN_HOME/audio/`. `--tts` synthesizes audio
through a pluggable backend spec. `--json` prints the script and writes no
files at all.

The `.json` and `.md` artifacts are written **before** TTS runs, so a failing
TTS backend still leaves the script behind.

## When it fails

| Symptom | Cause | What to do |
|---|---|---|
| `ask` exits non-zero citing Ollama | Ollama is down | Say so, offer `brain search` via `consult-brain`. **Do not retry in a loop.** |
| `ask --mode auto/fuse/local` errors | No AGE image / no built graph | Fall back to `--mode hybrid`. |
| `audio --topic` finds no communities | `graphrag communities build` never ran | Route to `brain-graph` / `brain-maintenance`. |
| `audio --person` finds no such person | Name is not in the graph | Report it plainly; do not guess spellings. |

`brain ask` exits non-zero rather than returning a partial answer. That is
deliberate — a half-retrieved synthesis reads exactly like a complete one.
Treat a non-zero exit as "no answer", never as "try harder".

## Safety rules

- **Only snippets reach the LLM in `ask`, and only entity names plus document
  summaries in `audio` — never full bodies.** Do not work around that by
  pasting document bodies into a prompt yourself.
- **`brain audio` artifacts land in the gitignored `audio/` directory. Never
  commit them.** They are generated, personal, and often large; they do not
  belong in version control under any circumstance.
- **`brain audio` writes files by default.** Use `--json` when the user only
  wants to read the script, and confirm before pointing `--out` anywhere
  outside the default directory.
- **There is no MCP tool for `brain audio`** — it is CLI-only. Do not claim
  otherwise or invent a tool name for it.
- **Never present a `brain ask` answer as your own reasoning.** It is a
  synthesis over the user's documents; say so, and keep the citations attached
  when you relay it.
- **Both commands are read-only against the corpus.** Neither ingests, edits,
  nor deletes anything, so a failed run never needs cleanup.
