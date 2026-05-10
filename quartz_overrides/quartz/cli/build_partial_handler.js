// Brain wiki — partial-build handler for the ``build-partial`` CLI subcommand.
//
// This file is a TEMPLATE. It is installed at
// ``<vault>/.quartz/quartz/cli/build_partial_handler.js`` by
// ``brain vault render --overlay``.
//
// Algorithm (per docs/plans/2026-05-09-plan-b-per-file-emit.md T4 section +
// Codex pre-flight contract):
//
//   1. Parse --slug from argv (required).
//   2. Compute fastpathDir = <argv.directory>/.quartz/.cache/fastpath/.
//   3. Load manifest.json (exit 1 if missing/unparseable).
//   4. Load contentmap.json (exit 1 if missing/unparseable).
//   5. Envelope cross-check — enforce BOTH:
//        manifest.version === contentmap.version
//        manifest.parent_build_id === contentmap.parent_build_id
//      Mismatch → exit 2, stderr "envelope mismatch: manifest=<v>/<id> contentmap=<v>/<id>".
//   6. Look up slugEntry = manifest.slugs[slug]. Absent → exit 3.
//   6b. Unsupported-slug guard: slugs starting with "tags/", ending with "/index",
//       or equal to "index" cannot be partially built — ContentPage.partialEmit skips
//       them (contentPage.tsx:112-117) and TagPage/FolderPage are in the deny-list.
//       → exit 6, stderr "scope: full build required for slug=<slug>".
//   7. Find contentEntry in contentmap.entries. Absent → exit 4.
//   8. Compile + run the TypeScript executor (_PARTIAL_BUILD_TS via esbuild stdin).
//      The executor uses the Quartz parse + emit machinery to re-parse the changed
//      file and run eligible emitters (skipping ContentIndex — Option C exclusion).
//      Emitter exception → exit 5 (written inside the executor TS).
//   9. Atomic write order: contentmap.json FIRST, manifest.json SECOND, .build-id LAST.
//      This mirrors the T2 commit-marker pattern (manifest is the commit marker).
//  10. Print success line: "wiki: build-partial slug=<slug> elapsed=<ms>".
//
// Exit codes:
//   1 — manifest.json or contentmap.json missing/unparseable; --slug missing.
//   2 — envelope mismatch (version or parent_build_id differs).
//   3 — slug absent from manifest.slugs.
//   4 — slug absent from contentmap.entries.
//   5 — emitter exception (fail-fast; no artifact written).
//   6 — unsupported-slug scope violation (tag/folder/index pages need full build).
//
// Emitter exclusion (Option C from T0 benchmark M2):
//   ContentIndex, TagPage, and FolderPage are excluded from the partial emitter walk.
//   They are tracked in _PARTIAL_EMIT_EXCLUDED (a Set) inside the executor.
//   No .emit call is made for any excluded emitter.
//
// _atomicWriteJson is defined here in plain JS (mirrors the function in
// util/fastpath_manifest.ts) for the artifact writes performed directly by this handler.
//
// The TypeScript executor (_PARTIAL_BUILD_TS) imports from ../util/fastpath_manifest
// (the T1+T2 helper) for computeFingerprint and FINGERPRINT_VERSION.

import { readFileSync, writeFileSync, mkdirSync, renameSync, statSync } from "node:fs"
import { promises as fsPromises } from "node:fs"
import { join } from "node:path"
import { randomUUID } from "node:crypto"
import path from "path"
import esbuild from "esbuild"
import { sassPlugin } from "esbuild-sass-plugin"
import { cwd } from "./constants.js"

// ---------------------------------------------------------------------------
// Atomic JSON write (mirrors _atomicWriteJson from util/fastpath_manifest.ts).
// Used for contentmap.json and manifest.json writes in this handler.
// ---------------------------------------------------------------------------

function _atomicWriteJson(dir, filename, data) {
  mkdirSync(dir, { recursive: true })
  const final = join(dir, filename)
  const tmp = join(dir, filename + "." + process.pid + "." + randomUUID() + ".tmp")
  writeFileSync(tmp, JSON.stringify(data), "utf8")
  renameSync(tmp, final)
}

