---
name: Bug report
about: Report something that isn't working as expected
title: "[bug] "
labels: bug
assignees: ''
---

## Describe the bug

A clear and concise description of what the bug is.

## To reproduce

Steps to reproduce the behavior:

1. Run `brain ...`
2. ...
3. See error

## Expected behavior

What you expected to happen instead.

## `brain doctor` output

Paste the full output of `brain doctor` (redact any personal paths or data):

```
<paste `brain doctor` output here>
```

## Environment

- **OS:** (e.g. macOS 14.5, Ubuntu 22.04, Windows via WSL2 + Ubuntu)
- **Python version:** (output of `python3 --version`)
- **Install method / profile:** (pipx one-liner · `pip install -e ".[dev]"` dev install · `brain setup --profile minimal|standard|full`)
- **Embedder backend (`BRAIN_EMBEDDER`):** (arctic · voyage · qwen3 · none)
- **brain version:** (from `pip show secondbrain-py`, or the git commit SHA)

## Logs / traceback

```
<paste any relevant error output or stack trace here>
```

## Additional context

Anything else that might help.

> Reminder: this is a public repo. Do **not** paste real personal data — names,
> email addresses, meeting attendees, or document/transcript bodies. Redact or
> use synthetic values.
