# Git hooks — PII / secret gate

A `pre-commit` hook that blocks a commit whose staged, added lines contain real
PII, real customer/employer/company names, internal project codenames, secrets,
or real transcript/email content. It complements the repo's synthetic-only test
policy (CLAUDE.md rule 15) with a hard gate at `git commit`.

## How it works

Three stages, in cost order. The first runs on the **staged file list**; the
other two run on the **added lines** of `git diff --cached`.

0. **Lint + type gate** — if any staged file is `*.py`, `ruff check` then
   `mypy src/` run over the **working tree** (not the staged snapshot; CI checks
   the real committed state). Either failing **blocks** the commit. Both tools
   run because neither is sufficient alone — the measured breakdown is in the
   hook's own header comment.

   **Tool resolution matters here, and was wrong until 2026-08-20.** Both tools
   are resolved via `resolve_tool`, which looks in `<repo>/.venv/bin` first,
   then `$VIRTUAL_ENV/bin`, then `$PATH`. A git hook inherits the environment of
   the `git commit` process, so a shell that never activated the venv has no
   `.venv/bin` on `$PATH`; the previous bare `command -v mypy` therefore failed
   and the type gate degraded to a **no-op that still printed like a routine
   skip**. Preferring the repo venv also stops a *globally* installed `ruff`
   (e.g. Homebrew's) from standing in for the one this repo pins. Only if all
   three lookups miss does the hook warn and skip — and then it names where it
   looked.

1. **Deterministic gate** (always runs, even offline): secret/API-key regexes,
   real-looking email addresses (synthetic/example domains excluded), and every
   term in your gitignored denylist.
2. **Semantic pass** via `claude -p`: the diff (with the allowlist in context)
   is sent to Claude, which returns `OK` or `BLOCK: <reason>`. Fails **closed**
   on a transient Claude error; if `claude` is not installed it warns and runs
   the deterministic gate only.

## The two lists

| File | Tracked? | Purpose |
|------|----------|---------|
| `.pii-allowlist.txt` | **committed** | Public/synthetic tokens to IGNORE (false positives). **NEVER real PII.** |
| `.pii-denylist.local.txt` | **gitignored** | YOUR real sensitive terms to ALWAYS block. Copy from `.pii-denylist.local.txt.example`. |

The allowlist is committed, so it must only ever contain public or synthetic
tokens. Real sensitive terms go in the gitignored denylist so they live only on
your machine and never enter git history.

## Install

```bash
git config core.hooksPath scripts/hooks
chmod +x scripts/hooks/pre-commit
cp .pii-denylist.local.txt.example .pii-denylist.local.txt   # then edit with your real terms
```

## Verifying the gate actually gates

A gate nobody has watched block is not a gate (repo memory: "prove the check can
fail"). To check yours, in a scratch clone or throwaway worktree — never by
staging a deliberate defect in a tree other people are working in:

```bash
printf 'def f(a: int) -> int:\n    """Doc."""\n    return a\n\n\nV: int = f("x")\n' > src/brain/_probe.py
git add src/brain/_probe.py && git commit -m "probe"   # must FAIL at the type gate
git reset && rm src/brain/_probe.py
```

Expect `🚫 pre-commit type gate: mypy src/ failed` and a non-zero exit. If you
instead see `'mypy' not found — skipping type check`, read the `Looked in:` line
the hook prints and install the dev extras (`pip install -e '.[dev]'`) into one
of those locations. The same probe with an undefined name instead of a type
error exercises the `ruff` half.

## Bypass (use consciously, only when you are certain the diff is clean)

```bash
git commit --no-verify
```
