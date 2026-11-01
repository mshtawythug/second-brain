# Using your brain from a second machine

The brain has **one writer**: one Postgres database and one vault, on one machine. Everything that
writes assumes that, including ingest, the vault watcher, the graph lane and content-hash dedup.
Two machines that both write would need a conflict-resolution product, and there isn't one. Two-way
sync is therefore **never** supported (roadmap decision RM-47.D1).

There are two supported ways to use the brain from a second machine. Neither one has two writers.

| | One brain, reached from elsewhere | A read replica |
|---|---|---|
| What runs on the second machine | An MCP client or a browser | A full brain install, holding a copy of the corpus |
| Freshness | Live | As of the last backup you restored |
| Works offline | No, it needs the primary on the network | Yes |
| Where confidential documents are | Only on the primary. Excluded over MCP by default. | **All of them, on the second machine too** |
| Writes from the second machine | Only with `brain mcp serve --allow-writes` | Lost at the next restore. Treat the replica as read-only. |

Prefer the first one when the primary is reachable. Use a replica when you need the corpus where
there is no network, such as on a flight, or on a machine that cannot reach the primary.

## Option 1: one brain, reached from elsewhere

- **Tools for an agent or MCP client:** `brain mcp serve` serves the MCP tools over HTTP. It is
  loopback-only, read-only and confidential-excluded by default. See
  [Reaching your brain from another machine](mcp-http-transport.md).
- **Reading in a browser:** use `brain ui --lan` or `--host <address> --token`. See
  [Reading your brain from a phone](brain-ui-on-your-phone.md).

Nothing is copied, so there is nothing to keep in sync.

## Option 2: a read replica

A replica is a second install that holds a restored copy of the primary's latest backup. You
refresh it by restoring a newer backup. Nothing flows back to the primary.

### Before you start

- **Use the same embedder on both machines.** `brain restore` refuses an archive embedded with a
  different `BRAIN_EMBEDDER`, or with a different vector size, because vectors cannot be translated
  between models. Check the primary's `$BRAIN_HOME/.env`.
- **Use an encrypted disk on the replica,** such as FileVault on macOS. A replica holds the whole
  corpus, including documents marked `confidential`, which the MCP path in option 1 keeps out by
  default.
- **Do not install the background daemons on the replica.** Run `brain setup` without `--daemons`,
  and do not run `brain-install-launchd`. The vault watcher and the scheduled ingests write, and
  anything they write is lost at the next restore.

### First restore

On the **primary**:

```bash
brain backup                       # prints the archive path, e.g. ~/.brain/backups/brain-backup-<ts>.tar.gz
```

Copy the archive **and** its `.sha256` sidecar to the replica over an encrypted channel. For
example:

```bash
scp ~/.brain/backups/brain-backup-<ts>.tar.gz* replica.example.internal:~/incoming/
```

On the **replica**:

```bash
brain setup                        # same embedder as the primary; no --daemons
brain restore ~/incoming/brain-backup-<ts>.tar.gz
brain doctor                       # look for the "replica" row
```

`brain restore` checks the sidecar and the manifest before it changes anything. It always takes a
pre-restore backup of the replica first. On a fresh install the database and vault are empty, so
the only prompt is the y/N confirmation.

The archive leaves out the AGE graph mirror, though the graph's relational tables are restored
exactly. If you use GraphRAG on the replica, run `brain graphrag build --force` after every
restore, as `brain restore` reminds you. Until you do, `brain doctor` reports `graph drift`, and
graph queries find nothing.

### What `brain doctor` shows

Every successful `brain restore` writes `$BRAIN_HOME/state/restored-from.json`. The file records
when the restored backup was taken, the archive's file name (never its full path), and when the
restore ran. `brain doctor` reads it and prints:

```text
replica         OK (read replica, restored from a backup taken at 2026-10-30 18:15 UTC (brain-backup-20261030-181500.tar.gz); writes here are lost at the next restore)
                — a primary recovered from a backup is not a replica: remove $BRAIN_HOME/state/restored-from.json (docs/guides/second-machine.md)
```

A machine that has never been restored has no such file and no `replica` row.

If the file cannot be read, the row is a WARN that says `restored-from marker is unreadable`.
Causes include a hand edit, a symlink or a file that is too large. The row never prints the file's
contents. Remove the file, and on a replica the next restore writes it again.

### Refreshing the replica

Repeat the first restore with a newer archive. On the primary, run `brain backup` and copy the
archive across. Then, on the replica:

```bash
brain restore ~/incoming/brain-backup-<ts>.tar.gz
brain graphrag build --force       # if you use GraphRAG: rebuilds the AGE graph mirror
brain doctor
```

These are the two follow-ups `brain restore` prints ("Then run: brain graphrag build --force",
"Then run: brain doctor"). The archive does not carry the AGE graph mirror. Until the rebuild runs,
`brain doctor` shows `graph drift  drift detected (entities rel=N age=0, …)` and graph queries come
back empty. After the rebuild it shows `graph drift  OK`.

- **Every refresh asks for the typed phrase.** After the first restore the replica is not empty, so
  `brain restore` asks you to type `restore and overwrite my brain`. No flag skips it, `--yes`
  included, so a refresh cannot run unattended from cron. This is deliberate: the same command on
  the primary would destroy data.
- **Each refresh keeps what it replaced.** The previous database is renamed to
  `<database>_replaced_<timestamp>` and the previous vault to `<vault>.replaced-<timestamp>`. Each
  refresh also writes a `pre-restore` archive to the replica's backup directory. Once the refreshed
  replica looks right, drop the old database by its exact name (as the restore printed it) and
  delete the old vault directory and archive. Otherwise they add up to one corpus-sized copy per
  refresh.

### Keep the replica read-only

Anything written on the replica is overwritten by the next restore. On the replica:

- do not run `brain ingest*`, `brain capture`, `brain note`, `brain edit`, `brain tag` or
  `brain rm`;
- do not run `brain vault sync --watch`, and do not edit vault files;
- never restore a **replica's** archive onto the **primary**. That is the one path by which
  replica writes would reach the primary, and it would also replace everything the primary gained
  since the replica's backup.

Searching, `brain ask`, `brain show`, `brain ui` and the read-only MCP tools are all fine.

### Handling the archive

An archive is the whole corpus: every document, transcript and email body, plus the vault.

- Copy it only over an encrypted channel (`scp`, `rsync -e ssh`, or a private network).
- Do not leave it on an unencrypted USB stick or a synced cloud folder.
- Delete the copy on the replica once the restore succeeds. The replica's own pre-restore backups
  are enough to undo a refresh.

### A primary restored from its own backup

Restoring a backup onto the **primary**, for example after a disk failure, also writes
`restored-from.json`, so `brain doctor` would describe the primary as a read replica. Once the
primary is back in service as the writer, remove the file:

```bash
rm "$BRAIN_HOME/state/restored-from.json"
```

`brain doctor` then shows no `replica` row. The file only describes the machine; removing it
changes no data.
