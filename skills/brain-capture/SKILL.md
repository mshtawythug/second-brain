---
name: brain-capture
description: >
  Zero-friction quick capture into the user's local second-brain, and the
  inbox review loop that processes what was captured. Use this skill when the
  user wants to jot something down fast without deciding where it goes, dump a
  thought or idea mid-conversation, or later triage the inbox — promoting
  items into real notes, retagging them, or discarding them. Backs
  `brain capture`, `brain capture list`, and `brain capture review`. For
  ingesting content that already exists (a file, an email, a transcript) use
  `ingest-brain`; for authoring a deliberate titled note use `brain-authoring`;
  for what *you the agent* record about the user across sessions use
  `brain-memory`.
  MANDATORY TRIGGERS: capture this, jot this down, quick note, note to self,
  add to my inbox, dump this in my brain, capture idea, my inbox, review my
  inbox, process my inbox, triage my captures, what's in my inbox, brain
  capture.
---

# Brain Capture

The inbox pattern: capture now, decide later. `brain capture` takes a thought
that has no home yet, stores it as a document tagged `inbox`, and gets out of
the way. Nothing about the note's eventual place — folder, title, tags — has to
be settled at capture time. `brain capture review` is the other half: the
deliberate pass where inbox items become real notes or get thrown away.

Capture is cheap and reversible. Review is **interactive and destructive** —
read "Safety rules" before running it.

## capture vs ingest vs note new

| The content is… | Use | Why |
|---|---|---|
| A thought the user just had, no home yet | `brain capture` | Tagged `inbox`, triaged later |
| A file / email / transcript that already exists | `ingest-brain` | Extraction, dedup, source tracking |
| A deliberate note with a title and a place | `brain-authoring` (`brain note new`) | Templates, folders, frontmatter |

If the user says "capture this" about something they just said, that is this
skill. If they say it about a document, a link, or a meeting, it is
`ingest-brain` — those need extraction, not a text box.

## Capturing

```bash
brain capture --text "Follow up on the cutover window before the next review"
brain capture --text "Idea: batch the nightly re-embed" -t ideas -t infra
echo "A thought, piped in" | brain capture --title "re-embed idea"
```

| Flag | Purpose |
|---|---|
| `--text <str>` | Content inline. Omit it and content is read from **stdin**. |
| `--title <str>` | Title. Defaults to a date-stamped auto-title. |
| `-t` / `--tag <str>` | Extra tag(s), repeatable, applied **alongside** the always-on `inbox` tag. |

Two rules worth internalising:

- The `inbox` tag is **not optional** and is not something you pass — every
  capture gets it. That tag is the entire mechanism by which `capture list`
  and `capture review` find the item later.
- With no `--text`, capture reads stdin. Invoking it with neither a `--text`
  flag nor piped input blocks waiting on a terminal that is not there. Always
  supply one or the other.

Capture the user's words, not your paraphrase. The value of an inbox item is
that it preserves the original thought; a summary of it is a different, lesser
artifact. If the thought is long enough to need structure, it is a note — hand
off to `brain-authoring`.

## Reading the inbox

```bash
brain capture list
brain capture list --json
```

A compact `<id> <kind> <type> <title>` table, mirroring `brain list`. The JSON
form is an array whose items carry their `tags` (always including `inbox`) —
use it when you need to reason about the inbox rather than show it.

An empty inbox prints nothing interesting and exits 0. That is the desired
steady state, not a problem to investigate.

## Triaging the inbox

```bash
brain capture review
brain capture review -n 5
brain capture review --auto
```

Interactive review offers five choices per item:

| Key | Action |
|---|---|
| `p` | **Promote** — drop the `inbox` tag; the item becomes an ordinary document |
| `t TAG ...` | **Tag** — add tags and drop `inbox` |
| `d` | **Discard** — delete the document, behind a second explicit `y` / `Y` |
| `s` | **Skip** — leave it in the inbox |
| `q` | **Quit** the pass |

`--limit` / `-n` caps the pass at N items (default 10). `--auto` routes every
item through the local-Ollama tag proposer instead of asking; it leaves items
without a summary untouched, so an `--auto` pass that clears nothing is a
normal outcome, not a failure.

After a promote or tag, the item is an ordinary document — reachable through
`brain search` / `brain show` (see `consult-brain`) and editable through
`brain-authoring`.

## Safety rules

- **`brain capture review` is interactive and destructive. NEVER pipe blind
  `d` / `y` responses into it.** `--limit` selects items by **inbox order**,
  not by relevance, so a scripted confirmation deletes whatever happens to sit
  at the front of the queue — not the item you had in mind. Doing exactly this
  destroyed a real note on 2026-06-09. Show the user each item and let them
  choose. If they want a bulk pass, `--auto` is the supported route: it
  retags, it never deletes.
- **`d` deletes the document outright.** No undo, no trash. Confirm with the
  user before discarding anything you did not capture in this same
  conversation.
- **Never capture on the user's behalf without being asked.** A capture writes
  to their corpus; "that's interesting" is not a request to store something.
- **Never invent the content.** Capture what the user said. If you cannot tell
  which part of a long message they meant, ask — an inbox full of your guesses
  is worse than an empty one.
- **Exit 0 with no output is a valid result.** An empty `capture list` means
  the inbox is clear. Do not re-run with different flags hunting for items.

If `brain capture` errors outright instead of printing a confirmation, that is
an environment problem — route to `brain-maintenance` (`brain doctor` first)
rather than retrying the capture and risking a duplicate.
