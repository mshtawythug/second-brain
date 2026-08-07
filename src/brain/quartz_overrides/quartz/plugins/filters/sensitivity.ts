import { QuartzFilterPlugin } from "../types"

/**
 * brain-extension (F6): keep `sensitivity: confidential` notes OFF the published site.
 *
 * WHY THIS IS A FILTER AND NOT AN EMITTER TWEAK — measured, not assumed.
 *
 * The first attempt at this boundary extended the `contentIndex` emitter's
 * drop branch (`fm.draft === true || fm.sensitivity === "confidential"`), on
 * the assumption that this was how `draft` quarantines a note. It is not, and
 * a real `npx quartz build` with a confidential fixture proved it: the index
 * entry and `contentBodies/` payload were correctly dropped, and the note's
 * FULL BODY was still published in three other places —
 *
 *   - `<slug>.html`        the rendered page, reachable by direct URL
 *   - `index.xml`          the RSS feed, which is *designed* to be polled
 *   - `tags/<tag>.html`    the tag listing, which is linked from the site
 *
 * `draft` never had that problem because it is filtered HERE, at
 * `shouldPublish`, by upstream's `RemoveDrafts`. A filter runs before any
 * emitter, so returning false removes the file from the build entirely —
 * page, feed, tag pages, index, bodies — rather than from one emitter's
 * output. That is the difference between hiding a document and not
 * publishing it.
 *
 * The general lesson, worth keeping: gating an emitter means enumerating every
 * emitter, and the enumeration was wrong on the first try. Gating publication
 * needs no enumeration.
 *
 * SEPARATE PLUGIN, NOT A CHANGE TO `RemoveDrafts` — deliberately, for two
 * reasons. `RemoveDrafts` is upstream Quartz code, and overlaying it would put
 * us on the wrong side of a future upstream change. More importantly the two
 * flags express different user intent: `draft` means "not ready to show",
 * `sensitivity: confidential` means "must not leak". Sharing a seam is fine;
 * sharing a branch is what let a publish guarantee be assumed rather than
 * checked. Keeping them independent lets either change without entangling the
 * other.
 *
 * Strict string equality mirrors `RemoveDrafts`' `=== true`: a truthy test
 * would also unpublish `sensitivity: normal`, silently emptying the entire
 * site. The `"true"`-string variant `RemoveDrafts` tolerates has no analogue
 * here because the value is a tier name, not a boolean.
 *
 * NOTE ON SCOPE: this stops confidential notes reaching the PUBLISHED site.
 * The note still exists in the vault on disk and in the database — the vault
 * is the user's own working copy (`brain wiki` builds with
 * `--directory <vault>`, i.e. the publish source IS the live vault), so
 * removing it there would break reading it in Obsidian, which is what the
 * vault is for. This is the correct boundary: publish nothing, keep everything
 * locally.
 */
export const RemoveConfidential: QuartzFilterPlugin<{}> = () => ({
  name: "RemoveConfidential",
  shouldPublish(_ctx, [_tree, vfile]) {
    return vfile.data?.frontmatter?.sensitivity !== "confidential"
  },
})