// Atomic write for plain-text content (used for .build-id).
function _atomicWriteText(dir, filename, text) {
  mkdirSync(dir, { recursive: true })
  const final = join(dir, filename)
  const tmp = join(dir, filename + "." + process.pid + "." + randomUUID() + ".tmp")
  writeFileSync(tmp, text, "utf8")
  renameSync(tmp, final)
}

// ---------------------------------------------------------------------------
// TypeScript executor source — compiled at runtime by esbuild stdin API.
//
// This source is "virtually" located at quartz/cli/build_partial_inline.ts,
// so esbuild resolves imports with resolveDir = path.join(cwd, "quartz", "cli"):
//
//   ../util/fastpath_manifest  → quartz/util/fastpath_manifest.ts  (T1 helper)
//   ../processors/parse        → quartz/processors/parse.ts
//   ../../quartz.config        → quartz.config.ts
//   ../plugins                 → quartz/plugins/index.ts
//   ../util/ctx                → quartz/util/ctx.ts
//   ../util/path               → quartz/util/path.ts
//   ../util/random             → quartz/util/random.ts
//
// Option C exclusion: ContentIndex, TagPage, and FolderPage are skipped in the emitter walk.
// _PARTIAL_EMIT_EXCLUDED.has(emitter.name) triggers a continue; no .emit call is made.
// ---------------------------------------------------------------------------

