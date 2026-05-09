// Unit tests for quartz_overrides/quartz/processors/parser_cache.ts
//
// Run with:
//   /Users/mshtawythug/brain-vault/.quartz/node_modules/.bin/tsx --test \
//     tests/test_quartz_parser_cache.ts
//
// The module under test uses only Node.js built-ins (node:crypto, node:fs,
// node:path) so no Quartz installation is required to run these tests.

import test, { describe, beforeEach, afterEach } from "node:test"
import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import { existsSync, mkdtempSync, rmSync, writeFileSync, mkdirSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, dirname } from "node:path"
import {
  CACHE_VERSION,
  cacheKey,
  cachePath,
  getCached,
  putCached,
} from "../quartz_overrides/quartz/processors/parser_cache"

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeTmpDir(): string {
  return mkdtempSync(join(tmpdir(), "brain-parser-cache-test-"))
}

// Minimal MDAST-shaped object that is JSON-round-trip safe.
const dummyAst = {
  type: "root",
  children: [
    {
      type: "paragraph",
      children: [{ type: "text", value: "hello world" }],
    },
  ],
}

const dummyData = {
  slug: "docs/test",
  filePath: "/vault/docs/test.md",
  relativePath: "docs/test.md",
  frontmatter: { title: "Test" },
}

// ---------------------------------------------------------------------------
// cacheKey
// ---------------------------------------------------------------------------

describe("cacheKey", () => {
  test("returns a 64-char lowercase hex string (sha256)", () => {
    const key = cacheKey(Buffer.from("content"), "some-slug")
    assert.match(key, /^[0-9a-f]{64}$/)
  })

  test("same bytes + same slug → same key (deterministic)", () => {
    const bytes = Buffer.from("hello")
    const key1 = cacheKey(bytes, "slug")
    const key2 = cacheKey(bytes, "slug")
    assert.equal(key1, key2)
  })

  test("different bytes → different key", () => {
    assert.notEqual(cacheKey(Buffer.from("aaa"), "slug"), cacheKey(Buffer.from("bbb"), "slug"))
  })

  test("same bytes, different slug → different key", () => {
    const bytes = Buffer.from("content")
    assert.notEqual(cacheKey(bytes, "slug-a"), cacheKey(bytes, "slug-b"))
  })

  test("CACHE_VERSION is incorporated: synthetic version bump changes key", () => {
    // Recompute a key manually with a different version to verify the version
    // is baked into the hash, making bumping CACHE_VERSION invalidate all entries.
    const bytes = Buffer.from("same content")
    const slug = "same-slug"
    const currentKey = cacheKey(bytes, slug)

    // Build a key as if CACHE_VERSION were CACHE_VERSION+1
    const hash = createHash("sha256")
    const vbuf = Buffer.allocUnsafe(4)
    vbuf.writeUInt32BE(CACHE_VERSION + 1, 0)
    hash.update(vbuf)
    hash.update(slug, "utf8")
    hash.update(bytes)
    const futureVersionKey = hash.digest("hex")

    assert.notEqual(currentKey, futureVersionKey)
  })
})

// ---------------------------------------------------------------------------
// cachePath
// ---------------------------------------------------------------------------

describe("cachePath", () => {
  test("returns sharded path under cacheDir", () => {
    const key = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    const p = cachePath("/cache", key)
    // Shard dirs are first two pairs of hex chars
    assert.ok(p.includes("/ab/cd/"), `expected /ab/cd/ in "${p}"`)
    assert.ok(p.endsWith(`/${key}.json`))
  })

  test("two keys with same first 4 chars land in same shard directory", () => {
    const key1 = "1234aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    const key2 = "1234bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    const p1 = cachePath("/cache", key1)
    const p2 = cachePath("/cache", key2)
    assert.equal(dirname(p1), dirname(p2))
  })

  test("two keys with different first 4 chars land in different shard directories", () => {
    const key1 = "aa00aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    const key2 = "bb11bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    const p1 = cachePath("/cache", key1)
    const p2 = cachePath("/cache", key2)
    assert.notEqual(dirname(p1), dirname(p2))
  })
})

// ---------------------------------------------------------------------------
// getCached / putCached (round-trip)
// ---------------------------------------------------------------------------

