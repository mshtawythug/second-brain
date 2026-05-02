// Sample Quartz v4 markdown transformer for brain vaults.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/plugins/transformers/derivedFenceMark.ts`
// by `brain vault render --overlay`. It does NOT compile or run from
// the brain repo itself; the imports below resolve against the
// dependencies Quartz pulls into the cloned workspace via
// `npm install`, not against any package brain ships.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz
// version restructures the transformer plugin shape or the mdast
// types it uses, pull the latest transformer reference from
// https://github.com/jackyzha0/quartz/tree/v4/quartz/plugins/transformers
// and re-apply the brain tweaks flagged below — `// brain:` for
// value/structural choices on upstream-supported fields, and
// `// brain-extension:` for keys/types that don't exist in stock
// Quartz.
//
// What this transformer does, and why: Phase D
// (`src/brain/vault/derived_links/fence.py`) writes a fenced section
// at the bottom of every `_ingested/` mirror file:
//
//     <!-- BRAIN_DERIVED_START -->
//     ## Related (auto-generated, do not edit)
//     - [[partner-stem|Partner Title]] *(same_day_participant)*
//     - [[partner-stem|Partner Title]] *(shared_thread)*
//     <!-- BRAIN_DERIVED_END -->
//
// The brain graph renderer needs to distinguish these "evidence"
// edges from authored wiki-links so it can apply dashed/translucent
// styling and surface the rule in hover tooltips. This transformer
// scans the mdast tree for the BRAIN_DERIVED_START / END html-comment
// markers and stamps each `<a>` link inside the fenced range with
// `data-brain-derived="true"` + `data-brain-rule="<rule>"` (and
// `data-brain-weight` if a weight is encoded — see parser below).
// The custom contentIndex emitter
// (`quartz_overrides/plugins/emitters/contentIndex.ts`) reads these
// HTML attributes back when emitting `static/contentIndex.json` so
// each link record carries `kind: "derived"` + structured rule /
// weight metadata.
//
// brain (Lane B 2026-05-02): also writes a `title` attribute on each
// derived link so the `cursor: help` styling Lane B's `_links.scss`
// applies to derived links surfaces a real tooltip on hover. Format:
// `"Derived: <rule>"` when the bullet emphasis parsed cleanly,
// `"Derived (related)"` as a fallback when no rule was extracted.
// Cross-lane edit (Lane B's CSS depends on this transformer's data).
//
// Registration: this transformer must run AFTER
// `Plugin.ObsidianFlavoredMarkdown()` so that `[[wiki-link]]` syntax
// has already been converted into mdast `link` nodes before we walk
// the tree. The brain `quartz.config.ts` template wires it up in the
// correct slot — see the `transformers: [...]` list there.
//
// brain: marker constants are mirrored verbatim from
// `src/brain/vault/derived_links/fence.py` (the canonical Phase D
// source). TypeScript can't import from Python, so they live in two
// places — keep them in sync. If Phase D ever changes the marker
// shape, update both files together and re-run the overlay's
// parse-smoke test against a freshly-rendered fixture vault.

import type { Root, RootContent, Link, Paragraph, Emphasis } from "mdast"

import { QuartzTransformerPlugin } from "../types"

// brain: kept identical to ``FENCE_START_MARKER`` /
// ``FENCE_END_MARKER`` in ``src/brain/vault/derived_links/fence.py``.
// Detection uses ``includes`` (not equality) so leading/trailing
// whitespace introduced by markdown parsers around the html-comment
// node doesn't break the match.
const FENCE_START_MARKER = "<!-- BRAIN_DERIVED_START -->"
const FENCE_END_MARKER = "<!-- BRAIN_DERIVED_END -->"

// brain-extension: the structured shape we extract from each bullet's
// trailing emphasis. ``rule`` is what fence.py writes today
// (``same_day_participant`` / ``shared_thread``). It defaults to
// undefined so the emitter's classifier can omit the
// ``data-brain-rule`` attribute when the parser couldn't pin a value
// down. Note: a missing ``rule`` does NOT downgrade the link's
// classification — see ``stampDerived`` for the fence-membership
// truth-source rule.
interface BulletMeta {
  rule?: string
}

