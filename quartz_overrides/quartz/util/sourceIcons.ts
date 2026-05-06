// Brain shared source-icon vocabulary — Phase 3.3 of the Wiki UX Overhaul.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/util/sourceIcons.ts` by `brain vault render
// --overlay`. It does NOT compile or run from the brain repo itself;
// imports are resolved by the cloned Quartz workspace.
//
// Why this exists: P3.2 hard-coded the source-icon table at module
// scope inside `Search.tsx` (and a fallback copy inside
// `search.inline.ts`). P3.3 needs the same vocabulary on the
// server-rendered tag-content rows. Rather than copy a third time, we
// factor the table into this small util module so a future ingest
// source (e.g. notion) is a single-line change. Search, TagContent,
// RelatedDocs, and CommandPalette all import this canonical table.

// brain: source-icon mapping. Adding a key here is necessary but not
// sufficient — source chip styles and any source-specific palettes must
// also gain the new entry. Static tests keep the consumers in lockstep.
export const SOURCE_ICONS: Record<string, string> = {
  gmail: "📧",
  krisp: "🎙️",
  slack: "💬",
  manual: "✍️",
  vault: "🌱",
}

// brain: ordered chip vocabulary — render order for any UI surface
// that lists every source. Pinned (vs derived from the index) so the
// rail renders identically when the live corpus is missing a source.
export const SOURCE_CHIP_ORDER: ReadonlyArray<keyof typeof SOURCE_ICONS> = [
  "krisp",
  "slack",
  "gmail",
  "manual",
  "vault",
] as const

// brain: fall back to a slug-based source classification when the
// emitter's frontmatter lift didn't graft a `source` field (e.g.
// legacy entries written before P1.5). Mirrors the heuristic the
// contentIndex post-processor uses (`slug.startsWith("_ingested/")`
// ⇒ ingested-tier; the next path segment is the source).
export function inferSource(slug: string, source: string | undefined): string {
  if (typeof source === "string" && source.length > 0) return source
  const match = slug.match(/^_ingested\/([^/]+)\//)
  if (match) return match[1]
  return "vault"
}

// brain: glyph lookup with a "vault" default fallback. Centralised so
// every consumer (Search row, tag-content row, future notebook chip,
// …) renders the same character even when the source is unrecognised.
export function sourceIconFor(source: string): string {
  return SOURCE_ICONS[source] ?? SOURCE_ICONS["vault"]
}