describe("getCached + putCached", () => {
  let tmpDir: string

  beforeEach(() => {
    tmpDir = makeTmpDir()
  })

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true })
  })

  test("miss: returns null when no entry exists", () => {
    const key = cacheKey(Buffer.from("bytes"), "slug")
    const result = getCached(tmpDir, key)
    assert.equal(result, null)
  })

  test("hit: getCached returns an object equal to what putCached stored", () => {
    const bytes = Buffer.from("# Hello\n\nworld")
    const slug = "notes/hello"
    const key = cacheKey(bytes, slug)
    const entry = { version: CACHE_VERSION, slug, ast: dummyAst, data: dummyData }

    putCached(tmpDir, key, entry)
    const retrieved = getCached<typeof entry>(tmpDir, key)

    assert.ok(retrieved !== null)
    assert.deepEqual(retrieved, entry)
  })

  test("hit: ast is deep-equal after JSON round-trip", () => {
    const bytes = Buffer.from("content")
    const slug = "docs/test"
    const key = cacheKey(bytes, slug)
    putCached(tmpDir, key, { version: CACHE_VERSION, slug, ast: dummyAst, data: {} })

    const retrieved = getCached<{ ast: typeof dummyAst }>(tmpDir, key)
    assert.ok(retrieved !== null)
    assert.deepEqual(retrieved.ast, dummyAst)
  })

  test("miss after version mismatch: entry at future-version key not found by current key", () => {
    const bytes = Buffer.from("some markdown")
    const slug = "docs/test"

    // Compute a key as if CACHE_VERSION were CACHE_VERSION+1 (simulates a stale
    // entry written by a future build using a bumped constant).
    const hash = createHash("sha256")
    const vbuf = Buffer.allocUnsafe(4)
    vbuf.writeUInt32BE(CACHE_VERSION + 1, 0)
    hash.update(vbuf)
    hash.update(slug, "utf8")
    hash.update(bytes)
    const futureKey = hash.digest("hex")

    // Store at future-version key
    putCached(tmpDir, futureKey, { version: CACHE_VERSION + 1, slug, ast: dummyAst, data: {} })

    // Current-version key → different path → clean miss
    const currentKey = cacheKey(bytes, slug)
    assert.notEqual(currentKey, futureKey)
    assert.equal(getCached(tmpDir, currentKey), null)
  })

  test("putCached creates intermediate shard directories", () => {
    const key = cacheKey(Buffer.from("x"), "y")
    const p = cachePath(tmpDir, key)
    putCached(tmpDir, key, { v: 1 })
    assert.ok(existsSync(p), `expected cache file at ${p}`)
  })

  test("putCached is idempotent: writing same key twice keeps the value", () => {
    const bytes = Buffer.from("data")
    const slug = "test"
    const key = cacheKey(bytes, slug)
    const entry = { version: CACHE_VERSION, slug, ast: dummyAst, data: {} }

    putCached(tmpDir, key, entry)
    putCached(tmpDir, key, entry) // second write — should not throw

    const retrieved = getCached<typeof entry>(tmpDir, key)
    assert.deepEqual(retrieved, entry)
  })

  test("corrupt JSON: getCached returns null and emits warning", () => {
    // Simulate a truncated write (e.g. kill -9 mid-write or disk full).
    // getCached should treat this as a soft miss, not crash the build.
    const bytes = Buffer.from("valid content")
    const slug = "docs/corrupt"
    const key = cacheKey(bytes, slug)
    const p = cachePath(tmpDir, key)
    mkdirSync(dirname(p), { recursive: true })
    writeFileSync(p, "{ not json") // deliberately invalid JSON

    // Capture console.warn output to verify the warning is emitted
    const warnings: string[] = []
    const origWarn = console.warn
    console.warn = (...args: unknown[]) => warnings.push(args.join(" "))
    try {
      const result = getCached(tmpDir, key)
      assert.equal(result, null)
      assert.ok(warnings.length > 0, "expected a warning to be emitted")
      assert.ok(warnings[0].includes("[parser_cache]"), `warning should mention [parser_cache], got: ${warnings[0]}`)
      assert.ok(warnings[0].includes("corrupt"), `warning should mention corrupt, got: ${warnings[0]}`)
    } finally {
      console.warn = origWarn
    }
  })

  test("getCached rethrows unexpected errors (not ENOENT)", () => {
    // Create the shard dir but make the cache file a directory (unreadable as file)
    const key = cacheKey(Buffer.from("bad"), "slug")
    const p = cachePath(tmpDir, key)
    mkdirSync(p, { recursive: true }) // p is a directory, not a .json file

    assert.throws(
      () => getCached(tmpDir, key),
      (err: unknown) => {
        assert.ok(err instanceof Error)
        const code = (err as NodeJS.ErrnoException).code
        // Node raises EISDIR when reading a directory as a file
        assert.ok(code !== "ENOENT", `Expected non-ENOENT error, got ${code}`)
        return true
      },
    )
  })
})
