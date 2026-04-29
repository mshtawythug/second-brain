"""Embedded template strings written by ``brain vault init``."""

DAILY_TEMPLATE = """\
---
title: "{{date}}"
tags: [daily]
---

# {{date}}

## Notes

## Tasks

## Reflection
"""

NOTE_TEMPLATE = """\
---
title: "{{title}}"
tags: []
---

# {{title}}
"""

INGESTED_README = """\
# Ingested artifacts

Files in this folder are mirrors of documents in the brain DB whose source of
truth lives elsewhere (Krisp, Slack, Gmail, raw files). They are rewritten by
`brain vault sync` whenever their upstream source is re-ingested.

**Do not edit these files** — your edits will be overwritten on the next
re-ingest. To capture thoughts about an ingested artifact, create a vault-tier
note (anywhere outside `_ingested/`) and link to it with `[[brain:<id-prefix>]]`.
"""

VAULT_README = """\
# Brain vault

This is your second brain's vault. Plain Markdown files are the source of truth
for vault-tier notes; the `brain` CLI keeps a Postgres index in sync.

## Layout

- `_templates/` — note templates (`daily.md`, `note.md`)
- `_attachments/` — binary files referenced by notes
- `_ingested/` — read-only mirrors of DB-authoritative artifacts
- `daily/<YYYY>/<YYYY-MM-DD>.md` — daily notes
- (anything else) — your authored notes

## Frontmatter contract

Every `.md` file has a YAML frontmatter block with at minimum `id` and `title`.
`brain vault sync` auto-assigns `id` on first sight if missing.
"""
