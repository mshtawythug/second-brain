// Brain wiki — fast-path manifest: per-slug structural fingerprint + manifest I/O.
//
// SYNC NOTE: This file mirrors tests/wiki/fixtures/fingerprint_parity_runner.mjs
// (and vice-versa). When you change the canonical-blob shape, FINGERPRINT_VERSION,
// or any _STRUCTURAL_FIELDS / _ARRAY_FIELDS / _BOOL_FIELDS / _DATE_FIELDS /
// _STRIP_ASCII_SET constant, edit BOTH files. The static test
// test_runner_and_ts_declare_identical_constants guards against accidental drift.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/util/fastpath_manifest.ts` by
// `brain vault render --overlay`.
//
// Canonical-blob format: docs/specs/2026-05-09-fastpath-fingerprint.md
//
// The Python counterpart (src/brain/wiki/fastpath_manifest.py) MUST produce
// byte-identical canonical blobs for the same inputs. The parity test at
// tests/wiki/test_fastpath_fingerprint_parity.py enforces this contract.
//
// Usage by T2 (full-build hook):
//   import { computeFingerprint, writeManifest } from "./util/fastpath_manifest"
//   const slugEntries: Record<string, SlugEntry> = {}
//   for (const pc of filteredContent) {
//     const [, vfile] = pc
//     const slug = vfile.data.slug!
//     // relativePath is vault-relative (e.g. "notes/my-note.md"), NOT the full disk path.
//     // outputPath may differ from slug+".html" for folder-index files.
//     const sourcePath = String(vfile.data.relativePath ?? "")
//     const outputPath = slug + ".html"  // T2: may be slug+"/index.html" for folder indexes
//     slugEntries[slug] = {
//       fingerprint: computeFingerprint(pc, { sourcePath, outputPath }),
//       output_path: outputPath,
//       source_path: sourcePath,
//     }
//   }
//   writeManifest(fastpathDir, {
//     version: FINGERPRINT_VERSION,
//     parent_build_id: process.env.QUARTZ_PARENT_BUILD_ID ?? "",
//     built_at_ms: Date.now(),
//     slugs: slugEntries,
//   })

import { createHash, randomUUID } from "node:crypto"
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"
import type { ProcessedContent } from "../plugins/vfile"

export const FINGERPRINT_VERSION: number = 1

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface SlugEntry {
  /** sha256 hex fingerprint of the canonical blob. */
  fingerprint: string
  /** HTML output path relative to the build directory (e.g. "my-note.html"). */
  output_path: string
  /** Markdown source path relative to the vault root (e.g. "notes/my-note.md").
   *  Callers MUST supply this explicitly — use ``vfile.data.relativePath``, NOT
   *  ``vfile.data.filePath`` (which is the full absolute disk path). */
  source_path: string
}

export interface Manifest {
  /** Must equal FINGERPRINT_VERSION at write time. */
  version: number
  /** Build-id of the full build that wrote this manifest (matches current/.build-id). */
  parent_build_id: string
  /** Unix epoch milliseconds when this manifest was written. */
  built_at_ms: number
  /** Per-slug fingerprint entries, keyed by Quartz FullSlug. */
  slugs: Record<string, SlugEntry>
}

// ---------------------------------------------------------------------------
// Structural frontmatter field order (must match Python _STRUCTURAL_FIELD_ORDER).
// ---------------------------------------------------------------------------

const _STRUCTURAL_FIELDS = [
  "title", "draft", "publish", "tags", "aliases", "permalink", "slug",
  "lang", "cssclasses", "socialImage", "enableToc", "comments", "kind",
  "description", "socialDescription", "date", "created", "modified",
  "updated", "published",
] as const

const _ARRAY_FIELDS = new Set(["tags", "aliases", "cssclasses"])
const _BOOL_FIELDS = new Set(["draft", "publish", "enableToc", "comments"])
const _DATE_FIELDS = new Set(["date", "created", "modified", "updated", "published"])

// ---------------------------------------------------------------------------
// Canonical blob encoding helpers
// ---------------------------------------------------------------------------