const _PARTIAL_BUILD_TS = `
import { computeFingerprint, FINGERPRINT_VERSION } from "../util/fastpath_manifest"
import { parseMarkdown } from "../processors/parse"
import cfg from "../../quartz.config"
import { getStaticResourcesFromPlugins } from "../plugins"
import { trieFromAllFiles } from "../util/ctx"
import { joinSegments, FilePath } from "../util/path"
import { randomIdNonSecure } from "../util/random"

export async function runPartialBuild(params) {
  const { argv, slug, slugEntry, contentmapEntries } = params

  // Reconstruct allFiles/allSlugs from contentmap for the BuildCtx.
  // This gives emitters the full cross-file view (backlinks, breadcrumbs, etc.)
  // even though we only re-parse and emit one file.
  const allFiles = contentmapEntries
    .filter(function(e) { return e.type === "markdown" && e.filePath })
    .map(function(e) { return e.filePath })
  const allSlugs = contentmapEntries
    .filter(function(e) { return e.type === "markdown" && e.vfileData && e.vfileData.slug })
    .map(function(e) { return e.vfileData.slug })

  const ctx = {
    buildId: randomIdNonSecure(),
    argv: argv,
    cfg: cfg,
    allSlugs: allSlugs,
    allFiles: allFiles,
    incremental: true,
    cacheDir: undefined,
  }

  // Re-parse the single changed file.  Uses Plan A parser cache on hit.
  const absoluteSourcePath = joinSegments(argv.directory, slugEntry.source_path)
  const parsed = await parseMarkdown(ctx, [absoluteSourcePath])

  if (parsed.length === 0) {
    throw new Error(
      "build-partial: parseMarkdown returned empty result for slug=" + slug +
      " source=" + absoluteSourcePath
    )
  }

  // Extract vfile early so it can be attached to changeEvents.
  // Every upstream partialEmit starts with: if (!changeEvent.file) continue
  // Without file attached, the emitter walk emits 0 files (ship-blocker HIGH-1 fix).
  const [_tree, vfile0] = parsed[0]

  // Rebuild ctx.trie from contentmap entries so breadcrumbs, backlinks, and
  // the Explorer component all see the full vault structure.
  const allVFileData = contentmapEntries
    .filter(function(e) { return e.type === "markdown" && e.vfileData })
    .map(function(e) {
      const d = e.vfileData
      return Object.assign({}, d, {
        filePath: d.filePath || e.filePath,
        dates: {
          created: d.dates && d.dates.created ? new Date(d.dates.created) : null,
          modified: d.dates && d.dates.modified ? new Date(d.dates.modified) : null,
          published: d.dates && d.dates.published ? new Date(d.dates.published) : null,
        },
      })
    })
  ctx.trie = trieFromAllFiles(allVFileData)

  const staticResources = getStaticResourcesFromPlugins(ctx)

  // Fix #1 (full-corpus reconstruction): build a ProcessedContent-equivalent array
  // containing ALL files, not just the changed one.  Emitters like ContentPage,
  // Backlinks, TagPage, and FolderPage derive allFiles from
  // content.map(c => c[1].data), so without the full set:
  //   - Backlinks loses cross-file sources (unchanged pages linking to this slug
  //     are invisible — they disappear from the rendered backlinks section).
  //   - Transclusion resolution (renderPage.tsx) cannot find target page blocks
  //     referenced in the changed file, causing broken transclusions.
  //   - TagPage / FolderPage see a 1-entry corpus and emit incorrect tag/folder pages.
  //
  // CONTRACT: synthesized unchanged entries have AST=null (index 0) and no
  // rawSource/value because contentmap is metadata-only by T2 design.
  // Emitters that dereference c[0] (HAST AST) for unchanged corpus entries MUST be
  // excluded from the emitter walk.  Currently excluded (see _PARTIAL_EMIT_EXCLUDED):
  //   - ContentIndex (full-build search index regeneration, Option C / T0 M2)
  //   - TagPage      (computeTagInfo reads unchanged custom tags/<tag>.md trees)
  //   - FolderPage   (computeFolderInfo reads unchanged folder description trees)
  // Verified by reading upstream emitters at tagPage.tsx:48-55 and folderPage.tsx:81-85.
  // Adding a new emitter that reads c[0] for non-changed slugs requires updating this set.
  const changedSlug = slug
  const synthesizedUnchanged = contentmapEntries
    .filter(function(e) {
      return e.type === "markdown" && e.vfileData &&
             (e.vfileData.slug ?? null) !== changedSlug
    })
    .map(function(e) {
      const d = e.vfileData
      const rehydratedData = Object.assign({}, d, {
        filePath: d.filePath || e.filePath,
        // Rehydrate Date strings -> Date instances (mirrors parse.ts rehydrateDates).
        dates: {
          created: d.dates && d.dates.created ? new Date(d.dates.created) : null,
          modified: d.dates && d.dates.modified ? new Date(d.dates.modified) : null,
          published: d.dates && d.dates.published ? new Date(d.dates.published) : null,
        },
      })
      // AST (index 0) is null for unchanged entries — only c[1].data is used by emitters.
      return [null, { data: rehydratedData, value: undefined }]
    })
  // filteredContent[0]    = changed file with full HAST + vfile (real parse result)
  // filteredContent[1..]  = unchanged files with null AST and metadata-only vfile data
  const filteredContent = [parsed[0], ...synthesizedUnchanged]
  const emittedFiles = []

  // Build ChangeEvent with file attached.  Without file, every emitter's partialEmit
  // does: if (!changeEvent.file) continue — emitting 0 files total.
  // Fix #3 (path shape): use vault-relative source_path, NOT absolute path.
  // Upstream chokidar watcher records vault-relative paths (build.ts:416-425) and
  // rebuild() emits those same relative keys as ChangeEvent.path (build.ts:499-507).
  const changeEvents = [{
    type: "change" as const,
    path: slugEntry.source_path as FilePath,
    file: vfile0,
  }]

  // Walk emitters — Option C: skip excluded emitters (see _PARTIAL_EMIT_EXCLUDED below).
  // emitter.partialEmit is preferred when available; falls back to emitter.emit.
  //
  // Fix #2 (fail-fast): a SINGLE try/catch wraps the ENTIRE emitter walk.
  // Any emitter exception aborts via process.exit(5) BEFORE the artifact write block
  // below (contentmap.json / manifest.json / .build-id writes at step 14).
  // This enforces the Plan B contract: partial emit writes NOTHING on failure so the
  // classifier's "successful fingerprint" invariant is never corrupted by stale HTML.
  let _currentEmitterName = "unknown"
  try {
    // Option C exclusions: emitters that dereference c[0] (HAST AST) for unchanged
    // content entries cannot operate safely on the synthesized [null, vfile] tuples.
    // All three exclusions accept staleness until the next full build:
    //   - ContentIndex: full-corpus search index regeneration is full-only by T0/M2.
    //   - TagPage:      computeTagInfo at tagPage.tsx:48-55 dereferences unchanged
    //                   custom tags/<tag>.md entries' tree -> TagContent.tsx:111
    //                   crashes on null.children.length.
    //   - FolderPage:   computeFolderInfo at folderPage.tsx:81-85 same pattern;
    //                   FolderContent.tsx:99 crashes on null.children.length.
    // Trivial edits don't change tags or folder structure (canonical-blob spec
    // routes those to NON_TRIVIAL -> full build via T3 classifier), so tag/folder
    // pages don't need regeneration on the fast path.
    const _PARTIAL_EMIT_EXCLUDED = new Set(["ContentIndex", "TagPage", "FolderPage"])
    for (const emitter of cfg.plugins.emitters) {
      if (_PARTIAL_EMIT_EXCLUDED.has(emitter.name)) {
        if (argv.verbose) {
          console.log("[build-partial] skipping " + emitter.name + " (Option C exclusion)")
        }
        continue
      }

      _currentEmitterName = emitter.name
      const emitFn = emitter.partialEmit || emitter.emit
      const result = await emitFn(ctx, filteredContent, staticResources, changeEvents)
      if (result == null) continue
      if (typeof result === "object" && Symbol.asyncIterator in result) {
        for await (const f of result) {
          emittedFiles.push(f)
          if (argv.verbose) {
            console.log("[build-partial emit:" + emitter.name + "] " + f)
          }
        }
      } else if (Array.isArray(result)) {
        for (const f of result) {
          emittedFiles.push(f)
          if (argv.verbose) {
            console.log("[build-partial emit:" + emitter.name + "] " + f)
          }
        }
      }
    }
  } catch (err) {
    // Fail-fast: abort before artifact writes so stale HTML is never committed
    // with a fresh fingerprint (Plan B contract violation if we continued).
    process.stderr.write(
      "partial emit failed in " + _currentEmitterName + ": " +
      (err instanceof Error ? err.message : String(err)) + "\\n"
    )
    process.exit(5)
  }

  // Compute updated fingerprint for this slug (using T1's computeFingerprint).
  // vfile0 was already extracted above (before emitter walk); parsed0 alias for computeFingerprint.
  const parsed0 = parsed[0]
  const newFingerprint = computeFingerprint(parsed0, {
    sourcePath: slugEntry.source_path,
    outputPath: slugEntry.output_path,
  })

  // Serialize updated vfile.data for contentmap entry replacement.
  const data = vfile0.data
  const rawDates = data.dates
  const serializedDates = {
    created: rawDates && rawDates.created instanceof Date
      ? rawDates.created.toISOString()
      : (rawDates && rawDates.created != null ? rawDates.created : null),
    modified: rawDates && rawDates.modified instanceof Date
      ? rawDates.modified.toISOString()
      : (rawDates && rawDates.modified != null ? rawDates.modified : null),
    published: rawDates && rawDates.published instanceof Date
      ? rawDates.published.toISOString()
      : (rawDates && rawDates.published != null ? rawDates.published : null),
  }

  return {
    newFingerprint: newFingerprint,
    emittedFiles: emittedFiles,
    newVFileData: {
      frontmatter: data.frontmatter || null,
      links: data.links || null,
      text: data.text || null,
      blocks: data.blocks || {},
      dates: serializedDates,
      filePath: String(data.filePath || ""),
      relativePath: String(data.relativePath || ""),
      slug: String(data.slug || ""),
      description: typeof data.description === "string" ? data.description : null,
      toc: data.toc != null ? data.toc : null,
      collapseToc: typeof data.collapseToc === "boolean" ? data.collapseToc : null,
      aliases: Array.isArray(data.aliases) ? data.aliases.map(String) : null,
      hasMermaidDiagram: typeof data.hasMermaidDiagram === "boolean"
        ? data.hasMermaidDiagram
        : null,
    },
  }
}
`

