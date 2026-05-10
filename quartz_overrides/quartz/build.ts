// Brain wiki overlay — quartz/build.ts
//
// This file shadows the upstream ``~/brain-vault/.quartz/quartz/build.ts``.
// All upstream functionality is preserved verbatim. The only addition is the
// ``writeFastpathArtifacts`` hook at the end of ``buildQuartz``, which writes
// ``manifest.json`` + ``contentmap.json`` under
// ``<vault>/.quartz/.cache/fastpath/`` after a successful full build.
//
// T2 additions (Plan B v3 — per-file emit fast path):
//   - ``_deriveOutputPath(slug)``       — uniform slug→html output path rule.
//   - ``writeFastpathArtifacts(ctx, filteredContent)`` — writes manifest +
//     contentmap (atomic: write tmp → rename). Called after ``emitContent``
//     succeeds, wrapped in try/catch so artifact failure NEVER crashes the build.
//   - ``QUARTZ_PARENT_BUILD_ID`` env var (Strategy A from T0): Python's
//     ``build_swap`` passes this; the overlay reads it and stamps both artifacts.
//     If the env var is absent, artifact write is skipped (non-fatal warning only).
//
// Spec: docs/plans/2026-05-09-plan-b-per-file-emit.md  (T2 section)
// Companion: quartz_overrides/quartz/util/fastpath_manifest.ts

import sourceMapSupport from "source-map-support"
sourceMapSupport.install(options)
import path from "path"
import { PerfTimer } from "./util/perf"
import { rm } from "fs/promises"
import { mkdirSync, renameSync, writeFileSync } from "node:fs"
import { randomUUID } from "node:crypto"
import { GlobbyFilterFunction, isGitIgnored } from "globby"
import { styleText } from "util"
import { parseMarkdown } from "./processors/parse"
import { filterContent } from "./processors/filter"
import { emitContent } from "./processors/emit"
import cfg from "../quartz.config"
import { FilePath, joinSegments, slugifyFilePath } from "./util/path"
import chokidar from "chokidar"
import { ProcessedContent } from "./plugins/vfile"
import { Argv, BuildCtx } from "./util/ctx"
import { glob, toPosixPath } from "./util/glob"
import { trace } from "./util/trace"
import { options } from "./util/sourcemap"
import { Mutex } from "async-mutex"
import { getStaticResourcesFromPlugins } from "./plugins"
import { randomIdNonSecure } from "./util/random"
import { ChangeEvent } from "./plugins/types"
import { minimatch } from "minimatch"
import {
  _atomicWriteJson,
  computeFingerprint,
  writeManifest,
  FINGERPRINT_VERSION,
} from "./util/fastpath_manifest"
import type { Manifest, SlugEntry } from "./util/fastpath_manifest"

// ---------------------------------------------------------------------------
// Shared types (same as upstream)
// ---------------------------------------------------------------------------

type ContentMap = Map<
  FilePath,
  | {
      type: "markdown"
      content: ProcessedContent
    }
  | {
      type: "other"
    }
>

type BuildData = {
  ctx: BuildCtx
  ignored: GlobbyFilterFunction
  mut: Mutex
  contentMap: ContentMap
  changesSinceLastBuild: Record<FilePath, ChangeEvent["type"]>
  lastBuildMs: number
}

// ---------------------------------------------------------------------------
// T2 additions — fastpath artifact helpers
// ---------------------------------------------------------------------------

/**
 * Derive the HTML output path for a Quartz slug.
 *
 * All Quartz pages use ``slug + ".html"`` as their output path, confirmed by
 * ``plugins/emitters/helpers.ts:write()`` which calls
 * ``joinSegments(ctx.argv.output, slug + ext)`` where ``ext = ".html"``.
 *
 * Examples:
 *   - ``"index"``        → ``"index.html"``        (root page)
 *   - ``"notes/my-doc"`` → ``"notes/my-doc.html"`` (regular page)
 *   - ``"folder/index"`` → ``"folder/index.html"`` (folder index)
 *   - ``"tags/foo"``     → ``"tags/foo.html"``     (tag page)
 */
function _deriveOutputPath(slug: string): string {
  return slug + ".html"
}

