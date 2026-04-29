"""Embedded template strings + the Phase 3 renderer.

The strings below are written by ``brain vault init`` into ``_templates/`` on
first run; the user owns them after that and we never overwrite them.

:func:`render_template` is the Phase 3 renderer used by ``brain note new`` and
``brain daily``. It supports a tiny grammar — ``{{name}}`` placeholders only
— so user templates stay readable in any text editor and don't accidentally
acquire surface area we'd have to maintain (no conditionals, no loops, no
filters). Unknown placeholders pass through unchanged.

:func:`list_template_names` enumerates the templates in a vault's
``_templates/`` directory so the CLI can validate ``--template T``.
"""
import re
from pathlib import Path

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

# ``{{ name }}`` — surrounding whitespace inside the braces is tolerated so
# users who write ``{{ title }}`` get the same substitution as ``{{title}}``.
# We deliberately disallow nested braces, dots, and pipes to keep the grammar
# narrow (no risk of clashing with future template extensions).
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

_TEMPLATES_DIRNAME = "_templates"


def render_template(template_text: str, vars: dict[str, str]) -> str:
    """Substitute ``{{name}}`` placeholders in ``template_text`` from ``vars``.

    The grammar is intentionally minimal:

    - Only ``{{name}}`` (or ``{{ name }}`` with optional inner whitespace).
    - ``name`` is a Python-style identifier (``[A-Za-z_][A-Za-z0-9_]*``).
    - Unknown placeholders are left **as-is** in the output — they're not an
      error. This lets a template keep ``{{some_future_var}}`` around without
      forcing the renderer to know about every variable a future call site
      might pass.
    - No conditionals, no loops, no filters. If a template needs more, the
      user is better served by a real templating engine outside the brain.

    The function is pure: same inputs → same output, no I/O, no datetime
    side effects (callers compute ``{{date}}`` / ``{{datetime}}`` themselves
    and pass them in via ``vars``).
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name in vars:
            return vars[name]
        # Preserve the original token verbatim — including any inner whitespace
        # the user typed — so the template round-trips losslessly when no
        # value is supplied.
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, template_text)


def list_template_names(vault_path: Path) -> list[str]:
    """Return the ``.md`` template basenames (without extension) under ``_templates/``.

    Used by the CLI to validate ``--template T`` and (eventually) to print a
    helpful "available templates: …" diagnostic when the user asks for one
    that doesn't exist.

    Returns an empty list if the vault doesn't have a ``_templates/`` directory
    yet (e.g. the user pointed ``--vault`` at an unmanaged folder). The caller
    decides whether the empty case is an error — for ``brain note new`` it is
    (we suggest ``brain vault init``); for diagnostics it isn't.

    Iteration order is sorted so output is deterministic across platforms.
    """
    templates_dir = vault_path / _TEMPLATES_DIRNAME
    if not templates_dir.is_dir():
        return []
    names = [
        p.stem
        for p in sorted(templates_dir.iterdir())
        if p.is_file() and p.suffix == ".md"
    ]
    return names
