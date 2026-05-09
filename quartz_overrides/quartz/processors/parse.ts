// Brain wiki — parse pipeline with per-file parser result cache.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/processors/parse.ts` by
// `brain vault render --overlay`, overwriting the stock upstream module.
//
// Changes from upstream:
//   1. `createFileParser` loop: reads raw bytes with `readFile` (replaces
//      `read` from to-vfile — equivalent but gives us the Buffer for hashing).
//      On each file, computes a cache key and checks the MDAST cache before
//      running `processor.parse` + `processor.run`. Hit → push reconstituted
//      MarkdownContent and continue. Miss → run existing path, then store.
//      Only the MDAST phase is cached: the cache key is deterministic on
//      (file_bytes, slug) and the dominant build cost is `processor.parse` +
//      `processor.run` (remark), not the remark-rehype step that follows.
//      The HTML phase (`createMarkdownParser`) is left identical to upstream.
//   2. `parseMarkdown`: computes `cacheDir` (defaults to
//      `<argv.directory>/.quartz/.cache/parser` unless ctx.cacheDir is already
//      set or `argv.noCache` is true). Propagates `cacheDir` to workers via
//      the `serializableCtx` literal.
//
// Cache module: `./parser_cache.ts` — pure Node.js, no Quartz-internal
// imports, tested independently in `tests/test_quartz_parser_cache.ts`.
//
// Safety: every MDAST transformer in quartz_overrides/quartz/plugins/
// transformers/ is a pure function of (file bytes, slug) — no cross-file
// reads at the parse stage. Cross-file work (backlinks, contentIndex,
// related-docs) happens in the emitter phase on the in-memory
// ProcessedContent[] collection AFTER this function returns. The
// transformers/index.ts file documents this contract.
//
// Tested against Quartz v4.5.x (April 2026). If a future Quartz version
// changes the parse pipeline shape (e.g. adds a third processor phase or
// changes how VFile is constructed), diff upstream against this file and
// re-apply the brain-cache hooks.

import esbuild from "esbuild"
import remarkParse from "remark-parse"
import remarkRehype from "remark-rehype"
import { Processor, unified } from "unified"
import { Root as MDRoot } from "remark-parse/lib"
import { Root as HTMLRoot } from "hast"
import { MarkdownContent, ProcessedContent } from "../plugins/vfile"
import { PerfTimer } from "../util/perf"
import { readFile } from "node:fs/promises"
import { VFile } from "vfile"
import { FilePath, QUARTZ, slugifyFilePath } from "../util/path"
import path from "path"
import workerpool, { Promise as WorkerPromise } from "workerpool"
import { QuartzLogger } from "../util/log"
import { trace } from "../util/trace"
import { BuildCtx, WorkerSerializableBuildCtx } from "../util/ctx"
import { styleText } from "util"
import { cacheKey, getCached, putCached, CACHE_VERSION } from "./parser_cache"

export type QuartzMdProcessor = Processor<MDRoot, MDRoot, MDRoot>
export type QuartzHtmlProcessor = Processor<undefined, MDRoot, HTMLRoot>

export function createMdProcessor(ctx: BuildCtx): QuartzMdProcessor {
  const transformers = ctx.cfg.plugins.transformers

  return (
    unified()
      // base Markdown -> MD AST
      .use(remarkParse)
      // MD AST -> MD AST transforms
      .use(
        transformers.flatMap((plugin) => plugin.markdownPlugins?.(ctx) ?? []),
      ) as unknown as QuartzMdProcessor
    //  ^ sadly the typing of `use` is not smart enough to infer the correct type from our plugin list
  )
}

export function createHtmlProcessor(ctx: BuildCtx): QuartzHtmlProcessor {
  const transformers = ctx.cfg.plugins.transformers
  return (
    unified()
      // MD AST -> HTML AST
      .use(remarkRehype, { allowDangerousHtml: true })
      // HTML AST -> HTML AST transforms
      .use(transformers.flatMap((plugin) => plugin.htmlPlugins?.(ctx) ?? []))
  )
}

function* chunks<T>(arr: T[], n: number) {
  for (let i = 0; i < arr.length; i += n) {
    yield arr.slice(i, i + n)
  }
}