/**
 * Serialise a Date instance (or string/null) to an ISO string or null.
 *
 * Mirrors the rehydration done by ``parse.ts:rehydrateDates`` in reverse:
 * Date → string so the contentmap is fully JSON-safe.
 */
function _serializeDate(v: unknown): string | null {
  if (v instanceof Date) return v.toISOString()
  if (typeof v === "string" && v) return v
  return null
}

/**
 * Write fastpath artifacts (manifest.json + contentmap.json) after a
 * successful full build.
 *
 * Reads ``QUARTZ_PARENT_BUILD_ID`` env var (Strategy A from T0).  If the
 * var is absent or empty, logs a warning and returns without writing —
 * the build itself is unaffected; the next edit will trigger a full build.
 *
 * Both files are written atomically (write-tmp + renameSync).
 *
 * @param ctx             The build context (provides argv.directory for vault path).
 * @param filteredContent The filtered markdown content from the full build.
 */
async function writeFastpathArtifacts(
  ctx: BuildCtx,
  filteredContent: ProcessedContent[],
): Promise<void> {
  const parentBuildId = process.env["QUARTZ_PARENT_BUILD_ID"] ?? ""
  if (!parentBuildId) {
    console.warn(
      "wiki: QUARTZ_PARENT_BUILD_ID not set — skipping fastpath artifact write " +
        "(next edit will trigger a full build)",
    )
    return
  }

  const { argv } = ctx

  // Fastpath dir lives under the vault's .quartz directory (NOT the build dir).
  // argv.directory = vault root; argv.output = build output dir.
  // Spec: docs/audits/2026-05-09-fastpath-t0.md F5.3.
  const fastpathDir = path.join(argv.directory, ".quartz", ".cache", "fastpath")
  mkdirSync(fastpathDir, { recursive: true })

  // -------------------------------------------------------------------------
  // 1. Build manifest.json — per-slug fingerprint + output/source paths.
  // -------------------------------------------------------------------------

  const slugEntries: Record<string, SlugEntry> = {}
  for (const pc of filteredContent) {
    const [, vfile] = pc
    const slug = vfile.data.slug!
    const sourcePath = String(vfile.data.relativePath ?? "")
    const outputPath = _deriveOutputPath(slug)
    slugEntries[slug] = {
      fingerprint: computeFingerprint(pc, { sourcePath, outputPath }),
      output_path: outputPath,
      source_path: sourcePath,
    }
  }

  const manifest: Manifest = {
    version: FINGERPRINT_VERSION,
    parent_build_id: parentBuildId,
    built_at_ms: Date.now(),
    slugs: slugEntries,
  }
  // manifest.json is written LAST (after contentmap.json) so it acts as a
  // commit marker — see write-order comment below.

  // -------------------------------------------------------------------------
  // 2. Build contentmap.json — metadata-only (hastRoot = null, htmlAst excluded).
  //
  //    Per T0 Amendment 1: full HAST trees are 273 MB on a 1100-doc vault.
  //    Metadata-only (no hastRoot, no htmlAst) drops this to ~1.3 MB and
  //    round-trips in <5 ms. The fast path re-parses the changed file's HAST
  //    from source; unchanged files don't need HAST because the emitter only
  //    accesses ``vfile.data`` (frontmatter, links, blocks, dates, etc.).
  //
  //    blocks MUST be included: block-ref transclusions are resolved via
  //    ``allFiles[target].data.blocks[blockId]`` at render time (M3 confirmed).
  // -------------------------------------------------------------------------

  type SerializedDates = {
    created: string | null
    modified: string | null
    published: string | null
  }

  type ContentMapVFileData = {
    frontmatter: Record<string, unknown> | null
    links: string[] | null
    text: string | null
    blocks: Record<string, unknown>
    dates: SerializedDates
    filePath: string
    relativePath: string
    slug: string
    // Extended metadata (Fix #3 — T3/T4 consumers need these without re-parsing).
    description: string | null
    toc: unknown
    collapseToc: boolean | null
    aliases: string[] | null
    hasMermaidDiagram: boolean | null
  }

  type ContentMapEntry = {
    type: "markdown"
    filePath: string
    hastRoot: null
    vfileData: ContentMapVFileData
  }

  type ContentmapEnvelope = {
    /** Must equal FINGERPRINT_VERSION — lets T3/T4 detect stale contentmap. */
    version: number
    /** Matches manifest.parent_build_id — cross-artifact consistency check. */
    parent_build_id: string
    /** Unix epoch milliseconds when this contentmap was written. */
    built_at_ms: number
    /** Per-file content metadata entries (no hastRoot, no htmlAst). */
    entries: ContentMapEntry[]
  }

  const contentMapEntries: ContentMapEntry[] = []
  for (const [, vfile] of filteredContent) {
    const data = vfile.data as Record<string, unknown>

    const rawDates = data["dates"] as Record<string, unknown> | undefined
    const serializedDates: SerializedDates = {
      created: _serializeDate(rawDates?.["created"]),
      modified: _serializeDate(rawDates?.["modified"]),
      published: _serializeDate(rawDates?.["published"]),
    }

    // blocks: Record<string, Element> — HAST Elements are plain objects, JSON-safe.
    // Include even though hastRoot is null (block-ref transclusion requirement, M3).
    const blocks = (data["blocks"] as Record<string, unknown> | undefined) ?? {}

    contentMapEntries.push({
      type: "markdown",
      filePath: String(vfile.data.relativePath ?? ""),
      hastRoot: null,
      vfileData: {
        frontmatter: (data["frontmatter"] as Record<string, unknown> | undefined) ?? null,
        links: (data["links"] as string[] | undefined) ?? null,
        text: (data["text"] as string | undefined) ?? null,
        blocks,
        dates: serializedDates,
        filePath: String(data["filePath"] ?? ""),
        relativePath: String(data["relativePath"] ?? ""),
        slug: String(data["slug"] ?? ""),
        // Extended metadata — T3/T4 consumers read these from the contentmap
        // instead of re-parsing the source file.
        description: typeof data["description"] === "string" ? data["description"] : null,
        toc: data["toc"] ?? null,
        collapseToc: typeof data["collapseToc"] === "boolean" ? data["collapseToc"] : null,
        aliases: Array.isArray(data["aliases"])
          ? (data["aliases"] as unknown[]).map(String)
          : null,
        hasMermaidDiagram:
          typeof data["hasMermaidDiagram"] === "boolean" ? data["hasMermaidDiagram"] : null,
      },
    })
  }

  // -------------------------------------------------------------------------
  // Write order: contentmap FIRST (as content), manifest LAST (as commit marker).
  // T3/T4 must observe: if manifest.json exists, contentmap.json is guaranteed
  // to already be present and consistent.  Writing manifest last enforces this
  // invariant atomically.
  // -------------------------------------------------------------------------

  const contentmapEnvelope: ContentmapEnvelope = {
    version: FINGERPRINT_VERSION,
    parent_build_id: parentBuildId,
    built_at_ms: Date.now(),
    entries: contentMapEntries,
  }
  // contentmap written FIRST — content is available before the commit marker.
  _atomicWriteJson(fastpathDir, "contentmap.json", contentmapEnvelope)

  // manifest written LAST — acts as the commit marker (T3/T4 invariant above).
  writeManifest(fastpathDir, manifest)

  console.log(
    `wiki: fastpath manifest + contentmap written ` +
      `(parent_build_id=${parentBuildId}, slugs=${filteredContent.length})`,
  )
}

