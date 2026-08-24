---
name: elicit-brain
description: >
  Surface and codify tacit knowledge from the user's local second-brain —
  knowledge implied everywhere in their transcripts and notes but written
  nowhere. Use this skill when the user wants to discover what they know
  implicitly, fill in knowledge gaps, or turn unwritten rules into authored
  vault notes. Backs `brain elicit` (interactive draft-then-correct loop,
  needs Ollama) and `brain elicit list` (Ollama-free gap queue view). For
  plain document lookup or Q&A use `consult-brain`; for themes and patterns
  across interactions use `brain-graph`.
  MANDATORY TRIGGERS: elicit my tacit knowledge, what do I know that I
  haven't written down, what haven't I written down, surface my knowledge
  gaps, surface my tacit knowledge, what implicit knowledge, draft the
  unwritten rules, what rules am I not writing down, interview me about,
  what do I keep referencing without writing, brain elicit, knowledge
  elicitation, codify what I know, fill in my knowledge gaps, what's in my
  head that isn't in my notes, tacit knowledge, gap in my second brain,
  unwritten knowledge, what am I implicitly assuming, what have I never
  documented.
---

# Elicit Brain

Use `brain elicit` to surface tacit knowledge — things the corpus shows the
user knows implicitly (referenced constantly, implied everywhere) but has never
written as an authored vault note. The interaction is **draft-then-correct**:
the system makes a confident guess at the unwritten rule and the user corrects
it. The correction is the elicited knowledge. Codified notes are tagged
`tacit` + the signal kind and include a `## Source` section wikilinking the
evidence documents.

For plain document lookup and Q&A, use `consult-brain`. For themes and
patterns across interactions, use `brain-graph`. For action-item queries, use
`brain-todo`.

## When to use this skill

| The user says… | Reach for |
|---|---|
| "What do I know that I haven't written down?" | `brain elicit list` then `brain elicit` |
| "Surface my knowledge gaps" | `brain elicit list --json` |
| "Interview me about [topic]" | `brain elicit --target "[topic]"` |
| "Draft the unwritten rules I keep referencing" | `brain elicit --signal delta` |
| "What implicit knowledge do I have about [person/project]?" | `brain elicit --target "[name]"` |
| "Show me my knowledge gaps without drafting anything" | `brain elicit list` (Ollama-free) |

## The gap signals

| Signal | What it detects |
|---|---|
| `delta` | Entities heavily referenced in ingested docs (transcripts, Slack, mail) but never authored into a vault note — the cleanest proxy for tacit knowledge. |
| `orphan` | Graph entities with high mention-count but no written description. The graph knows *who* matters; this surfaces *why* it doesn't. |
| `contradiction` | Opposing positions across docs (flag-gated: `BRAIN_ELICIT_CONTRADICTION_ENABLED=true`; requires Ollama + non-null summaries). |
| `user_flagged` | A topic the user names with `--target`; always enters the queue regardless of corpus signals. |

## Viewing the gap queue — `brain elicit list`

Read-only. Runs the active detectors, upserts results, and renders the ranked
open gaps. **Does not need Ollama.**

```bash
brain elicit list                    # refresh detectors and show the queue
brain elicit list --json             # machine-readable (for synthesis)
brain elicit list --limit 10         # show at most 10 gaps
brain elicit list --low-confidence   # include gaps below the score floor
brain elicit list --type person --type org   # only person/org gaps (repeatable)
```

JSON shape per gap: `signal_kind`, `target_type`, `target_id`, `score`,
`evidence_ids` (list of doc IDs), `rationale`, `status`.

`--type` filters the queue to one or more entity types
(`person` / `org` / `project` / `topic` / `tool` / `doc`, repeatable) — pass it
when the user scopes the ask ("what haven't I written down about my *projects*").

## Interactive loop — `brain elicit`

**Requires Ollama.** For each gap in the ranked queue the command drafts a
confident rule text and opens it in `$EDITOR`. The user corrects/accepts; on a
non-empty, changed body the result is codified as a vault note.

```bash
brain elicit                                 # run the loop over the full ranked queue
brain elicit --target "engineering culture"  # user-flagged: jump to this specific topic
brain elicit --signal delta                  # only surface delta gaps this run
brain elicit --signal orphan
brain elicit --signal contradiction          # needs BRAIN_ELICIT_CONTRADICTION_ENABLED=true
brain elicit --type project                  # only draft gaps about projects (repeatable)
brain elicit --include-low-confidence        # include below-score-floor gaps
```

`--type` (repeatable: `person` / `org` / `project` / `topic` / `tool`) narrows
the loop to gaps about those entity types — combine with `--signal` to be
precise ("interview me about my *orgs*, delta signal only").

Interactive keymap:

| Key | Action |
|---|---|
| `e` | Open draft in `$EDITOR`. Save a non-empty, changed body to codify as a vault note. |
| `s` | Skip (dismiss) this gap. |
| `n` | Snooze — prompts for days, suppresses until then. |
| `q` | Quit the loop. |

The editor buffer opens with the drafted rule and a comment block listing
the evidence doc IDs. The body must be non-empty **and changed** from the
draft — re-saving the draft unchanged re-prompts rather than codifying.

## After codification

A codified gap produces:
- A vault note at `brain note new`'s path, tagged `tacit` + signal kind.
- A `## Source` footer with `[[wikilinks]]` to the evidence documents.
- The `elicitation_gaps` row updated to `status='resolved'` — the gap will not
  resurface.

Read the new note with `brain show <id-prefix>` — it is one document you just
wrote, so the body is bounded and known. Search future sessions with
`brain search "<topic>" --tag tacit --json --brief`; to read across several
tacit notes at once, `brain recall "<topic>" --tag tacit --budget 2000 --json`
keeps the read under a token ceiling. See `consult-brain` for the full
cost-ordered table.

## Config knobs

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_ELICIT_MIN_EVIDENCE_DOCS` | `3` | Minimum source docs required before a gap is draftable. |
| `BRAIN_ELICIT_MIN_GAP_SCORE` | `0.3` | Post-normalization score floor (0–1). |
| `BRAIN_ELICIT_QUEUE_LIMIT` | `20` | Max gaps surfaced per `list` run. |
| `BRAIN_ELICIT_CONTRADICTION_ENABLED` | `false` | Enable the contradiction detector. |
| `BRAIN_ELICIT_CONTRADICTION_MIN_DOCS` | `5` | Min entity doc-count for contradiction scan. |

## Prerequisites check

Before running `brain elicit` (the loop), verify:

```bash
brain doctor        # Postgres + Ollama must be healthy
brain elicit list   # confirms the table exists and detectors run cleanly
```

If `brain doctor` reports Ollama unavailable, `brain elicit list` still works
(Ollama-free). The interactive loop (`brain elicit`) will fail until Ollama is
running.

## When NOT to use this skill

- Plain "find docs about X", a quote, a single fact → `consult-brain`.
- "Themes in my conversations with X", "what connects A and B" → `brain-graph`.
- "What are my open action items?" → `brain-todo`.
- The user explicitly says "don't elicit" / "just search my brain".
