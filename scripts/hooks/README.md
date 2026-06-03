# Git hooks — PII / secret gate

A `pre-commit` hook that blocks a commit whose staged, added lines contain real
PII, real customer/employer/company names, internal project codenames, secrets,
or real transcript/email content. It complements the repo's synthetic-only test
policy (CLAUDE.md rule 15) with a hard gate at `git commit`.

## How it works

Two stages run on the **added lines** of `git diff --cached`:

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

## Bypass (use consciously, only when you are certain the diff is clean)

```bash
git commit --no-verify
```
