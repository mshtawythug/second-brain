---
title: Long TOC Doc (Right-Sidebar Regression Canary)
date: 2026-05-08
tags: [demo, harness]
kind: vault
---

This fixture exists for the right-sidebar regression test added on
2026-05-08. The bug it guards against: on long pages with many
headings, the rendered TOC filled the full 100vh-tall right sidebar
and the user could not reach the `brain-related-docs` or `Backlinks`
panels stacked underneath. The fix lives in
`quartz_overrides/quartz/styles/brain/_sidebar.scss` and pins
`overflow-y: auto` on `.sidebar.right`, `flex: 0 0 auto` on every
direct child, and `max-height: 40vh` on the right-sidebar TOC.

The page intentionally carries ≥15 H2/H3 headings so the rendered
table of contents is long enough to trigger the original bug. Stock
Quartz only renders the TOC component when the page has at least one
heading, so the section list below is the canary that exercises the
ranked sidebar layout.

## Section 1 — Overview
Filler paragraph one. The TOC component lists each H2 as a top-level
entry and each H3 as a nested entry, so this list contributes ten
top-level rows to the rendered TOC.

### Subsection 1.1 — Detail
Filler paragraph one-one.

### Subsection 1.2 — Detail
Filler paragraph one-two.

## Section 2 — Architecture
Filler paragraph two.

### Subsection 2.1 — Detail
Filler paragraph two-one.

## Section 3 — Components
Filler paragraph three.

## Section 4 — Data Flow
Filler paragraph four.

## Section 5 — Persistence
Filler paragraph five.

## Section 6 — Search
Filler paragraph six.

## Section 7 — Indexing
Filler paragraph seven.

## Section 8 — Embeddings
Filler paragraph eight.

## Section 9 — Hybrid Ranking
Filler paragraph nine.

## Section 10 — Backlinks Resolution
Filler paragraph ten.

## Section 11 — Derived Edges
Filler paragraph eleven.

## Section 12 — People Hub
Filler paragraph twelve.

## Section 13 — Ingestion
Filler paragraph thirteen.

## Section 14 — Migration Safety
Filler paragraph fourteen.

## Section 15 — Wrap-up
Filler paragraph fifteen.