// ---------------------------------------------------------------------------
// Upstream buildQuartz (with T2 hook added after emitContent)
// ---------------------------------------------------------------------------

async function buildQuartz(argv: Argv, mut: Mutex, clientRefresh: () => void) {
  const ctx: BuildCtx = {
    buildId: randomIdNonSecure(),
    argv,
    cfg,
    allSlugs: [],
    allFiles: [],
    incremental: false,
  }

  const perf = new PerfTimer()
  const output = argv.output

  const pluginCount = Object.values(cfg.plugins).flat().length
  const pluginNames = (key: "transformers" | "filters" | "emitters") =>
    cfg.plugins[key].map((plugin) => plugin.name)
  if (argv.verbose) {
    console.log(`Loaded ${pluginCount} plugins`)
    console.log(`  Transformers: ${pluginNames("transformers").join(", ")}`)
    console.log(`  Filters: ${pluginNames("filters").join(", ")}`)
    console.log(`  Emitters: ${pluginNames("emitters").join(", ")}`)
  }

  const release = await mut.acquire()
  perf.addEvent("clean")
  await rm(output, { recursive: true, force: true })
  console.log(`Cleaned output directory \`${output}\` in ${perf.timeSince("clean")}`)

  perf.addEvent("glob")
  const allFiles = await glob("**/*.*", argv.directory, cfg.configuration.ignorePatterns)
  const markdownPaths = allFiles.filter((fp) => fp.endsWith(".md")).sort()
  console.log(
    `Found ${markdownPaths.length} input files from \`${argv.directory}\` in ${perf.timeSince("glob")}`,
  )

  const filePaths = markdownPaths.map((fp) => joinSegments(argv.directory, fp) as FilePath)
  ctx.allFiles = allFiles
  ctx.allSlugs = allFiles.map((fp) => slugifyFilePath(fp as FilePath))

  const parsedFiles = await parseMarkdown(ctx, filePaths)
  const filteredContent = filterContent(ctx, parsedFiles)

  await emitContent(ctx, filteredContent)
  console.log(
    styleText("green", `Done processing ${markdownPaths.length} files in ${perf.timeSince()}`),
  )

  // brain T2: write fastpath artifacts (manifest.json + contentmap.json).
  // Non-fatal: any failure logs a warning but does NOT abort the build or swap.
  // QUARTZ_PARENT_BUILD_ID (Strategy A) must be set by Python's build_swap
  // before invoking node; if absent, artifact write is silently skipped.
  try {
    await writeFastpathArtifacts(ctx, filteredContent)
  } catch (err) {
    console.warn("wiki: fastpath artifact write failed (non-fatal):", err)
  }

  release()

  if (argv.watch) {
    ctx.incremental = true
    return startWatching(ctx, mut, parsedFiles, clientRefresh)
  }
}

