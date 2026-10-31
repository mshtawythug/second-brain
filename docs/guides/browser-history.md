# Ingesting browser history and bookmarks (allowlist only)

> Part of the [Second Brain](../../README.md) docs. See [docs/README.md](../README.md) for the full index.

`brain ingest-web` (roadmap ticket RM-41) makes the pages you keep going back to searchable: pages
you bookmarked, or visited at least three times, **on hosts you have listed**. It reads Chrome's
or Safari's own history files on this Mac. Nothing is fetched from the web unless you ask for it
with `--fetch`, and brain stores no account or token. The command reference is in
[cli-reference.md](../cli-reference.md#browser-history-ingest).

```bash
brain ingest-web --browser chrome --since 2026-10-01 --dry-run   # list what would be ingested
brain ingest-web --browser chrome --since 2026-10-01             # ingest it
```

## Step 1: the allowlist (nothing happens without it)

Browsing history is private by nature, so the command ingests **nothing** until you list the
hosts it may take pages from. The list is a plain text file, `$BRAIN_HOME/web-allowlist.txt`
(`~/.brain/web-allowlist.txt` for most installs):

```text
# pages from these hosts may become documents
docs.example.com
# every subdomain of example.org, but not example.org itself
*.example.org
example.org
```

- One entry per line: an exact host, or `*.` followed by a domain. A wildcard covers
  subdomains only; list the bare domain on its own line if you want it too.
- Lines starting with `#` are comments. A comment must take a whole line.
- Write every entry in ASCII. An internationalized host goes in its punycode form
  (`xn--bcher-kva.example.com`), because browsers and Python disagree about how some names
  (`faß.de`) convert.
- Refused: paths, ports, `https://`, a single-label host (`intranet`), `localhost` and its
  subdomains, a wildcard over one label (`*.com`), and an address in any form (`10.0.0.8`,
  `127.1`, `2130706433`, `0x7f.1`).
- **Not detected: public suffixes.** `*.co.uk` or `*.github.io` would allow every site under
  that suffix, and brain cannot tell them from your own domain without the Public Suffix List.
  Don't write one; list the exact hosts you mean.
- With no file, an empty file, or a malformed line, the command exits 2 and reads nothing. The
  message names the line number, never the line.

## Step 2: Full Disk Access (Safari only)

macOS keeps `~/Library/Safari` behind **Full Disk Access**. If your terminal app does not have
it, `brain ingest-web --browser safari` stops with one line asking you to grant it. Open
**System Settings → Privacy & Security → Full Disk Access**, turn it on for the terminal app you
run `brain` from (Terminal, iTerm, …), then restart that app. Chrome needs no permission.

## What is stored, and what is dropped

- **One document per page**, keyed by its cleaned-up URL. The query string (`?…`), the fragment
  (`#…`), any `user:password@` part and the default port are **removed before anything is
  stored**, because that is where session tokens and search terms live. Links that are not
  `http`/`https` (`file:`, `chrome:`, `javascript:` …) are never kept.
- **The body** is the page title, the URL and the host. With `--fetch`, the page's text is added.
- **Visit counts and times** go in the document's metadata, per browser (`chrome_visits`,
  `safari_last_visit` …), never in the body. A daily re-run updates them **without**
  re-chunking or re-embedding the page.
- **The title is kept as first stored.** `--refresh` rebuilds a page from today's title or
  bookmark name.
- **Sensitivity** is `normal` by default (the hosts are ones you chose).
  `--sensitivity confidential` stores new pages as confidential, and also raises pages already
  stored (their vault mirror included). A run never lowers a page to `normal`: an explicit
  `--sensitivity normal` leaves a confidential page as it is and names `brain mark-normal`.
- **Path parameters are dropped too.** `/app;jsessionid=…/page` is stored as `/app/page`:
  some servers put a session id there when cookies are off.

**A token in the path is not removed.** Some sites put a secret in the path itself (a share link
like `/s/<token>`, a reset link). The query-string rule does not catch those. Don't allowlist a
host whose links work that way, or exclude it. Every page still goes through brain's secret guard
(`BRAIN_SECRET_GUARD`).

## How the history is read

The browser's database is **never opened in place**. brain copies it and its write-ahead log into
a private temporary folder, reads the copy read-only, and deletes the copy. Your browser is never
locked or modified, and you don't need to quit it. If the browser was writing during the copy
and the copy comes out torn, brain retries once. If the retry fails, it asks you to close the
browser and run again.

- Only real files are read. A history file that is a symlink, a device or a pipe is refused.
- The copy is counted byte by byte against a 1 GiB limit. A disk that fills up during the copy
  stops the run with one line naming the error (for example `OSError ENOSPC`).
- A query that runs longer than 60 seconds is stopped.
- Closing the terminal (SIGHUP) or stopping the process (SIGTERM) still deletes the copy. A run
  killed outright (SIGKILL, a power cut) cannot clean up, so the next run deletes your own
  leftover copies that are more than 3 hours old.

## `--fetch`: downloading the page text (opt-in)

`--fetch` downloads the text of **new** pages only (`--refresh --fetch` re-fetches stored ones).
Every request, and every redirect, must:

- use `https`;
- go to an allowlisted host (a redirect off the list is refused);
- resolve only to **public** addresses. A host that resolves to anything on your local
  network, loopback, a cloud metadata address, or a documentation or benchmarking range is
  refused. So is an IPv6 address that wraps one of those (v4-mapped, NAT64, 6to4, Teredo). The
  connection goes to the address that was checked, with a fresh connection, and a fresh
  certificate check, for every host.

Each fetch stops after 10 seconds in total (every redirect included) or 1 MiB as sent. It refuses
compressed responses (any `Content-Encoding` other than `identity`) rather than unpacking them,
and accepts only HTML or plain text, in an ordinary text encoding. It sends no cookies and ignores
proxy settings. A page whose fetch is refused or fails is still stored with its title
and URL. Fetched pages get an auto-summary (`--no-enrich` skips it). Without `--fetch` there is
no summary, because a title and a URL are too short to summarize.

## Cost

`web_page` documents skip concept extraction in the graph lane. At most `--max` pages are written
in one run (default 500, newest visit first), and the command says how many more qualified.
Start with a recent `--since` and `--dry-run`.
