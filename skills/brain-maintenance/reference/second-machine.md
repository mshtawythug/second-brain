# Using the brain from a second machine (read replica)

Part of the [`brain-maintenance`](../SKILL.md) skill. Its safety rules apply here too. The full
user guide is `docs/guides/second-machine.md`.

The brain has **one writer**. Two-way sync between machines is **never** supported: do not
suggest rsyncing `data/postgres`, two machines writing to one vault, or merging two databases.

## Two supported shapes

1. **One brain, reached from elsewhere:** `brain mcp serve` (see [mcp-http.md](mcp-http.md)) or
   `brain ui --lan`. Prefer this when the primary is reachable.
2. **A read replica:** the second machine restores the primary's backups.

```bash
# primary
brain backup
# copy the archive AND its .sha256 sidecar over an encrypted channel (scp / rsync -e ssh)
# replica: same BRAIN_EMBEDDER as the primary, no --daemons
brain setup
brain restore <archive>
brain doctor            # shows the "replica" row
```

## What to tell the user

- **Refreshing is a restore.** After the first restore the replica is non-empty, so every
  refresh asks for the typed phrase `restore and overwrite my brain`. `--yes` does not skip it.
  Never type it for the user, and never offer to automate it.
- **Treat the replica as read-only.** Ingest, capture, note edits and the vault watcher on the
  replica are lost at the next restore. Do not run them there. Never restore a replica's archive
  onto the primary.
- **Each refresh keeps what it replaced:** a `<db>_replaced_<ts>` database, a
  `<vault>.replaced-<ts>` directory and a `pre-restore` archive. The **user** drops them by exact
  name once satisfied. Do not drop databases yourself.
- **A replica holds every document, confidential ones included.** It belongs on an encrypted
  disk, and the archive is handled like the corpus: no unencrypted cloud folders.

## The `replica` doctor row

Every successful `brain restore` writes `$BRAIN_HOME/state/restored-from.json` (backup time and
archive name). `brain doctor` then prints `replica  OK (read replica, restored from a backup taken
at …)`. With no file there is no row.

- **WARN `restored-from marker is unreadable`:** the file was edited, replaced by a symlink or
  grew. The user removes it; a replica's next restore writes it again.
- **A primary restored after a disaster** also shows the row. Once it is the writer again, the
  user removes `$BRAIN_HOME/state/restored-from.json`. That changes no data.
