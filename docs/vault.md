# Vault

> Part of the [Second Brain](../README.md) docs — see [docs/README.md](README.md) for the full index.

The vault is a folder of plain Markdown with YAML frontmatter and `[[wiki-links]]`. Read it through `brain ui`, the reading surface (search, read and edit in a browser; see [Reading your brain from a phone](guides/brain-ui-on-your-phone.md) for LAN use), or open the folder in Obsidian, vim or any editor. The files stay readable when Postgres is down.

> **The rendered Quartz wiki was retired** in phase 9 of the wiki → `brain ui` consolidation (R3). `brain vault render`, `brain wiki install`, the build daemon and the Caddy recipe are gone. An older install may have left files behind that nothing reads any more; delete them by hand if you like: `<vault>/.quartz/` (workspace, `node_modules`, builds, `current`, `.cache`; vault sync, the watcher and `brain backup` all skip it while it is there), a `<vault>/.brain-fastpath-ignore` file, `<vault>/_partials/recent.md` and the `<!-- BRAIN_RECENT_START -->` … `<!-- BRAIN_RECENT_END -->` recent-items block in `<vault>/index.md` (the wiki's homepage rail; nothing refreshes them now), `$BRAIN_HOME/Caddyfile`, and `$BRAIN_HOME/build/`. A Caddy you started for the wiki is not stopped by upgrading and may still serve the last build on `:8080`; stop it with `brew services stop caddy` (or end the `caddy run` process).

## Vault model

Brain has two storage tiers, both searchable through the same hybrid index:

- **Ingested tier** (`kind='ingested'`) — Krisp transcripts, Slack threads, Gmail, raw files. Read-only by convention; the DB is authoritative. Files are mirrored under `_ingested/<source>/` in the vault folder, so the whole corpus is readable as plain Markdown.
- **Vault tier** (`kind='vault'`) — notes you author. The `.md` file on disk is the source of truth; the DB is a derived index that `brain vault sync` rebuilds from the file. Edit in any text editor.

Both tiers live in a single vault folder (default `~/brain-vault/`, override with `BRAIN_VAULT_PATH`). Wiki-links (`[[Title]]`, `[[brain:<id>]]`, `[[krisp:<external_id>]]`) cross both tiers — vault notes can link to ingested artifacts and back.

### One-time vault setup

```bash
# 1. Scaffold the vault folder + templates + _ingested/ subdirs.
brain vault init                         # creates ~/brain-vault/

# 2. Dump the existing DB into the vault as Markdown files. One-shot,
#    safe to re-run, idempotent.
brain vault export --to ~/brain-vault    # produces _ingested/<source>/*.md

# 3. Establish the round-trip baseline so future sync runs are no-ops
#    until you actually edit something.
brain vault sync
```

After this, your DB and vault are in lockstep. Edit any `.md` file in `~/brain-vault/` and the next `brain vault sync` (or the watcher — see below) will re-chunk + re-embed it.

### Authoring commands

```bash
# Create a note from _templates/note.md, opens $EDITOR.
brain note new "person-x conversation"

# Today's daily note at daily/<YYYY>/<YYYY-MM-DD>.md.
brain daily

# Rename a note safely — rewrites every [[old-title]] reference across
# the vault. Atomic snapshot/restore on failure.
brain note rename <id-prefix> "New title"
brain note rename <id-prefix> "New title" --dry-run   # preview the diff

# Edit an existing doc. For vault-tier docs, opens the file in $EDITOR
# directly. For ingested-tier (Krisp/Slack/Gmail), uses the JSON-header
# editor flow.
brain edit <id-prefix>
```

### Link graph

```bash
brain backlinks <id-prefix>              # what links TO this doc
brain links <id-prefix>                  # what this doc links to
brain links <id-prefix> --unresolved     # plus dangling [[refs]]
brain orphans                            # vault notes with no links
brain orphans --all                      # include ingested-tier
brain graph --format json                # full link graph as JSON
brain graph --format dot | dot -Tsvg > graph.svg && open graph.svg
brain graph --format mermaid             # paste into mermaid.live
brain graph --root <id> --depth 2 --format dot | dot -Tsvg > focus.svg
```

### Watcher mode

```bash
brain vault sync --watch
```

Runs as a daemon (Ctrl-C to stop). Filesystem events trigger debounced (500ms) per-file syncs — edit a note, save, and within a beat the chunk + embedding update in the DB. Skips `_templates/`, `_attachments/`, hidden directories. `brain-install-launchd` (which `bin/brain-up` runs) keeps it running under launchd as `com.brain.watcher`, and `brain doctor`'s `vault watcher` line reports when it is not installed (macOS) or not running (elsewhere). `brain ui` reports it too, in its top bar: "Watcher not running — external edits will not appear in search" (or "Watcher status unknown" where the process list cannot be read — doctor says the same off macOS). The two can legitimately differ only on macOS, where doctor checks that the `com.brain.watcher` plist is installed (leaving its health to the `daemons` line) while the UI looks for a running `brain vault sync --watch` process. That gives two disagreements: a watcher that is installed but has crashed is "not running" in the UI while doctor's `vault watcher` line says ok, and a watcher started by hand without the plist leaves the UI silent while doctor warns that it is not installed.

### Vault maintenance

```bash
# Preview or prune vault-tier DB rows whose source files vanished.
brain vault sync --dry-run
brain vault sync --prune

# Remove stale _ingested/ mirror files whose DB rows no longer exist.
brain vault prune-orphans
brain vault prune-orphans --include-stale
brain vault prune-orphans --apply

# Rebuild the name/email directory used by metadata-derived links.
brain vault directory refresh
brain vault directory show
brain vault directory show --source gmail

# Rebuild Gmail/Krisp metadata-derived graph edges and rewrite the
# "Related" fences in affected _ingested/ files.
brain vault relink-derived
```

Use `brain vault directory refresh` after a large Gmail ingest, after changing
Google contacts/calendar access, or after editing any people metadata consumed
by the linker. Use `brain vault relink-derived` when you want the graph and
"Related" fences to reflect the latest Gmail/Krisp corpus. Both
commands are idempotent; missing `gws` degrades to warnings, with Gmail-derived
directory entries still refreshed from already-ingested mail.

**Excluding the corpus owner from derived edges.** By default the
participant-overlap rules (R2 `shared_participant`, R3 `same_day_participant`)
treat every participant as graph-worthy — including yourself, which can make
every meeting/email link to every other doc you're on. Set
`BRAIN_OWNER_PARTICIPANTS` in `.env` to a comma-separated list of identifiers
to strip before the rules evaluate. Both emails AND display names are
accepted, matching is case-insensitive, and entries are trimmed +
lowercased at load time.

For full coverage **list every form your name appears under in your
corpus** — Gmail headers contribute both the email AND a normalized
display-name key for each participant, so listing only the email leaves
the display-name key behind and the rules still match. The recommended
shape is `<Display Name>,<email>[,<other-email>...]`:

```
BRAIN_OWNER_PARTICIPANTS="Pat Morgan,redacted@example.com,redacted@example.com"
```

After changing the value, run `brain vault relink-derived` to rebuild the
derived-links table, then `brain vault sync` to refresh the in-body
"Related" fences. Unset / blank disables the exclusion (existing behavior).

The `brain owner` subcommand group manages this list without hand-editing
`.env`: `brain owner show` prints the active list, `brain owner set
"<csv>"` replaces it, and `brain owner add <id>` / `brain owner remove
<id>` adjust one entry at a time (idempotent, case-insensitive). Each
mutation rewrites `.env` atomically and reminds you to run
`brain vault relink-derived` + `brain vault sync` afterward.

## Daily use — `bin/` scripts

Four convenience scripts under `bin/` keep the vault in sync. They assume `bin/` is on your PATH — see [Running `brain` from any directory](configuration.md#running-brain-from-any-directory) in the configuration docs for the one-time setup.

```bash
brain-up       # start the vault sync watcher (launchd, com.brain.watcher) and the
               # 07:00 daily-brief job (com.brain.brief). Idempotent.
brain-down     # stop the sync watcher (and any legacy build watcher left by an
               # older install).
brain-rebuild  # full-corpus rebuild: embeddings → summaries → search →
               # graph → graph-weights → communities → connect → mirror
               # (vault export + sync-summaries + prune-orphans).
               # Common flags:
               #   --only STAGES   comma-separated stage ids to run
               #                   (--only mirror refreshes just the vault mirror)
               #   --skip STAGES   comma-separated stage ids to skip
               #   --dry-run       print the plan; run nothing, take no lock
               #   --force         bypass the in-flight-ingest guard
               # --wiki-only is removed (it exits 2): use --only mirror.
brain-status   # show whether the sync watcher is running, and the vault it inspected.
```

`brain-up` is idempotent: re-running it re-uses the running sync watcher. It runs `brain-install-launchd`, which also removes the retired `com.brain.build` daemon if an older install left it behind. It does not start `brain ui`, which runs in the foreground by design; it names it instead.

Env overrides:

| Variable | Default | Purpose |
|---|---|---|
| `BRAIN_VAULT_PATH` | `~/brain-vault` | Vault directory the watcher operates against. `brain-install-launchd` (which `brain-up` runs) bakes it into the daemon plists, and when it is not exported it uses the value in `$BRAIN_HOME/.env`, the same precedence as `brain setup`. `brain-status`, `brain-monitor` and a manual `_brain-watcher-fg` run resolve it the same way, through `brain.install_config` (exported, else `$BRAIN_HOME/.env`, else the default). |
| `BRAIN_NO_BUILD_WATCHER` | `0` | Set `1` to skip launchd supervision during `brain-up` (used by the bin-script tests). The name predates R2; there is no build watcher left to skip. |
| `BRAIN_PY` | (unset) | Overrides the Python interpreter `brain-install-launchd` (which `brain-up` runs) writes into the daemon plists; unset, it uses the installer's own `sys.executable`. `brain-rebuild` is a Python console-script entry point that uses its venv's interpreter directly; `BRAIN_PY` does not affect it. |

The watcher's PID is tracked at `$BRAIN_HOME/run/brain-watch.pid` (moved off `/tmp` in `f5e551a` because macOS's `tmp_cleaner` reaped it and `brain-status` then falsely reported the daemon as stopped; override via `BRAIN_WATCH_PID`). `brain-down` still cleans up the legacy `brain-build.pid` and `brain-wiki.pid` files an older install may have left.
