# Capturing from your phone: the "Save to Brain" share sheet

> Part of the [Second Brain](../../README.md) docs. See [docs/README.md](../README.md) for the full index.

Share a link or some text from any app on your iPhone or iPad, add a comment if you like, and
it lands in your capture inbox (`brain capture list`) on the Mac. There is no app to install
and no network service on the Mac. An iOS Shortcut writes each share as a small `.md` file
into an iCloud Drive folder, and on the Mac that folder is a
[drop folder](drop-folders.md) in **capture** mode. Roadmap ticket RM-39.

## Before you start: your shares pass through iCloud

**The text you share is stored by Apple's iCloud Drive** between the phone and the Mac.

- It is end-to-end encrypted only if **Advanced Data Protection** is on (Settings › your
  name › iCloud › Advanced Data Protection). Without it, Apple holds the keys. **Turn
  Advanced Data Protection on** before using this.
- Do not share anything through this path that you would not keep in iCloud Drive.
- Once a share is imported, brain deletes its file from the folder. iCloud Drive keeps
  deleted files in **Recently Deleted** for up to 30 days. Empty it in the Files app if that
  matters to you.
- Shared text is **not scanned for secrets**. That matches `brain capture`: a share is
  something you chose to send. Don't share a password or an API key.

## 1. Make a folder in iCloud Drive

In the Files app on the phone, open **iCloud Drive** and create a folder called
`Brain Inbox`. On the Mac the same folder is:

```
~/Library/Mobile Documents/com~apple~CloudDocs/Brain Inbox
```

## 2. Build the Shortcut

Open the **Shortcuts** app and tap **+** to start a new shortcut. Name it **Save to Brain**.
The labels below are those in recent iOS versions and may differ slightly in yours.

1. Open the shortcut's settings (the **ⓘ** button). Turn on **Show in Share Sheet**. Under the
   share-sheet types, keep **Text**, **URLs** and **Safari web pages**, and turn the rest off.
2. In the first block ("Receive … from Share Sheet"), set **If there's no input** to
   **Ask For** › **Text**, so the shortcut also works when run on its own.
3. Add **Ask for Input**. Set its type to **Text** and its prompt to `Comment (optional)`.
   You can leave the answer empty.
4. Add **Format Date**. Set it to **Current Date** with the custom format
   `yyyy-MM-dd HH.mm.ss`. The file name then makes each share unique.
5. Add **Text** and lay it out like this. The two blue pills are variables: tap to insert
   **Provided Input** (the comment) and **Shortcut Input** (what you shared).

   ```
   [Provided Input]

   [Shortcut Input]
   ```

6. Add **Save File**. Make it save the **Text** from step 5:
   - turn **Ask Where to Save** off;
   - choose the `Brain Inbox` folder from step 1;
   - set the sub-path to `Save to Brain [Formatted Date].md`, inserting the **Formatted Date**
     variable from step 4;
   - leave **Overwrite If File Exists** off.

Share a web page to **Save to Brain** to try it. Type a comment, and a file appears in
`Brain Inbox`.

### The confidential variant

Duplicate the shortcut (long-press it › **Duplicate**) and rename the copy
**Save to Brain (confidential)**. In its **Text** action, put these three lines at the very
top, before the comment:

```
---
sensitivity: confidential
---
```

A share made with it becomes a **confidential** note. It is kept out of every published or
outbound surface, as any confidential document is. `sensitivity` is the only line brain
reads from that block; any other line there is ignored.

## 3. Point brain at the folder

Add the folder to `$BRAIN_HOME/drop-folders.toml` (`~/.brain/drop-folders.toml` for a
normal install):

```toml
[[folder]]
path = "~/Library/Mobile Documents/com~apple~CloudDocs/Brain Inbox"
mode = "capture"
```

