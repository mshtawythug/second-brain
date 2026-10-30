# Gmail — `brain ingest-gmail`

Part of the [`ingest-brain`](../SKILL.md) skill. Its safety rules apply to this recipe too.

This command **refuses to run without a scope flag** by design. Pick the
narrowest scope that satisfies the user's ask:

| User ask | Flag |
|---|---|
| "Ingest emails from `<person>`" | `--from someone@example.com` |
| "Ingest my Linear tickets / GitHub PR notifications" | `--label "linear/notifications"` or whatever the user uses |
| "Pull emails about <topic>" | `--query "subject:<topic> OR body:<topic>"` (Gmail query syntax) |
| "Pull last 30 days" | `--since YYYY/MM/DD` (concrete date 30 days ago — Gmail's native `after:` syntax; relative strings like `30d` build a broken `after:30d` and return nothing) |
| "Pull this specific thread" | `--query "rfc822msgid:<msg-id>"` — `--query` takes **raw Gmail search syntax** only (e.g. `from:alice@example.com subject:invoice`). If the user gives you a Gmail search URL, extract the underlying query string from it before passing — don't paste the URL. |

Always run with `--dry-run` first when the scope might be broader than the
user expected. Cap with `--max 100` for safety. Note: **drafts are now
included** (Q1-A change) — they ingest with `documents.draft=TRUE` and are
hidden from `brain ui`'s browseable views but visible to `brain search` / `brain recall` /
`brain show`.

The summary line is `N ingested (M draft)` when drafts are present.