// ---------------------------------------------------------------------------
// esbuild compilation of the TypeScript executor
//
// The compiled output is cached at _PARTIAL_CACHE_FILE.  Freshness check:
// skip recompilation if the file exists AND is <5 minutes old (300 000 ms).
// This keeps "warm" partial builds fast (~100-300ms) while ensuring overlay
// changes (which require re-installation anyway) are picked up promptly.
// ---------------------------------------------------------------------------

const _PARTIAL_CACHE_FILE = path.join(cwd, "quartz", ".quartz-cache", "transpiled-build-partial.mjs")

async function _compilePartialBuildTs() {
  // Freshness check — skip recompilation if cache is warm (<5 min old).
  let needsCompile = true
  try {
    const { mtimeMs } = statSync(_PARTIAL_CACHE_FILE)
    needsCompile = (Date.now() - mtimeMs) > 300_000
  } catch (_e) {
    // File absent — must compile.
  }

  if (needsCompile) {
    // Ensure cache directory exists.
    mkdirSync(path.join(cwd, "quartz", ".quartz-cache"), { recursive: true })

    await esbuild.build({
      stdin: {
        contents: _PARTIAL_BUILD_TS,
        resolveDir: path.join(cwd, "quartz", "cli"),
        loader: "ts",
        sourcefile: "build_partial_inline.ts",
      },
      outfile: _PARTIAL_CACHE_FILE,
      bundle: true,
      keepNames: true,
      minifyWhitespace: true,
      minifySyntax: true,
      platform: "node",
      format: "esm",
      packages: "external",
      sourcemap: false,
      plugins: [
        sassPlugin({ type: "css-text", cssImports: true }),
        sassPlugin({ filter: /\.inline\.scss$/, type: "css", cssImports: true }),
        {
          name: "inline-script-loader",
          setup(build) {
            build.onLoad({ filter: /\.inline\.(ts|js)$/ }, async (args) => {
              let text = await fsPromises.readFile(args.path, "utf8")
              text = text.replace("export default", "")
              text = text.replace("export", "")
              const sourcefile = path.relative(path.resolve("."), args.path)
              const resolveDir = path.dirname(sourcefile)
              const transpiled = await esbuild.build({
                stdin: { contents: text, loader: "ts", resolveDir, sourcefile },
                write: false,
                bundle: true,
                minify: true,
                platform: "browser",
                format: "esm",
              })
              return { contents: transpiled.outputFiles[0].text, loader: "text" }
            })
          },
        },
      ],
    })
  }

  // Bypass module cache with unique query string (same pattern as upstream handleBuild).
  const { runPartialBuild } = await import(_PARTIAL_CACHE_FILE + "?update=" + randomUUID())
  return runPartialBuild
}