function _u32be(n: number): Buffer {
  const buf = Buffer.allocUnsafe(4)
  buf.writeUInt32BE(n, 0)
  return buf
}

function _encodeSection(s: string): Buffer {
  const encoded = Buffer.from(s, "utf8")
  return Buffer.concat([_u32be(encoded.length), encoded])
}

// ---------------------------------------------------------------------------
// github-slugger reimplementation (matches npm github-slugger v2.x exactly).
// Inlined so this file has no runtime import that would break when loaded
// outside the Quartz workspace (e.g. via tsx in the parity test runner).
// The Quartz overlay version (with ProcessedContent import) uses the same
// algorithm; the parity runner script also inlines this same code.
// ---------------------------------------------------------------------------

const _STRIP_ASCII_SET = new Set([
  "\\", "'", '"', "!", "#", "$", "%", "&", "(", ")", "*", "+",
  ",", ".", "/", ":", ";", "<", "=", ">", "?", "@", "[", "]",
  "^", "`", "{", "|", "}", "~",
])

function _slugNormalize(text: string): string {
  // NFC + lowercase
  text = text.normalize("NFC").toLowerCase().trim()
  const result: string[] = []
  for (const ch of text) {
    const cp = ch.codePointAt(0)!
    if (_STRIP_ASCII_SET.has(ch)) continue
    if (cp >= 0x2000 && cp <= 0x206f) continue
    if (cp >= 0x2e00 && cp <= 0x2e7f) continue
    result.push(ch)
  }
  return result.join("").replace(/\s+/g, "-")
}

class _Slugger {
  private seen = new Map<string, number>()

  slug(text: string): string {
    const base = _slugNormalize(text)
    if (!this.seen.has(base)) {
      this.seen.set(base, 0)
      return base
    }
    const count = (this.seen.get(base)! + 1)
    this.seen.set(base, count)
    return `${base}-${count}`
  }
}

// ---------------------------------------------------------------------------
// YAML frontmatter tag extraction from raw source
// (needed to get YAML-only tags for SECTION_FRONTMATTER, before OFM merge).
// ---------------------------------------------------------------------------

/**
 * Parse YAML-only tags from raw file source.
 * Returns null when the ``tags:`` key is ABSENT (parity: Python returns null too).
 * Returns [] when tags: is present but empty.
 * Returns [tag, ...] when tags are declared.
 */