// brain-extension: parses the trailing-emphasis text of a Phase D
// bullet. Strict — only the shape fence.py actually writes today
// (``*(<rule>)*`` where ``<rule>`` is an identifier) is accepted;
// anything else returns ``{}``. Identifier-shape guard prevents
// matching whole sentences if a future fence accidentally mixes
// prose with the metadata convention.
//
// brain: the fence membership — not this parser's success — is the
// truth-source for ``kind: "derived"``. Links inside the fence whose
// emphasis fails to parse (missing, malformed, mixed prose) still
// get ``data-brain-derived="true"`` stamped on them by
// ``stampDerived``; only ``data-brain-rule`` is omitted. If Phase E
// ever adds a structured ``weight`` to the bullet shape, extend
// this parser and ``BulletMeta`` together — don't pre-build the
// hook.
function parseBulletEmphasis(text: string): BulletMeta {
  const match = text.trim().match(/^\(([A-Za-z_][A-Za-z0-9_-]*)\)$/)
  if (!match) return {}
  return { rule: match[1] }
}

// brain: recursive text collector. mdast emphasis/strong nodes can
// contain mixed text + nested formatting; flattening to a plain
// string is safer than reading ``.children[0].value`` blindly when
// fence.py's output is hand-edited or rendered through alternate
// markdown processors.
function collectText(node: RootContent): string {
  if (node.type === "text") return node.value
  if ("children" in node && Array.isArray(node.children)) {
    return (node.children as RootContent[]).map(collectText).join("")
  }
  return ""
}

// brain: in-place attribute stamper. mdast's standard extension
// point for "attach HTML attributes that survive into hast" is the
// ``data.hProperties`` object — remark-rehype copies its keys onto
// the corresponding hast element's properties, which then become
// real HTML attributes after stringification. The brain emitter
// reads these attributes back via ``classifyLink`` to flag derived
// links in ``contentIndex.json``.
//
// brain: ``data-brain-derived`` is stamped unconditionally for
// every link inside the fence range — fence membership is the
// truth-source for "this is a derived edge," NOT the bullet
// emphasis parse. ``data-brain-rule`` is added only when the parser
// pinned a rule down; bullets with missing/malformed metadata still
// produce ``kind: "derived"`` records (just without a rule field).
//
// brain (Lane B 2026-05-02): also stamp ``title`` so the
// ``cursor: help`` styling on derived links in
// ``brain/styles/_links.scss`` actually surfaces a tooltip on hover.
// Without the title write, the SCSS rule promised an affordance the
// DOM didn't deliver. The fallback string for the rule-less case is
// the literal word "Derived (related)" so screen readers and
// browser tooltips both read sensibly even when the bullet emphasis
// failed to parse.
function stampDerived(link: Link, meta: BulletMeta): void {
  const data = (link.data ??= {}) as Record<string, unknown>
  const props = (data.hProperties ??= {}) as Record<string, string>
  props["data-brain-derived"] = "true"
  if (meta.rule !== undefined) {
    props["data-brain-rule"] = meta.rule
    props["title"] = `Derived: ${meta.rule}`
  } else {
    props["title"] = "Derived (related)"
  }
}

// brain: walks a Phase D bullet's paragraph children to find the
// (link, trailing-emphasis) pair fence.py writes. The bullet shape
// after Obsidian-flavored-markdown processing is:
//
//   listItem
//   └─ paragraph
//      ├─ link        ← [[stem|title]]
//      ├─ text " "
//      └─ emphasis    ← *(<rule>)*
//
// We walk left-to-right so a paragraph that mistakenly contains
// multiple links gets paired correctly: each link inherits the
// emphasis that immediately follows it, falling back to "no rule"
// if none does. Returns the map keyed by mdast link node identity.
function pairLinksAndRules(paragraph: Paragraph): Map<Link, BulletMeta> {
  const result = new Map<Link, BulletMeta>()
  let pendingLink: Link | null = null
  for (const child of paragraph.children) {
    if (child.type === "link") {
      if (pendingLink !== null && !result.has(pendingLink)) {
        // Previous link had no trailing emphasis; commit it as
        // "derived but rule unknown" so the emitter still flags it.
        result.set(pendingLink, {})
      }
      pendingLink = child
      continue
    }
    if (child.type === "emphasis" && pendingLink !== null) {
      const text = collectText(child as Emphasis)
      result.set(pendingLink, parseBulletEmphasis(text))
      pendingLink = null
    }
  }
  if (pendingLink !== null && !result.has(pendingLink)) {
    result.set(pendingLink, {})
  }
  return result
}