// ---------------------------------------------------------------------------
// Main exported handler — called by handlers.js as ``partialBuildContent``
// ---------------------------------------------------------------------------

export async function handlePartialBuild(argv) {
  const startMs = Date.now()
  const slug = argv.slug

  if (!slug) {
    process.stderr.write("build-partial: --slug is required\n")
    process.exit(1)
  }

  // Step 2: fastpath dir lives under the vault root (argv.directory), NOT argv.output.
  // Per T0 F5.3: <vault>/.quartz/.cache/fastpath/
  const fastpathDir = path.join(argv.directory, ".quartz", ".cache", "fastpath")

  // Step 3: Load manifest.json (the commit marker written by T2 full-build hook).
  let manifest
  try {
    const raw = readFileSync(join(fastpathDir, "manifest.json"), "utf8")
    manifest = JSON.parse(raw)
  } catch (_err) {
    process.stderr.write("manifest not found — full build required\n")
    process.exit(1)
  }

  // Step 4: Load contentmap.json (written atomically before manifest by T2).
  let contentmap
  try {
    const raw = readFileSync(join(fastpathDir, "contentmap.json"), "utf8")
    contentmap = JSON.parse(raw)
  } catch (_err) {
    process.stderr.write("contentmap not found — full build required\n")
    process.exit(1)
  }

  // Step 5: Envelope cross-check (Codex pre-flight contract — non-negotiable).
  // Both version and parent_build_id must match across manifest and contentmap.
  if (manifest.version !== contentmap.version ||
      manifest.parent_build_id !== contentmap.parent_build_id) {
    process.stderr.write(
      "envelope mismatch: manifest=" + manifest.version + "/" + manifest.parent_build_id +
      " contentmap=" + contentmap.version + "/" + contentmap.parent_build_id + "\n"
    )
    process.exit(2)
  }

  // Step 6: Look up slug in manifest.slugs.
  // Output paths come from manifest.slugs[slug].output_path — not re-derived.
  const slugEntry = manifest.slugs[slug]
  if (!slugEntry) {
    process.stderr.write("slug not in manifest — full build required: " + slug + "\n")
    process.exit(3)
  }

  // Step 6b: Unsupported fast-path slug guard (exit 6).
  // ContentPage.partialEmit skips slugs starting with "tags/" or ending with "/index"
  // (contentPage.tsx:112-117).  Our deny-list also skips TagPage/FolderPage which own
  // those outputs.  So a fast-path edit to such a slug would advance
  // manifest/contentmap/.build-id while leaving the rendered HTML stale.
  // Refuse here and let T5/T6 fall back to full build.
  if (slug.startsWith("tags/") || slug.endsWith("/index") || slug === "index") {
    process.stderr.write(
      "scope: full build required for slug=" + slug +
      " (tag/folder pages must be refreshed by full build)\n"
    )
    process.exit(6)
  }

  // Step 7: Look up slug in contentmap.entries (envelope-style array, NOT bare array).
  const contentEntryIdx = (contentmap.entries || []).findIndex(
    function(e) { return e.vfileData && e.vfileData.slug === slug }
  )
  if (contentEntryIdx === -1) {
    process.stderr.write("slug not in contentmap — full build required: " + slug + "\n")
    process.exit(4)
  }

  // Steps 8-10: Compile + run the TypeScript executor via esbuild stdin.
  let result
  try {
    const runPartialBuild = await _compilePartialBuildTs()
    result = await runPartialBuild({
      argv: argv,
      slug: slug,
      slugEntry: slugEntry,
      contentmapEntries: contentmap.entries,
    })
  } catch (err) {
    process.stderr.write("build-partial: executor failed: " + err + "\n")
    process.exit(1)
  }

  const { newFingerprint, newVFileData, emittedFiles } = result

  if (argv.verbose) {
    console.log("wiki: build-partial emitted " + emittedFiles.length + " file(s):")
    for (const f of emittedFiles) console.log("  " + f)
  }

  // Step 12: Update manifest.slugs for this slug only.
  // KEEP manifest.parent_build_id unchanged — partial emit inherits it from the full build.
  const updatedManifest = Object.assign({}, manifest, {
    built_at_ms: Date.now(),
    slugs: Object.assign({}, manifest.slugs, {
      [slug]: Object.assign({}, slugEntry, { fingerprint: newFingerprint }),
    }),
  })

  // Step 13: Replace contentmap entry for this slug.
  // KEEP contentmap.parent_build_id unchanged.
  const updatedEntries = (contentmap.entries || []).slice()
  updatedEntries[contentEntryIdx] = Object.assign(
    {},
    updatedEntries[contentEntryIdx],
    {
      hastRoot: null,
      vfileData: newVFileData,
    }
  )
  const updatedContentmap = Object.assign({}, contentmap, {
    built_at_ms: Date.now(),
    entries: updatedEntries,
  })

  // Step 14: Write order — contentmap FIRST, manifest SECOND, .build-id LAST.
  // This preserves the T2 commit-marker invariant: if manifest.json exists,
  // contentmap.json is guaranteed present and consistent.
  _atomicWriteJson(fastpathDir, "contentmap.json", updatedContentmap)
  _atomicWriteJson(fastpathDir, "manifest.json", updatedManifest)

  // .build-id lives at <argv.output>/.build-id — the watcher's reload trigger.
  // Emit a millisecond-suffixed fast-path id matching the plan spec.
  const buildId = "fastpath-" + Date.now() + "-" + randomUUID().slice(0, 8)
  _atomicWriteText(argv.output, ".build-id", buildId)

  const elapsedMs = Date.now() - startMs
  console.log("wiki: build-partial slug=" + slug + " elapsed=" + elapsedMs + "ms")
  process.exit(0)
}
