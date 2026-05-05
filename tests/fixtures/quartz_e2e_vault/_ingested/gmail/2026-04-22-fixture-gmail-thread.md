---
title: Fixture Gmail Thread
date: 2026-04-22
tags: [demo, gmail]
kind: ingested
source: gmail
---

This is a synthetic Gmail thread used by the E2E harness. Its
`source: gmail` frontmatter triggers the 📧 icon on Search rows and
TagContent rows.

From: alice@example.com
To: bob@example.com
Subject: Re: Phase 3 demo

This thread exists purely so the harness has a `_ingested/gmail/...`
slug to render — the body content is incidental.
