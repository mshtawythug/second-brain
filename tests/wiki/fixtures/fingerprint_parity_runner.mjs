#!/usr/bin/env node
/**
 * Parity test runner for the fastpath fingerprint canonical blob.
 *
 * SYNC NOTE: This file mirrors quartz_overrides/quartz/util/fastpath_manifest.ts
 * (and vice-versa). When you change the canonical-blob shape, FINGERPRINT_VERSION,
 * or any STRUCTURAL_FIELDS / ARRAY_FIELDS / BOOL_FIELDS / DATE_FIELDS /
 * STRIP_ASCII_SET constant, edit BOTH files. The static test
 * test_runner_and_ts_declare_identical_constants guards against accidental drift.
 *
 * Reads a JSON payload from stdin:
 *   { slug, source_path, output_path, source_text }
 *
 * Modes (CLI flags):
 *   (default)    Write sha256 hex fingerprint + "\n" to stdout.
 *   --emit-blob  Write JSON {"fingerprint":"<hex>","blob_hex":"<hex>"} + "\n" to stdout.
 *   --session    Read newline-delimited JSON payloads from stdin indefinitely;
 *                write one result line per input line; shut down on {"shutdown":true}.
 *
 * Implements the SAME algorithm as
 * quartz_overrides/quartz/util/fastpath_manifest.ts:computeFingerprintFromSource
 * so the Python parity test (test_fastpath_fingerprint_parity.py) can compare
 * outputs byte-by-byte.
 *
 * Pure Node.js ESM — no TypeScript compilation, no external npm dependencies.
 * Run with: node tests/wiki/fixtures/fingerprint_parity_runner.mjs
 */

import { createHash } from "node:crypto"
import { readFileSync } from "node:fs"
import { createInterface } from "node:readline"

const FINGERPRINT_VERSION = 1

// ---------------------------------------------------------------------------
// Canonical blob encoding (must match _u32be / _encodeSection in TS file)
// ---------------------------------------------------------------------------

function u32be(n) {
  const buf = Buffer.allocUnsafe(4)
  buf.writeUInt32BE(n, 0)
  return buf
}

function encodeSection(s) {
  const encoded = Buffer.from(s, "utf8")
  return Buffer.concat([u32be(encoded.length), encoded])
}

// ---------------------------------------------------------------------------
// github-slugger v2 reimplementation (inline, no external dependency).
// Must match _STRIP_ASCII_SET and _slugNormalize in fastpath_manifest.ts
// and _normalize in src/brain/wiki/_github_slugger.py.
// ---------------------------------------------------------------------------

const STRIP_ASCII_SET = new Set([
  "\\", "'", '"', "!", "#", "$", "%", "&", "(", ")", "*", "+",
  ",", ".", "/", ":", ";", "<", "=", ">", "?", "@", "[", "]",
  "^", "`", "{", "|", "}", "~",
])

function slugNormalize(text) {
  text = text.normalize("NFC").toLowerCase().trim()
  const result = []
  for (const ch of text) {
    const cp = ch.codePointAt(0)
    if (STRIP_ASCII_SET.has(ch)) continue
    if (cp >= 0x2000 && cp <= 0x206f) continue
    if (cp >= 0x2e00 && cp <= 0x2e7f) continue
    result.push(ch)
  }
  return result.join("").replace(/\s+/g, "-")
}

class Slugger {
  constructor() { this.seen = new Map() }
  slug(text) {
    const base = slugNormalize(text)
    if (!this.seen.has(base)) {
      this.seen.set(base, 0)
      return base
    }
    const count = this.seen.get(base) + 1
    this.seen.set(base, count)
    return `${base}-${count}`
  }
}

// ---------------------------------------------------------------------------
// YAML frontmatter tag extraction
// ---------------------------------------------------------------------------

/**
 * Parse YAML-only tags from raw file source.
 * Returns null when the tags: key is ABSENT (not in YAML at all).
 * Returns [] when tags: is present but empty.
 * Returns [tag, ...] when tags are declared.
 * This null-vs-[] distinction is critical for frontmatter blob parity with Python.
 */
