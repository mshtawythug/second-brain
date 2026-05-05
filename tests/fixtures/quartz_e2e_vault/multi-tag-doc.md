---
title: Multi Tag Doc
date: 2026-05-02
tags: [demo, harness, integration]
kind: vault
---

A second vault-tier doc tagged with `demo`, `harness`, and
`integration`. Renders as a second row on `/tags/demo/` so the
TagContent override has more than one entry to display.

This row is the canary for the "tag page renders multiple rows"
assertion in the E2E harness — TagContent's `<ul class="brain-tag-list">`
must list both this doc and `demo-vault-doc.md`.