async function transpileWorkerScript() {
  // transpile worker script
  const cacheFile = "./.quartz-cache/transpiled-worker.mjs"
  const fp = "./quartz/worker.ts"
  return esbuild.build({
    entryPoints: [fp],
    outfile: path.join(QUARTZ, cacheFile),
    bundle: true,
    keepNames: true,
    platform: "node",
    format: "esm",
    packages: "external",
    sourcemap: true,
    sourcesContent: false,
    plugins: [
      {
        name: "css-and-scripts-as-text",
        setup(build) {
          build.onLoad({ filter: /\.scss$/ }, (_) => ({
            contents: "",
            loader: "text",
          }))
          build.onLoad({ filter: /\.inline\.(ts|js)$/ }, (_) => ({
            contents: "",
            loader: "text",
          }))
        },
      },
    ],
  })
}

// brain-cache: shape of a stored MDAST cache entry.
type MdCacheEntry = {
  version: number
  slug: string
  ast: MDRoot
  data: Record<string, unknown>
}

export function createFileParser(ctx: BuildCtx, fps: FilePath[]) {
  const { argv, cfg } = ctx
  return async (processor: QuartzMdProcessor) => {
    const res: MarkdownContent[] = []
    for (const fp of fps) {
      try {
        const perf = new PerfTimer()

        // brain-cache: read raw bytes once; re-use as VFile value and as
        // hash input. Equivalent to `await read(fp)` from to-vfile but
        // gives us a Buffer for the cache key without a second fs.readFile.
        const rawBytes = await readFile(fp)
        const file = new VFile({ path: fp as string, value: rawBytes })

        // strip leading and trailing whitespace
        file.value = file.value.toString().trim()

        // Text -> Text transforms
        for (const plugin of cfg.plugins.transformers.filter((p) => p.textTransform)) {
          file.value = plugin.textTransform!(ctx, file.value.toString())
        }

        // base data properties that plugins may use
        file.data.filePath = file.path as FilePath
        file.data.relativePath = path.posix.relative(argv.directory, file.path!) as FilePath
        file.data.slug = slugifyFilePath(file.data.relativePath as FilePath)

        // brain-cache: check MDAST cache before running the expensive parse.
        const mdKey = ctx.cacheDir ? cacheKey(rawBytes, file.data.slug as string) : null
        if (mdKey !== null) {
          const cached = getCached<MdCacheEntry>(ctx.cacheDir!, mdKey)
          // belt-and-suspenders: version is already baked into the cache key (the
          // key hash includes CACHE_VERSION), so a stale entry at a different key
          // path is simply a miss. The explicit version check here guards the
          // unlikely edge case where two builds with different CACHE_VERSION values
          // happen to collide on a key (sha256 collision) — effectively impossible,
          // but cheap to double-check.
          if (cached !== null && cached.version === CACHE_VERSION) {
            // Reconstitute MarkdownContent from cached entry.
            // value is set to the trimmed string so cache-hit and cache-miss
            // paths produce the same file.value shape for downstream emitters.
            const cachedFile = new VFile({ path: fp as string, value: rawBytes })
            cachedFile.value = rawBytes.toString().trim()
            cachedFile.data = cached.data as typeof file.data
            res.push([cached.ast, cachedFile])
            if (argv.verbose) {
              console.log(`[markdown hit] ${fp} -> ${cachedFile.data.slug} (${perf.timeSince()})`)
            }
            continue
          }
        }

        const ast = processor.parse(file)
        const newAst = await processor.run(ast, file)

        // brain-cache: push BEFORE putCached so the file is always emitted
        // even if putCached throws (ENOSPC / EACCES / readonly FS). A write
        // failure must never silently drop a file from the build output.
        res.push([newAst, file])

        // brain-cache: store result for next build.
        if (mdKey !== null) {
          putCached<MdCacheEntry>(ctx.cacheDir!, mdKey, {
            version: CACHE_VERSION,
            slug: file.data.slug as string,
            ast: newAst,
            data: file.data as Record<string, unknown>,
          })
        }

        if (argv.verbose) {
          console.log(`[markdown] ${fp} -> ${file.data.slug} (${perf.timeSince()})`)
        }
      } catch (err) {
        trace(`\nFailed to process markdown \`${fp}\``, err as Error)
      }
    }

    return res
  }
}

export function createMarkdownParser(ctx: BuildCtx, mdContent: MarkdownContent[]) {
  return async (processor: QuartzHtmlProcessor) => {
    const res: ProcessedContent[] = []
    for (const [ast, file] of mdContent) {
      try {
        const perf = new PerfTimer()

        const newAst = await processor.run(ast as MDRoot, file)
        res.push([newAst, file])

        if (ctx.argv.verbose) {
          console.log(`[html] ${file.data.slug} (${perf.timeSince()})`)
        }
      } catch (err) {
        trace(`\nFailed to process html \`${file.data.filePath}\``, err as Error)
      }
    }

    return res
  }
}