function parseYamlTags(fileSource) {
  const blockMatch = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(fileSource)
  if (!blockMatch) return null  // no frontmatter → tags key absent
  const yaml = blockMatch[1]

  // Inline array: tags: [a, b] or tags: []
  const inlineArr = /^tags:\s*\[([^\]]*)\]/m.exec(yaml)
  if (inlineArr) {
    return inlineArr[1]
      .split(",")
      .map(s => s.trim().replace(/^['"]|['"]$/g, ""))
      .filter(Boolean)
  }

  // Block list
  const blockListMatch = /^tags:\s*$/m.exec(yaml)
  if (blockListMatch) {
    const after = yaml.slice(blockListMatch.index + blockListMatch[0].length)
    const items = []
    for (const line of after.split("\n")) {
      const m = /^[ \t]*-\s*(.+)/.exec(line)
      if (m) items.push(m[1].trim().replace(/^['"]|['"]$/g, ""))
      else if (line.trim() !== "" && !/^[ \t]/.test(line)) break
    }
    return items  // may be [] if block was empty
  }

  // Scalar: tags: single-tag
  const scalar = /^tags:\s+([^\[\r\n].+)/m.exec(yaml)
  if (scalar) {
    const val = scalar[1].trim().replace(/^['"]|['"]$/g, "")
    return val ? [val] : []
  }

  // tags: key not found in YAML block
  return null
}

// ---------------------------------------------------------------------------
// Minimal YAML parser (structural fields only)
// ---------------------------------------------------------------------------

function parseMinimalYaml(yamlText) {
  const result = {}
  const lines = yamlText.split("\n")
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const keyMatch = /^([a-zA-Z_][a-zA-Z0-9_-]*):\s*(.*)$/.exec(line)
    if (!keyMatch) { i++; continue }
    const key = keyMatch[1]
    const rest = keyMatch[2].trim()

    if (rest === "") {
      const items = []
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
      result[key] = items.length > 0 ? items : null
      i = j
      continue
    } else if (rest.startsWith("[")) {
      const arrMatch = /^\[([^\]]*)\]/.exec(rest)
      if (arrMatch) {
        result[key] = arrMatch[1].split(",").map(s => s.trim().replace(/^['"]|['"]$/g, "")).filter(Boolean)
      }
    } else if (rest === "true" || rest === "True" || rest === "TRUE") {
      result[key] = true
    } else if (rest === "false" || rest === "False" || rest === "FALSE") {
      result[key] = false
    } else if (rest === "null" || rest === "~" || rest === "Null") {
      result[key] = null
    } else {
      result[key] = rest.replace(/^['"]|['"]$/g, "")
    }
    i++
  }
  return result
}

// ---------------------------------------------------------------------------
// Date normalization (must match _normalizeDateVal in TS / _normalize_date_val in Python)
// ---------------------------------------------------------------------------

function normalizeDateVal(val) {
  if (val === null || val === undefined) return null
  const s = String(val)
  // Normalize midnight datetime strings to date-only to match Python _normalize_date_val.
  // Python: yaml.safe_load("2024-01-10T00:00:00") → datetime.datetime(2024,1,10,0,0,0)
  //         → _normalize_date_val → "2024-01-10" (midnight truncated)
  // TS/MJS: parseMinimalYaml keeps it as a string → must apply the same truncation.
  if (/^\d{4}-\d{2}-\d{2}T00:00:00$/.test(s)) return s.slice(0, 10)
  return s
}

// ---------------------------------------------------------------------------
// Frontmatter JSON blob (must match _buildFrontmatterJson in TS file)
// ---------------------------------------------------------------------------

const STRUCTURAL_FIELDS = [
  "title", "draft", "publish", "tags", "aliases", "permalink", "slug",
  "lang", "cssclasses", "socialImage", "enableToc", "comments", "kind",
  "description", "socialDescription", "date", "created", "modified",
  "updated", "published",
]
const ARRAY_FIELDS = new Set(["tags", "aliases", "cssclasses"])
const BOOL_FIELDS = new Set(["draft", "publish", "enableToc", "comments"])
const DATE_FIELDS = new Set(["date", "created", "modified", "updated", "published"])

function buildFrontmatterJson(fm, yamlTags) {
  const obj = {}
  for (const key of STRUCTURAL_FIELDS) {
    // For tags: use yamlTags (null=absent, []=empty, [...]= values).
    // null is the sentinel for "key not present in frontmatter YAML".
    const rawVal = key === "tags" ? yamlTags : fm[key]

    if (ARRAY_FIELDS.has(key)) {
      // null (absent) or undefined → JSON null
      if (rawVal === undefined || rawVal === null) {
        obj[key] = null
      } else {
        const arr = Array.isArray(rawVal)
          ? rawVal.map(String).filter(Boolean)
          : typeof rawVal === "string" ? [rawVal] : []
        obj[key] = arr.slice().sort()
      }
    } else if (BOOL_FIELDS.has(key)) {
      obj[key] = rawVal === undefined || rawVal === null ? null : Boolean(rawVal)
    } else if (DATE_FIELDS.has(key)) {
      obj[key] = normalizeDateVal(rawVal === undefined ? null : rawVal)
    } else {
      obj[key] = rawVal === undefined || rawVal === null ? null : String(rawVal)
    }
  }
  return JSON.stringify(obj)
}

// ---------------------------------------------------------------------------
// Body parsing
// ---------------------------------------------------------------------------

function extractInlineTags(body) {
  const re = /(?:^|(?<=\s))#([A-Za-zÀ-ɏͰ-Ͽ一-龥_][^\s#,;!@$%^&*()\[\]{}'\"<>?\/\\|`]*)/gm
  const tags = []
  let m
  while ((m = re.exec(body)) !== null) tags.push(m[1])
  return tags
}

function extractWikilinks(body) {
  const re = /(?<!!)\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]/g
  const targets = []
  let m
  while ((m = re.exec(body)) !== null) {
    const t = m[1].trim()
    if (t) targets.push(t)
  }
  return targets
}

function extractTransclusions(body) {
  const re = /!\[\[([^\[\]]+?)\]\]/g
  const targets = []
  let m
  while ((m = re.exec(body)) !== null) {
    const t = m[1].trim()
    if (t) targets.push(t)
  }
  return targets
}

function extractBlockRefs(body) {
  const re = /(?<![[\#])\^([A-Za-z0-9][A-Za-z0-9-]*)(?=[ \t]*$)/gm
  const refs = []
  let m
  while ((m = re.exec(body)) !== null) refs.push(m[1])
  return refs
}

function extractHeadingAnchors(body) {
  const re = /^#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?$/gm
  const slugger = new Slugger()
  const anchors = []
  let m
  while ((m = re.exec(body)) !== null) anchors.push(slugger.slug(m[1].trim()))
  return anchors
}

// ---------------------------------------------------------------------------
// Main computation
// ---------------------------------------------------------------------------

/**
 * Build the canonical blob and return it together with its sha256 fingerprint.
 * Used by both the single-shot and --emit-blob paths.
 */
function computeFingerprintAndBlob({ slug, source_path, output_path, source_text }) {
  // Parse frontmatter
  const fmMatch = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(source_text)
  let fm = {}
  let body = source_text
  if (fmMatch) {
    fm = parseMinimalYaml(fmMatch[1])
    body = source_text.slice(fmMatch[0].length)
  }

  const yamlTags = parseYamlTags(source_text)  // null | string[]
  const inlineTags = extractInlineTags(body)
  // yamlTags null means key absent — treat as [] for merging purposes
  const yamlTagsArr = yamlTags ?? []
  const mergedTags = Array.from(new Set([...yamlTagsArr, ...inlineTags])).sort()

  const fmJson = buildFrontmatterJson(fm, yamlTags)
  const tagsStr = mergedTags.join("\n")
  const wikilinksStr = Array.from(new Set(extractWikilinks(body))).sort().join("\n")
  const transclusionsStr = Array.from(new Set(extractTransclusions(body))).sort().join("\n")
  const blockRefsStr = Array.from(new Set(extractBlockRefs(body))).sort().join("\n")
  const headingsStr = extractHeadingAnchors(body).join("\n")

  const blob = Buffer.concat([
    u32be(FINGERPRINT_VERSION),
    encodeSection(slug),
    encodeSection(source_path),
    encodeSection(output_path),
    encodeSection(fmJson),
    encodeSection(tagsStr),
    encodeSection(wikilinksStr),
    encodeSection(transclusionsStr),
    encodeSection(blockRefsStr),
    encodeSection(headingsStr),
  ])

  return { fingerprint: createHash("sha256").update(blob).digest("hex"), blob }
}

function computeFingerprint(params) {
  return computeFingerprintAndBlob(params).fingerprint
}

// ---------------------------------------------------------------------------
// Entry point: read JSON from stdin, write result(s) to stdout
// ---------------------------------------------------------------------------

const _args = process.argv.slice(2)
const _emitBlob = _args.includes("--emit-blob")
const _sessionMode = _args.includes("--session")

function _respond(data) {
  if (_emitBlob) {
    const { fingerprint, blob } = computeFingerprintAndBlob(data)
    process.stdout.write(JSON.stringify({ fingerprint, blob_hex: blob.toString("hex") }) + "\n")
  } else {
    process.stdout.write(computeFingerprint(data) + "\n")
  }
}

if (_sessionMode) {
  // Session mode: read newline-delimited JSON from stdin; write one result per line.
  // Terminates when stdin closes or when a {"shutdown":true} line is received.
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity })
  for await (const line of rl) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const data = JSON.parse(trimmed)
    if (data.shutdown) break
    _respond(data)
  }
} else {
  // Single-shot: read one JSON object from /dev/stdin, write one result.
  const inputData = JSON.parse(readFileSync("/dev/stdin", "utf8"))
  _respond(inputData)
}