// setup watcher for rebuilds
async function startWatching(
  ctx: BuildCtx,
  mut: Mutex,
  initialContent: ProcessedContent[],
  clientRefresh: () => void,
) {
  const { argv, allFiles } = ctx

  const contentMap: ContentMap = new Map()
  for (const filePath of allFiles) {
    contentMap.set(filePath, {
      type: "other",
    })
  }

  for (const content of initialContent) {
    const [_tree, vfile] = content
    contentMap.set(vfile.data.relativePath!, {
      type: "markdown",
      content,
    })
  }

  const gitIgnoredMatcher = await isGitIgnored()
  const buildData: BuildData = {
    ctx,
    mut,
    contentMap,
    ignored: (fp) => {
      const pathStr = toPosixPath(fp.toString())
      if (pathStr.startsWith(".git/")) return true
      if (gitIgnoredMatcher(pathStr)) return true
      for (const pattern of cfg.configuration.ignorePatterns) {
        if (minimatch(pathStr, pattern)) {
          return true
        }
      }

      return false
    },

    changesSinceLastBuild: {},
    lastBuildMs: 0,
  }

  const watcher = chokidar.watch(".", {
    awaitWriteFinish: { stabilityThreshold: 250 },
    persistent: true,
    cwd: argv.directory,
    ignoreInitial: true,
  })

  const changes: ChangeEvent[] = []
  watcher
    .on("add", (fp) => {
      fp = toPosixPath(fp)
      if (buildData.ignored(fp)) return
      changes.push({ path: fp as FilePath, type: "add" })
      void rebuild(changes, clientRefresh, buildData)
    })
    .on("change", (fp) => {
      fp = toPosixPath(fp)
      if (buildData.ignored(fp)) return
      changes.push({ path: fp as FilePath, type: "change" })
      void rebuild(changes, clientRefresh, buildData)
    })
    .on("unlink", (fp) => {
      fp = toPosixPath(fp)
      if (buildData.ignored(fp)) return
      changes.push({ path: fp as FilePath, type: "delete" })
      void rebuild(changes, clientRefresh, buildData)
    })

  return async () => {
    await watcher.close()
  }
}

