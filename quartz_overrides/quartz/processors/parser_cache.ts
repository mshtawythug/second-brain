// Pure file-IO parser result cache for the Quartz incremental-build pipeline.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/processors/parser_cache.ts` by
// `brain vault render --overlay`. It does NOT compile or run from the
// brain repo itself; it is pure Node.js with no Quartz-internal imports
// so it can also be unit-tested directly from the brain repo with
// `tsx --test tests/test_quartz_parser_cache.ts`.
//
// Cache key: sha256(CACHE_VERSION as 4-byte big-endian || slug as UTF-8 || fileBytes).
// Version baked into key — bumping CACHE_VERSION changes every key and
// therefore makes every existing entry unreachable without deleting them.
// Disk: <cacheDir>/<key[0:2]>/<key[2:4]>/<key>.json (two-level sharding
// keeps any single directory under a few thousand entries at vault scale).
// Atomicity: write to <path>.<pid>.tmp, then fs.renameSync (POSIX-atomic).
// Two workers writing the same key race to the same final bytes (same
// content) so the last rename wins without corruption.
// Eviction: none. Disk grows O(vault size). The entire cache directory
// can be deleted at any time — the next build regenerates it.

import { createHash } from "node:crypto"
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"

export const CACHE_VERSION = 1

// cacheKey computes sha256(version-bytes || slug-utf8 || fileBytes) as lowercase hex.
// CACHE_VERSION is mixed in first so a global version bump changes every key.
export function cacheKey(fileBytes: Buffer, slug: string): string {
  const hash = createHash("sha256")
  const versionBuf = Buffer.allocUnsafe(4)
  versionBuf.writeUInt32BE(CACHE_VERSION, 0)
  hash.update(versionBuf)
  hash.update(slug, "utf8")
  hash.update(fileBytes)
  return hash.digest("hex")
}

// cachePath returns the on-disk JSON path for a given key under cacheDir.
// Two-level shard: <aa>/<bb>/<full-key>.json where aa=key[0:2], bb=key[2:4].
export function cachePath(cacheDir: string, key: string): string {
  return join(cacheDir, key.slice(0, 2), key.slice(2, 4), `${key}.json`)
}

// getCached reads and JSON-parses the cache entry for key, returning null on
// ENOENT (clean miss). Any other error (bad JSON, permission denied, etc.) is
// rethrown so the build surfaces it rather than silently falling back to a
// re-parse that might also fail.
export function getCached<T>(cacheDir: string, key: string): T | null {
  const p = cachePath(cacheDir, key)
  try {
    const raw = readFileSync(p, "utf8")
    return JSON.parse(raw) as T
  } catch (err: unknown) {
    if (err instanceof Error && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT") {
      return null
    }
    throw err
  }
}

// putCached serialises value to JSON and writes it atomically via a tmp file.
// mkdir -p is called on the shard directory so the first write to a shard
// creates the directory tree.
export function putCached<T>(cacheDir: string, key: string, value: T): void {
  const p = cachePath(cacheDir, key)
  mkdirSync(dirname(p), { recursive: true })
  const tmp = `${p}.${process.pid}.tmp`
  writeFileSync(tmp, JSON.stringify(value))
  renameSync(tmp, p)
}
