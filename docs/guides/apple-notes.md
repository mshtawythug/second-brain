# Ingesting Apple Notes (macOS)

> Part of the [Second Brain](../../README.md) docs. See [docs/README.md](../README.md) for the full index.

`brain ingest-apple-notes` (roadmap ticket RM-36) makes the notes in the Apple Notes folders you
name searchable. It reads them with `osascript -l JavaScript`, which ships with macOS, so there
is nothing to install and no account or token for brain to store. The command reference is in
[cli-reference.md](../cli-reference.md#apple-notes-ingest).

```bash
brain ingest-apple-notes --folder Reading --dry-run   # list what would be ingested
brain ingest-apple-notes --folder Reading             # ingest it
```

## The permission prompt on the first run

macOS does not let one app read another app's data without asking you. The first time
`brain ingest-apple-notes` runs (the `--dry-run` too, because it reads the same notes), macOS
shows a dialog like this:

> "Terminal" wants access to control "Notes". Allowing control will provide access to documents
> and data in "Notes", and to perform actions within that app.

The app named is the one `brain` runs inside: Terminal, iTerm, your editor's terminal, or the
app hosting an agent session. **Allow** lets the run continue. macOS remembers the answer for
that app, so later runs do not ask again.

brain only ever **reads**: the script it sends asks Notes for each note's id, name, folder,
dates, lock state and body, and nothing else. It never creates, edits, moves or deletes a note.
The script is a fixed program; the folder names you pass reach it as JSON data, not as code.

### If you clicked "Don't Allow", or nothing happens

A refused or missing permission makes `osascript` fail, and the command exits 1 with:

```
`osascript` failed (exit 1); if macOS asked for permission to control Notes, allow it in System Settings → Privacy & Security → Automation, then rerun
```

To fix it, open **System Settings → Privacy & Security → Automation**, find the app you ran
`brain` from, and turn on **Notes** under it. Then rerun the command.

If the app is not listed there, macOS has not asked yet: run the command again from that app
and answer the dialog. `tccutil reset AppleEvents` makes macOS ask again, but it resets the
Automation answers of **every** app, so prefer the switch in System Settings.

To revoke access later, turn the same switch off. Documents already ingested stay in the brain;
remove them with `brain rm` if you want them gone.

### Scheduled runs

There is no daemon for this command. A launchd job runs outside your terminal app, so it would
need its own Automation permission, which macOS can only ask for interactively. Run the command
yourself, from the app you granted.

## What is read, stored and skipped

- **Scope.** `--folder NAME` is required and repeatable; "all notes" is not offered. A folder is
  matched by its exact name in every account (iCloud, On My Mac), so two folders with the same
  name are both read. A name that matches no folder is warned about, and if none matches, the
  command exits 1. `--since YYYY-MM-DD` keeps notes modified on or after that day.
- **The body** is converted from Notes' HTML to Markdown: headings, bold and italic, lists,
  checklists (`- [ ]` / `- [x]`) and links (`http`, `https` and `mailto` only).
- **Attachments** (images, scans, drawings, files) are not ingested. Images are removed before
  the note leaves Notes, so their data never reaches brain.
- **Locked notes** are never read: their body cannot be read without the password, and brain
  does not ask for it. The command counts them: `2 locked note(s) skipped`.
- **Sensitivity.** Notes are stored as `confidential` unless you pass `--sensitivity normal`,
  because personal notes are private by nature.
- **Re-runs.** A note is identified by its Notes id. An edited note is updated in place, and a
  note moved to another folder only has its metadata updated.

## Size, time and cost

- At most `--max` notes a run (default 200), the most recently modified first. The command says
  how many more matched, so you can run it again with a higher `--max` or a narrower `--since`.
- A note whose HTML body is over 2,000,000 characters is skipped and counted, and the Markdown
  kept from one note stops at 1,000,000 characters, marked `[note truncated: …]`.
- Notes scripting is slow on large folders, so `osascript` is stopped after 120 seconds. If a run
  times out, lower `--max` or add `--since`.
- Every new note gets an Ollama summary (`--no-enrich` skips it) and, when the graph is on,
  concept extraction in the vault watcher's graph lane at several minutes per note. A full
  200-note run keeps the lane busy for more than a day, so start small.

## Checking the setup

`brain doctor` has an `apple notes` row. On macOS it says whether `/usr/bin/osascript` exists
(the command always runs that path and never searches `PATH`);
elsewhere it says the command is unavailable. It never runs `osascript`, so it never triggers
the permission prompt.
