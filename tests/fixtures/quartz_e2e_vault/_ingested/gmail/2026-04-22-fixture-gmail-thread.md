---
title: Fixture Gmail Thread
date: 2026-04-22
tags: [demo, gmail]
kind: ingested
source: gmail
content_type: email_thread
---

This is a synthetic Gmail thread used by the E2E harness. Its
`source: gmail` frontmatter triggers the 📧 icon on Search rows and
TagContent rows.

## 2026-04-22 14:15 — owner@example.com

Latest message from the owner. It stays expanded in the reading-mode
shape and gives the replies-only runtime a message to mark as mine.

<details>
<summary>2026-04-22 14:00 — alice@example.com</summary>

Earlier reply from Alice. It gives the reading-mode runtime a
non-owner message to filter when replies-only is enabled.

</details>
