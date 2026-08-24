---
name: brain-memory
description: >
  The read/write protocol for the user's second brain — search before
  answering, and write back the durable residue of a session. Use when a
  session produced a decision, constraint, or hard-won gotcha worth keeping,
  when a Stop-hook nudge asks for a capture pass, or when the user says to
  remember something. For retrieving and synthesizing existing content, use
  `consult-brain`; for bulk-importing external content, use `ingest-brain`.
  MANDATORY TRIGGERS: remember this, write this down, save this to my brain,
  note this for later, capture this decision, add this to memory, log this
  learning, don't forget that, keep this in the brain, capture pass, session
  capture, what did we learn.
---

# Brain Memory

Agents read the brain reliably and write to it almost never, because writing is
always the last step of a task and nothing forces it. This skill is the write
protocol: what earns a durable note, how to check the brain does not already
say it, and how to record it in one call.

**Division of responsibility.** `consult-brain` is the read protocol — how to
retrieve and synthesize. `brain-memory` is the write protocol — when and how the
durable residue of a session goes back in, plus the dedupe rule that stops the
brain filling with near-duplicates. For bulk-importing external content (files,
Gmail, Krisp, Slack), use `ingest-brain`. For themes and connections across
interactions, use `brain-graph`.

## When this fires

1. **The user says so** — "remember this", "don't forget that", "keep this in
   the brain". Always write. Do not second-guess an explicit instruction.
2. **A Stop-hook nudge arrives** — a session-end hook decided the session did
   substantive work and nothing went back. Do **one** capture pass (below), then
   stop. Do not start new work.
3. **You noticed something durable mid-session** — a decision got made, a
   constraint bit you, someone told you a fact you will need again. Offer it in
   one line; write it if the user agrees or if it is unambiguously theirs.

## The read half

Before answering anything about the user's own history, work, or prior
decisions, search first:

```bash
brain search "<2-4 keywords>" --limit 5 --json --brief
```

`--brief` returns each hit's ingest-time summary instead of its chunk snippet
when the summary is cheaper — enough to tell you *whether* the brain already
knows something, which is all this pass needs.

If the search says yes and you now need the material itself, read it under a
ceiling: `brain recall "<keywords>" --budget 2000 --json`. Never loop
`brain show` over the hits — that returns every body in full, is unbounded,
and this skill's read half never needs it. The measured cost is in
`consult-brain`'s cost-ordered table.

Retrieval mechanics — filters, `brain recall`, `brain show`, `brain ask`, voice
writing — live in `consult-brain`, including the cost-ordered command table. Do
not restate them here; hand off.

## The write half

One capture per pass. One claim per capture.

```bash
brain capture --json --tag <topic> --text "<the durable fact in one or two sentences>"
```

Assert on the response. It returns exactly six keys:

```json
{
  "document_id": "3f2a1b9c-7d4e-4a11-9c02-5b6e8f0a1234",
  "id_prefix": "3f2a1b9c",
  "title": "2026-07-25-capture-hnsw-index-caps-at-2000-dims",
  "tags": ["inbox", "pgvector"],
  "vault_path": "capture/2026-07-25-capture-hnsw-index-caps-at-2000-dims.md",
  "status": "ingested"
}
```

`"status": "ingested"` is the success signal, and it is keyed identically on the
MCP tool `brain_capture` — so the same assertion works whichever surface you
used. Report the `id_prefix` back to the user. If the command exits non-zero it
prints a plain error on stderr, **not** JSON; treat that as a failure and say so
rather than claiming a write that did not happen.

Every capture lands tagged `inbox` for later triage. Add one topical `--tag` so
it is findable before it is triaged.

### What IS worth writing

- **A decision and the reason behind it** — not just "we chose Postgres", but
  why, and what it ruled out.
- **A constraint discovered the hard way** — "pgvector caps HNSW at 2000 dims,
  so the 4096-dim backend has no index." The kind of thing that costs an hour
  to rediscover.
- **A fact about a person, system, or commitment** that will not be obvious
  from the repo in three months.
- **A correction** to something already in the brain — see the dedupe rule.

### What is NOT worth writing

- A summary of what you just did. The user was there.
- Anything `git log`, the code, or `CLAUDE.md` already says.
- A restatement of a document already in the brain.
- Speculation, or anything the user did not confirm.
- Transient state — "the test is currently failing", "the branch is mid-rebase".
- Routine edits, lookups, or answers to generic technical questions.

When a nudge fires and nothing clears this bar, say so in one line and stop.
Declining is a valid outcome; padding the brain with session summaries is worse
than writing nothing.

## Dedupe before you write

Never write without checking first:

```bash
brain search "<the claim in 3-5 words>" --limit 5 --json --brief
```

Then:

- **A hit already states it** → do not write. Say `already in the brain
  (<id-prefix>)` and stop.
- **A hit contradicts it** → do not add a second competing note. `brain edit
  <id-prefix>` the existing one so the brain holds one current answer. See
  `brain-authoring` for the edit flow.
- **A hit is adjacent but not the same** → write the new one, and mention the
  neighbour so the user can link them.
- **No hits** → write it.

Two near-duplicate notes are worse than one, because retrieval then returns
both and the user has to adjudicate.

## Citing memory that changed the answer

When a retrieved document materially changed what you said, name it inline:
"from your 2026-05-20 note (`3f2a1b9c`) …". Then log the signal:

```bash
brain rate <id-prefix> useful        # this doc helped
brain rate <id-prefix> irrelevant    # false positive
```

Log silently — do not ask permission. This feeds ranking. Same convention as
`consult-brain`.

## Operational notes

- The `brain` CLI works from any cwd. No need to `cd` into the second-brain repo.
- The session-end nudge is installed with `brain claude install-hooks` and is
  **opt-in**. It fires at most once per session, only after real work, and only
  when nothing was written back. Disable it with `BRAIN_HOOK_ENABLED=false` or
  `brain claude install-hooks --uninstall`.
- Tuning knobs are real environment variables, not `.env` entries:
  `BRAIN_HOOK_ENABLED`, `BRAIN_HOOK_MIN_TOOL_CALLS` (default 12),
  `BRAIN_HOOK_TRANSCRIPT_MAX_BYTES`, `BRAIN_HOOK_SENTINEL_TTL_DAYS`.
- Captures are picked up by the normal `brain-rebuild` cycle. They are not
  summarized or graph-synced inline, so a fresh capture will not appear in
  graph queries immediately.
- Review the inbox later with `brain capture list` / `brain capture review` —
  see `brain-proactivity` for the triage flow.
- If `brain capture` fails because the vault is unconfigured, that is a setup
  problem — see `brain-maintenance`, and do not retry in a loop.