const clamp = (num: number, min: number, max: number) =>
  Math.min(Math.max(Math.round(num), min), max)

export async function parseMarkdown(ctx: BuildCtx, fps: FilePath[]): Promise<ProcessedContent[]> {
  const { argv } = ctx
  const perf = new PerfTimer()
  const log = new QuartzLogger(argv.verbose)

  // brain-cache: resolve cacheDir. Precedence:
  //   1. ctx.cacheDir already set by caller (string | null | undefined)
  //   2. argv.noCache === true  → null  (disable for this run)
  //   3. default               → <argv.directory>/.quartz/.cache/parser
  // `cacheDir: undefined` from an upstream BuildCtx constructor means
  // "not set yet" and falls through to the default.
  // NOTE: this path must match the rm target in bin/brain-rebuild's
  // --clean-cache handler: `"$VAULT/.quartz/.cache/parser"`.
  const cacheDir: string | null =
    ctx.cacheDir !== undefined
      ? ctx.cacheDir
      : argv.noCache
        ? null
        : path.join(argv.directory, ".quartz", ".cache", "parser")

  const patchedCtx: BuildCtx = { ...ctx, cacheDir }

  // rough heuristics: 128 gives enough time for v8 to JIT and optimize parsing code paths
  const CHUNK_SIZE = 128
  const concurrency = argv.concurrency ?? clamp(fps.length / CHUNK_SIZE, 1, 4)

  let res: ProcessedContent[] = []
  log.start(`Parsing input files using ${concurrency} threads`)
  if (concurrency === 1) {
    try {
      const mdRes = await createFileParser(patchedCtx, fps)(createMdProcessor(patchedCtx))
      res = await createMarkdownParser(patchedCtx, mdRes)(createHtmlProcessor(patchedCtx))
    } catch (error) {
      log.end()
      throw error
    }
  } else {
    await transpileWorkerScript()
    const pool = workerpool.pool("./quartz/bootstrap-worker.mjs", {
      minWorkers: "max",
      maxWorkers: concurrency,
      workerType: "thread",
    })
    const errorHandler = (err: any) => {
      console.error(err)
      process.exit(1)
    }

    // brain-cache: include cacheDir so workers can read/write the same
    // on-disk cache. WorkerSerializableBuildCtx = Omit<BuildCtx, "cfg" | "trie">
    // so cacheDir is automatically part of the type; this literal is explicit
    // per the upstream pattern (workers deserialise by spreading partialCtx).
    const serializableCtx: WorkerSerializableBuildCtx = {
      buildId: patchedCtx.buildId,
      argv: patchedCtx.argv,
      allSlugs: patchedCtx.allSlugs,
      allFiles: patchedCtx.allFiles,
      incremental: patchedCtx.incremental,
      cacheDir: patchedCtx.cacheDir,
    }

    const textToMarkdownPromises: WorkerPromise<MarkdownContent[]>[] = []
    let processedFiles = 0
    for (const chunk of chunks(fps, CHUNK_SIZE)) {
      textToMarkdownPromises.push(pool.exec("parseMarkdown", [serializableCtx, chunk]))
    }

    const mdResults: Array<MarkdownContent[]> = await Promise.all(
      textToMarkdownPromises.map(async (promise) => {
        const result = await promise
        processedFiles += result.length
        log.updateText(`text->markdown ${styleText("gray", `${processedFiles}/${fps.length}`)}`)
        return result
      }),
    ).catch(errorHandler)

    const markdownToHtmlPromises: WorkerPromise<ProcessedContent[]>[] = []
    processedFiles = 0
    for (const mdChunk of mdResults) {
      markdownToHtmlPromises.push(pool.exec("processHtml", [serializableCtx, mdChunk]))
    }
    const results: ProcessedContent[][] = await Promise.all(
      markdownToHtmlPromises.map(async (promise) => {
        const result = await promise
        processedFiles += result.length
        log.updateText(`markdown->html ${styleText("gray", `${processedFiles}/${fps.length}`)}`)
        return result
      }),
    ).catch(errorHandler)

    res = results.flat()
    await pool.terminate()
  }

  log.end(`Parsed ${res.length} Markdown files in ${perf.timeSince()}`)
  return res
}
