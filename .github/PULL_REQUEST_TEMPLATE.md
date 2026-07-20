## Summary

<!-- What does this PR change, and why? -->

## Related issues

<!-- e.g. Closes #123 -->

## Checklist

- [ ] **Tests added/updated** — new behavior is covered; bug fixes include a
      regression test written **red-first** (it fails before the fix, passes after).
- [ ] **Quality gate is green** — `ruff check && mypy src/ && pytest` all pass
      locally, coverage floors met (85% overall / 95% pure logic / 90% ingest /
      85% CLI).
- [ ] **No PII** — no real names, emails, attendees, transcript/email bodies, or
      other personal data in code, fixtures, tests, comments, **or** the commit
      messages / this PR description. Synthetic values only.
- [ ] **Docs updated** — README, `docs/`, and/or `CHANGELOG.md` updated when the
      change is user-facing (new command/flag, changed behavior, new config).
- [ ] **Conventional commits** — commit messages follow `<type>: <description>`
      (`feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `perf` / `ci`).

## Notes for reviewers

<!-- Anything that needs special attention: migrations, schema changes,
     performance considerations, follow-ups deferred to a later PR. -->