async function rebuild(changes: ChangeEvent[], clientRefresh: () => void, buildData: BuildData) {
  const { ctx, contentMap, mut, changesSinceLastBuild } = buildData
  const { argv, cfg } = ctx

  const buildId = randomIdNonSecure()
  ctx.buildId = buildId
  buildData.lastBuildMs = new Date().getTime()
  const numChangesInBuild = changes.length
  const release = await mut.acquire()

  // if there's another build after us, release and let them do it
  if (ctx.buildId !== buildId) {
    release()
    return
  }

  const perf = new PerfTimer()
  perf.addEvent("rebuild")
  console.log(styleText("yellow", "Detected change, rebuilding..."))

  // update changesSinceLastBuild
  for (const change of changes) {
    changesSinceLastBuild[change.path] = change.type
  }

  const staticResources = getStaticResourcesFromPlugins(ctx)
  const pathsToParse: FilePath[] = []
  for (const [fp, type] of Object.entries(changesSinceLastBuild)) {
    if (type === "delete" || path.extname(fp) !== ".md") continue
    const fullPath = joinSegments(argv.directory, toPosixPath(fp)) as FilePath
    pathsToParse.push(fullPath)
  }

  const parsed = await parseMarkdown(ctx, pathsToParse)
  for (const content of parsed) {
    contentMap.set(content[1].data.relativePath!, {
      type: "markdown",
      content,
    })
  }

  // update state using changesSinceLastBuild
  // we do this weird play of add => compute change events => remove
  // so that partialEmitters can do appropriate cleanup based on the content of deleted files
  for (const [file, change] of Object.entries(changesSinceLastBuild)) {
    if (change === "delete") {
      // universal delete case
      contentMap.delete(file as FilePath)
    }

    // manually track non-markdown files as processed files only
    // contains markdown files
    if (change === "add" && path.extname(file) !== ".md") {
      contentMap.set(file as FilePath, {
        type: "other",
      })
    }
  }

  const changeEvents: ChangeEvent[] = Object.entries(changesSinceLastBuild).map(([fp, type]) => {
    const path = fp as FilePath
    const processedContent = contentMap.get(path)
    if (processedContent?.type === "markdown") {
      const [_tree, file] = processedContent.content
      return {
        type,
        path,
        file,
      }
    }

    return {
      type,
      path,
    }
  })

  // update allFiles and then allSlugs with the consistent view of content map
  ctx.allFiles = Array.from(contentMap.keys())
  ctx.allSlugs = ctx.allFiles.map((fp) => slugifyFilePath(fp as FilePath))
  let processedFiles = filterContent(
    ctx,
    Array.from(contentMap.values())
      .filter((file) => file.type === "markdown")
      .map((file) => file.content),
  )

  let emittedFiles = 0
  for (const emitter of cfg.plugins.emitters) {
    // Try to use partialEmit if available, otherwise assume the output is static
    const emitFn = emitter.partialEmit ?? emitter.emit
    const emitted = await emitFn(ctx, processedFiles, staticResources, changeEvents)
    if (emitted === null) {
      continue
    }

    if (Symbol.asyncIterator in emitted) {
      // Async generator case
      for await (const file of emitted) {
        emittedFiles++
        if (ctx.argv.verbose) {
          console.log(`[emit:${emitter.name}] ${file}`)
        }
      }
    } else {
      // Array case
      emittedFiles += emitted.length
      if (ctx.argv.verbose) {
        for (const file of emitted) {
          console.log(`[emit:${emitter.name}] ${file}`)
        }
      }
    }
  }

  console.log(`Emitted ${emittedFiles} files to \`${argv.output}\` in ${perf.timeSince("rebuild")}`)
  console.log(styleText("green", `Done rebuilding in ${perf.timeSince()}`))
  changes.splice(0, numChangesInBuild)
  clientRefresh()
  release()
}

export default async (argv: Argv, mut: Mutex, clientRefresh: () => void) => {
  try {
    return await buildQuartz(argv, mut, clientRefresh)
  } catch (err) {
    trace("\nExiting Quartz due to a fatal error", err as Error)
  }
}