A capture folder takes `.md` and `.txt` files and makes **normal** notes unless a share asks
for confidential. To make every share from this folder confidential, add
`sensitivity = "confidential"`. A share can raise a folder's level, never lower it. A
capture folder has no `kind`, and naming one is an error. It must not be your vault, be
inside it (such as the vault's own `capture/`), or contain it: brain refuses such a folder,
because it would take your existing notes as new shares. All the keys are listed in
[configuration](../configuration.md#drop-folderstoml--drop-folders-rm-33).

Check it, then import once by hand:

```bash
brain capture import --dir ~/"Library/Mobile Documents/com~apple~CloudDocs/Brain Inbox" --since 7d --dry-run
brain capture import --dir ~/"Library/Mobile Documents/com~apple~CloudDocs/Brain Inbox" --since 7d
brain capture list
```

`brain ingest-folder --configured --since 7d` imports it too, alongside your other drop folders,
and both commands go through the same code. To import every 10 minutes, install the drop-folder
LaunchAgent once (`brain-install-launchd --with-dropfolders`; see
[drop folders](drop-folders.md#3-run-it-every-10-minutes-optional)). It also covers this folder.

macOS may refuse a background job access to iCloud Drive. If a run reports `PermissionError`,
allow it under System Settings › Privacy & Security (Files and Folders, or Full Disk Access).

## What happens to a share

- Each file is moved into the folder's `imported/` sub-folder first, as with every drop folder.
  It then becomes a note in your vault's `capture/` folder, tagged `inbox` and titled from its
  first words (as `brain capture` titles a note). After that the file is **deleted**. Review
  shares as usual with `brain capture review`.
- The note is the file's text (your comment, then the link or text you shared), minus the
  `---` block. `sensitivity` is the only key the block may carry. The block must close within
  40 lines and 4 KiB. A block that does not close, or a `sensitivity` value brain does not
  know, makes the note **confidential**: when brain cannot tell what you meant, it plays safe.
  If all of a share's text sits inside the block, the whole text is kept as the note, so
  nothing you wrote is dropped.
- **The same file is one note.** A share is identified by its file name plus its contents, so
  a file seen twice is imported once. Two shares with the same content are both kept as long
  as their names differ, which the date in the Shortcut's file name ensures.
- iCloud files that have not downloaded yet (evicted, or a `.icloud` placeholder) are left
  alone until they arrive. So is a file modified in the last 5 seconds.
- The run reports each file:

  | Status | Meaning |
  |---|---|
  | `ingested` | A new inbox note was made; the file is gone. |
  | `duplicate` | This file (same name and contents) was imported before, so nothing new was made, and the file is gone. Whichever way brain found that out, it is reported as `duplicate`. |
  | `empty` | Nothing to capture (only blank lines); the file is gone. |
  | `written` | The note was written to the vault, but indexing it failed (the database or the embedder was down). The next `brain vault sync` (or the vault watcher) indexes it. The file is gone, because the note holds its content. |
  | `rejected` | The file is over 1 MiB or not UTF-8 text. It stays in `imported/` and is not retried. |
  | `failed` | Something that may pass next time (no vault template, the database was down before the note was written). The file goes back to the folder and is retried on the next run. |

  If the file cannot be deleted (a locked file), it stays in `imported/`, and the report adds
  `imported copy kept`.

Your content always sits in exactly one of three places: the folder, `imported/`, or the vault.
A file is deleted only once its note exists.

## Troubleshooting

- **Nothing happens.** Run the `--dry-run` command above. Files the phone has saved but iCloud
  has not downloaded yet are not listed. Open the folder in Finder to start the download.
- **`… is configured as an ingest folder`.** The folder's entry in `drop-folders.toml` has no
  `mode = "capture"`.
- **`a capture folder must not be the vault …`.** Point the Shortcut and the folder entry
  at a folder outside your vault, such as the iCloud Drive folder above.
- **A `rejected` share.** It is in `<folder>/imported/`. Read it there, and capture what you
  need with `brain capture --text …`. Then delete the file, because it is still in iCloud.