function _parseYamlTags(fileSource: string): string[] | null {
  // Extract the YAML block between --- delimiters.
  const blockMatch = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(fileSource)
  if (!blockMatch) return null  // no frontmatter → tags key absent

  const yaml = blockMatch[1]

  // Case 1: inline array — tags: [a, b, c] or tags: []
  const inlineArr = /^tags:\s*\[([^\]]*)\]/m.exec(yaml)
  if (inlineArr) {
    return inlineArr[1]
      .split(",")
      .map((s) => s.trim().replace(/^['"]|['"]$/g, ""))
      .filter(Boolean)
  }

  // Case 2: block list —
  //   tags:
  //     - item
  const blockListMatch = /^tags:\s*$/m.exec(yaml)
  if (blockListMatch) {
    const after = yaml.slice(blockListMatch.index + blockListMatch[0].length)
    const items: string[] = []
    for (const line of after.split("\n")) {
      const m = /^[ \t]*-\s*(.+)/.exec(line)
      if (m) {
        items.push(m[1].trim().replace(/^['"]|['"]$/g, ""))
      } else if (line.trim() !== "" && !/^[ \t]/.test(line)) {
        break
      }
    }
    return items  // may be [] if block was empty
  }

  // Case 3: scalar — tags: single-tag
  const scalar = /^tags:\s+([^\[\r\n].+)/m.exec(yaml)
  if (scalar) {
    const val = scalar[1].trim().replace(/^['"]|['"]$/g, "")
    return val ? [val] : []
  }

  // tags: key not found in YAML block
  return null
}

// ---------------------------------------------------------------------------
// Body extraction: strip YAML frontmatter from raw source markdown.
// Shared by computeFingerprint (ProcessedContent path) and
// computeFingerprintFromSource (raw-source path) so both see identical body text.
// ---------------------------------------------------------------------------

function _extractBody(rawSource: string): string {
  const fmMatch = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(rawSource)
  return fmMatch ? rawSource.slice(fmMatch[0].length) : rawSource
}

// ---------------------------------------------------------------------------
// Date normalization — Date objects from js-yaml → ISO string.
// ---------------------------------------------------------------------------

function _normalizeDateVal(val: unknown): string | null {
  if (val === null || val === undefined) return null
  if (val instanceof Date) {
    // Normalize to Python datetime.isoformat()-compatible format (no timezone suffix,
    // no milliseconds) so TS and Python blobs are byte-identical for the same YAML:
    //   "2024-03-15T00:00:00.000Z" → "2024-03-15"        (midnight UTC = date-only)
    //   "2024-03-15T12:00:00.000Z" → "2024-03-15T12:00:00" (non-midnight datetime)
    // Matches Python _normalize_date_val() output for datetime.date / datetime.datetime.
    const noZone = val.toISOString().replace(/\.000Z$/, "").replace(/Z$/, "")
    return noZone.endsWith("T00:00:00") ? noZone.slice(0, 10) : noZone
  }
  const s = String(val)
  // String dates from _parseMinimalYaml: normalize midnight datetimes to date-only to
  // match Python pyyaml → _normalize_date_val which truncates midnight datetimes.
  // Python: yaml.safe_load("2024-01-10T00:00:00") → datetime.datetime(2024,1,10,0,0,0)
  //         → _normalize_date_val → "2024-01-10" (midnight truncated)
  // TS:     _parseMinimalYaml → "2024-01-10T00:00:00" (string) → must also truncate.
  if (/^\d{4}-\d{2}-\d{2}T00:00:00$/.test(s)) return s.slice(0, 10)
  return s
}

// ---------------------------------------------------------------------------
// Frontmatter JSON blob builder (structural fields, deterministic key order).
// ---------------------------------------------------------------------------

function _buildFrontmatterJson(
  fm: Record<string, unknown>,
  yamlTags: string[] | null,
): string {
  const obj: Record<string, unknown> = {}
  for (const key of _STRUCTURAL_FIELDS) {
    // For tags: use YAML-only (pre-OFM) tags. null = key absent in YAML.
    const rawVal = key === "tags" ? yamlTags : fm[key]

    if (_ARRAY_FIELDS.has(key)) {
      if (rawVal === undefined || rawVal === null) {
        obj[key] = null
      } else {
        const arr = Array.isArray(rawVal)
          ? (rawVal as unknown[]).map(String).filter(Boolean)
          : typeof rawVal === "string"
          ? [rawVal]
          : []
        obj[key] = arr.slice().sort()
      }
    } else if (_BOOL_FIELDS.has(key)) {
      obj[key] = rawVal === undefined || rawVal === null ? null : Boolean(rawVal)
    } else if (_DATE_FIELDS.has(key)) {
      obj[key] = _normalizeDateVal(rawVal)
    } else {
      obj[key] = rawVal === undefined || rawVal === null ? null : String(rawVal)
    }
  }
  return JSON.stringify(obj)
}

// ---------------------------------------------------------------------------
// Body parsing: wikilinks, transclusions, block-refs, headings
// ---------------------------------------------------------------------------

/** Non-transclusion wikilinks: [[target]] or [[target|alias]]. */
const _WIKILINK_RE = /(?<!!)\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]/g

/** Transclusions: ![[target]] including anchors. */
const _TRANSCLUSION_RE = /!\[\[([^\[\]]+?)\]\]/g

/**
 * Block-ref IDs defined in body: ^blockid at end of line.
 * Negative lookbehind for [ and # to avoid matching inside wikilinks/anchors.
 */
const _BLOCKREF_DEF_RE = /(?<![[\#])\^([A-Za-z0-9][A-Za-z0-9-]*)(?=[ \t]*$)/gm

/** ATX headings: # text or ## text (with optional trailing ##). */
const _HEADING_RE = /^#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?$/gm

/** Inline body tags: #tagname preceded by whitespace or start of line. */
const _INLINE_TAG_RE = /(?:^|(?<=\s))#([A-Za-zÀ-ɏͰ-Ͽ一-龥_][^\s#,;!@$%^&*()\[\]{}'\"<>?\/\\|`]*)/gm

function _extractInlineTags(body: string): string[] {
  const tags: string[] = []
  let m: RegExpExecArray | null
  const re = new RegExp(_INLINE_TAG_RE.source, _INLINE_TAG_RE.flags)
  while ((m = re.exec(body)) !== null) {
    tags.push(m[1])
  }
  return tags
}

function _extractWikilinks(body: string): string[] {
  const targets: string[] = []
  let m: RegExpExecArray | null
  const re = new RegExp(_WIKILINK_RE.source, _WIKILINK_RE.flags)
  while ((m = re.exec(body)) !== null) {
    const t = m[1].trim()
    if (t) targets.push(t)
  }
  return targets
}

function _extractTransclusions(body: string): string[] {
  const targets: string[] = []
  let m: RegExpExecArray | null
  const re = new RegExp(_TRANSCLUSION_RE.source, _TRANSCLUSION_RE.flags)
  while ((m = re.exec(body)) !== null) {
    const t = m[1].trim()
    if (t) targets.push(t)
  }
  return targets
}

function _extractBlockRefs(body: string): string[] {
  const refs: string[] = []
  let m: RegExpExecArray | null
  const re = new RegExp(_BLOCKREF_DEF_RE.source, _BLOCKREF_DEF_RE.flags)
  while ((m = re.exec(body)) !== null) {
    refs.push(m[1])
  }
  return refs
}

function _extractHeadingAnchors(body: string): string[] {
  const slugger = new _Slugger()
  const anchors: string[] = []
  let m: RegExpExecArray | null
  const re = new RegExp(_HEADING_RE.source, _HEADING_RE.flags)
  while ((m = re.exec(body)) !== null) {
    anchors.push(slugger.slug(m[1].trim()))
  }
  return anchors
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Compute the structural fingerprint for one ``ProcessedContent`` entry.
 *
 * Called at full-build time over each entry in ``filteredContent``.
 * Reads the pre-OFM raw source from ``vfile.data.rawSource`` — a snapshot
 * set by ``processors/parse.ts`` AFTER text transforms but BEFORE
 * ``processor.run`` (where Quartz's OFM transformer mutates ``vfile.value``,
 * e.g. appending ``|^blockId`` display aliases to transclusion links like
 * ``![[target#^block]]`` → ``![[target#^block|^block]]``).
 *
 * Throws if ``vfile.data.rawSource`` is absent — ``parse.ts`` MUST set the
 * snapshot before any transformer runs.
 *
 * MUST produce byte-identical blobs as the Python counterpart for the same
 * source file. Verified by ``tests/wiki/test_fastpath_fingerprint_parity.py``.
 */
export function computeFingerprint(
  processedContent: ProcessedContent,
  paths: { sourcePath: string; outputPath: string },
): string {
  const [, vfile] = processedContent
  const slug = vfile.data.slug!
  const { sourcePath, outputPath } = paths

  // Use the pre-OFM snapshot stored by processors/parse.ts.
  // parse.ts sets vfile.data.rawSource = rawBytes.toString("utf8") after readFile
  // but before processor.run, ensuring we see the unmodified markdown text.
  const rawSource = (vfile.data as Record<string, unknown>)["rawSource"]
  if (rawSource === undefined || rawSource === null) {
    throw new Error(
      "vfile.data.rawSource missing — processors/parse.ts must snapshot raw source " +
      "before processor.run (OFM transformer); cannot compute fingerprint for slug: " +
      String(slug),
    )
  }
  const fileSource = String(rawSource)

  // Parse structural frontmatter from RAW source, NOT from vfile.data.frontmatter.
  //
  // vfile.data.frontmatter is mutated by Quartz transformers:
  //   - title is set to file.stem for files that have no explicit title field
  //   - date fields are parsed by js-yaml as Date objects using the host machine's
  //     local timezone offset (e.g. PST: "2024-01-10T00:00:00" → T08:00:00Z)
  //
  // Using _parseMinimalYaml on the raw source makes computeFingerprint
  // byte-identical to computeFingerprintFromSource and the Python counterpart,
  // both of which parse from raw text rather than the post-transformer object.
  const fmMatch = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(fileSource)
  const fm: Record<string, unknown> = fmMatch ? _parseMinimalYaml(fmMatch[1]) : {}

  // YAML-only tags (before inline-tag merge) — parse from raw source.
  // null means tags: key is absent from YAML frontmatter.
  const yamlTags = _parseYamlTags(fileSource)

  // Body: raw markdown minus frontmatter.
  // Sourced from vfile.data.rawSource (parse-time snapshot) — NOT from vfile.value
  // (OFM-mutated) or vfile.data.text (rendered plain text that strips OFM syntax).
  const body = _extractBody(fileSource)

  // SECTION_TAGS: merged YAML tags + inline body tags extracted from raw source.
  // We do NOT use vfile.data.frontmatter.tags (post-OFM-transformer) because
  // Quartz's OFM may classify inline content differently from our regex (e.g., it
  // processes wikilink anchors and block-refs in a way that can pollute the tags
  // array). Using _extractInlineTags here makes production computeFingerprint
  // byte-identical to computeFingerprintFromSource and the Python counterpart.
  const inlineTags = _extractInlineTags(body)
  const mergedTagsRaw = Array.from(new Set([...(yamlTags ?? []), ...inlineTags]))

  return _computeFingerprintFromParts(
    slug, sourcePath, outputPath, fm, yamlTags, mergedTagsRaw, body,
  )
}

/**
 * Compute fingerprint from raw source text (no ProcessedContent needed).
 *
 * Used by the parity test runner (no Quartz runtime available). Accepts the
 * raw file source (frontmatter + body) plus the pre-computed slug and paths.
 */
export function computeFingerprintFromSource(params: {
  slug: string
  source_path: string
  output_path: string
  source_text: string
}): string {
  const { slug, source_path, output_path, source_text } = params

  // Parse frontmatter from raw source.
  const fmMatch = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(source_text)
  let fm: Record<string, unknown> = {}
  if (fmMatch) {
    // Minimal YAML parser for structural fields — covers string, bool, array types.
    fm = _parseMinimalYaml(fmMatch[1])
  }
  const body = _extractBody(source_text)

  const yamlTags = _parseYamlTags(source_text)  // null | string[]
  const inlineTags = _extractInlineTags(body)
  // yamlTags null = key absent → treat as [] for merge
  const mergedTagsRaw = Array.from(new Set([...(yamlTags ?? []), ...inlineTags]))

  return _computeFingerprintFromParts(
    slug, source_path, output_path, fm, yamlTags, mergedTagsRaw, body,
  )
}

// ---------------------------------------------------------------------------
// Internal shared computation
// ---------------------------------------------------------------------------

function _computeFingerprintFromParts(
  slug: string,
  sourcePath: string,
  outputPath: string,
  fm: Record<string, unknown>,
  yamlTags: string[] | null,
  mergedTagsRaw: string[],
  body: string,
): string {
  const fmJson = _buildFrontmatterJson(fm, yamlTags)

  const mergedTags = Array.from(new Set(mergedTagsRaw)).sort()
  const tagsStr = mergedTags.join("\n")

  const wikilinks = Array.from(new Set(_extractWikilinks(body))).sort()
  const wikilinksStr = wikilinks.join("\n")

  const transclusions = Array.from(new Set(_extractTransclusions(body))).sort()
  const transclusionsStr = transclusions.join("\n")

  const blockRefs = Array.from(new Set(_extractBlockRefs(body))).sort()
  const blockRefsStr = blockRefs.join("\n")

  const headingsStr = _extractHeadingAnchors(body).join("\n")

  const blob = Buffer.concat([
    _u32be(FINGERPRINT_VERSION),
    _encodeSection(slug),
    _encodeSection(sourcePath),
    _encodeSection(outputPath),
    _encodeSection(fmJson),
    _encodeSection(tagsStr),
    _encodeSection(wikilinksStr),
    _encodeSection(transclusionsStr),
    _encodeSection(blockRefsStr),
    _encodeSection(headingsStr),
  ])
  return createHash("sha256").update(blob).digest("hex")
}

// ---------------------------------------------------------------------------
// Minimal YAML parser for computeFingerprintFromSource (structural fields only)
// ---------------------------------------------------------------------------

function _parseMinimalYaml(yamlText: string): Record<string, unknown> {
  const result: Record<string, unknown> = {}
  const lines = yamlText.split("\n")
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const keyMatch = /^([a-zA-Z_][a-zA-Z0-9_-]*):\s*(.*)$/.exec(line)
    if (!keyMatch) { i++; continue }
    const key = keyMatch[1]
    const rest = keyMatch[2].trim()

    if (rest === "") {
      // Could be a block list: next lines are "  - item"
      const items: string[] = []
      let j = i + 1
      while (j < lines.length) {
        const itemMatch = /^[ \t]*-\s*(.+)/.exec(lines[j])
        if (itemMatch) {
          items.push(itemMatch[1].trim().replace(/^['"]|['"]$/g, ""))
          j++
        } else if (lines[j].trim() === "") {
          j++
        } else {
          break
        }
      }
      if (items.length > 0) {
        result[key] = items
        i = j
        continue
      }
      result[key] = null
    } else if (rest.startsWith("[")) {
      // Inline array: [a, b, c]
      const arrMatch = /^\[([^\]]*)\]/.exec(rest)
      if (arrMatch) {
        result[key] = arrMatch[1]
          .split(",")
          .map((s) => s.trim().replace(/^['"]|['"]$/g, ""))
          .filter(Boolean)
      }
    } else if (rest === "true" || rest === "True" || rest === "TRUE") {
      result[key] = true
    } else if (rest === "false" || rest === "False" || rest === "FALSE") {
      result[key] = false
    } else if (rest === "null" || rest === "~" || rest === "Null") {
      result[key] = null
    } else {
      // String or number — strip quotes, keep as string for structural fields.
      result[key] = rest.replace(/^['"]|['"]$/g, "")
    }
    i++
  }
  return result
}

// ---------------------------------------------------------------------------
// Manifest I/O
// ---------------------------------------------------------------------------

const _MANIFEST_FILENAME = "manifest.json"

/**
 * Atomically write ``data`` as JSON to ``<dir>/<filename>``.
 *
 * Atomic strategy: write to ``<dir>/<filename>.<pid>.<uuid>.tmp``, then
 * ``renameSync`` to the final path. On POSIX, ``rename(2)`` is atomic; on
 * Windows it replaces atomically on NTFS.
 *
 * Shared by ``writeManifest`` and the contentmap writer in ``build.ts`` to
 * avoid duplicating the tmp-write + rename strategy.
 */
export function _atomicWriteJson(dir: string, filename: string, data: unknown): void {
  mkdirSync(dir, { recursive: true })
  const final = join(dir, filename)
  const tmp = join(dir, `${filename}.${process.pid}.${randomUUID()}.tmp`)
  writeFileSync(tmp, JSON.stringify(data), "utf8")
  renameSync(tmp, final)
}

/**
 * Atomically write ``manifest`` to ``<dir>/manifest.json``.
 *
 * Delegates to ``_atomicWriteJson`` for the shared write-tmp + rename strategy.
 */
export function writeManifest(dir: string, manifest: Manifest): void {
  _atomicWriteJson(dir, _MANIFEST_FILENAME, manifest)
}

/**
 * Read and JSON-parse ``<dir>/manifest.json``.
 *
 * Throws on file-not-found, JSON parse error, or type mismatch — callers
 * should catch and treat any error as «manifest unavailable → full build».
 */
export function readManifest(dir: string): Manifest {
  const path = join(dir, _MANIFEST_FILENAME)
  const raw = readFileSync(path, "utf8")
  const data = JSON.parse(raw) as Manifest
  return data
}