// brain: top-level mdast walker. Tracks ``inFence`` state across the
// root's direct children (where the BRAIN_DERIVED_START / END html
// comments live as siblings of the bullet list). Within the fence
// range, recurses into descendants and:
//
//   1. Records every link node so we know which links are derived
//      (kind="derived") regardless of whether their bullet's
//      emphasis was parseable.
//   2. Detects paragraph nodes (the bullet's content node) and pairs
//      each link with its trailing emphasis to extract rule/weight
//      metadata.
//
// Two-pass shape (collect, then stamp) keeps the recursion side-
// effect-free and lets us merge rule/weight metadata onto the
// minimal ``derived=true`` baseline in one place.
function annotateFence(tree: Root): void {
  let inFence = false
  const linksInFence = new Set<Link>()
  const ruleByLink = new Map<Link, BulletMeta>()

  const visit = (node: RootContent | Root): void => {
    if (node.type === "html" && typeof node.value === "string") {
      // brain: html-comment markers are root-level siblings of the
      // bullet list, so toggling ``inFence`` here (rather than on
      // descent) gives the correct enclosing-range semantics. The
      // markers themselves don't carry links, so there's nothing to
      // miss by skipping the recursion below.
      //
      // brain: literal ``<!-- BRAIN_DERIVED_START -->`` text in body
      // prose would also trigger this — vault docs almost never
      // self-document the fence format (the canonical doc lives in
      // ``docs/specs/`` outside the vault), so we accept the
      // false-positive risk rather than wrap the marker in a unique
      // sentinel that fence.py would also need to emit.
      if (!inFence && node.value.includes(FENCE_START_MARKER)) {
        inFence = true
        return
      }
      if (inFence && node.value.includes(FENCE_END_MARKER)) {
        inFence = false
        return
      }
    }

    if (inFence) {
      if (node.type === "link") {
        linksInFence.add(node)
      }
      // brain: paragraph-only — fence.py emits bullets, which become
      // ``listItem > paragraph`` after mdast parsing. If a future
      // fence shape ever inserts a heading or blockquote inside the
      // marker range, links there would still get
      // ``kind: "derived"`` (via ``linksInFence`` above) but without
      // a ``rule`` field. Revisit the pairing logic at that point.
      if (node.type === "paragraph") {
        for (const [link, meta] of pairLinksAndRules(node)) {
          ruleByLink.set(link, meta)
        }
      }
    }

    if ("children" in node && Array.isArray(node.children)) {
      for (const child of node.children as RootContent[]) {
        visit(child)
      }
    }
  }

  visit(tree)

  if (inFence) {
    // brain: defensive — fence.py always emits matched START/END
    // pairs (and ``extract_fence`` in fence.py guards against the
    // same corruption from the other side), but a hand-edited file
    // could leave a doc in-fence forever. Drop everything we
    // collected and surface a console warning rather than silently
    // mis-tag every link below the orphaned START marker.
    console.warn(
      "[derivedFenceMark] unmatched BRAIN_DERIVED_START — discarding fence annotations for this document",
    )
    return
  }

  for (const link of linksInFence) {
    stampDerived(link, ruleByLink.get(link) ?? {})
  }
}

// brain-extension: the transformer plugin itself. Empty options
// object is intentional — the only knobs would be the marker strings
// (which are sourced from fence.py) and the matched-rule allowlist
// (which is enforced upstream by fence.py's ``FENCE_RULES``).
// Keeping the signature ``(opts?: never)`` documents that no
// configuration is expected; if a knob is genuinely needed later,
// widen the type at that point rather than guessing now.
export const DerivedFenceMark: QuartzTransformerPlugin = () => {
  return {
    name: "DerivedFenceMark",
    markdownPlugins() {
      return [
        () => {
          return (tree: Root) => {
            annotateFence(tree)
          }
        },
      ]
    },
  }
}
