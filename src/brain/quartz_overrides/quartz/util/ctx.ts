// Brain wiki — BuildCtx with parser-cache plumbing.
//
// This file is a TEMPLATE. It is installed at
// `<vault>/.quartz/quartz/util/ctx.ts` by `brain vault render --overlay`,
// overwriting the stock upstream module.
//
// Changes from upstream:
//   * `Argv.noCache?: boolean` — opt-out flag. When true, parseMarkdown
//     sets cacheDir to null and the parser cache is disabled for the run.
//     The upstream Quartz CLI handler does not set this flag (it lives in
//     upstream-only handlers.ts which we do not override), so in practice
//     it is always undefined (falsy) and the cache is always active unless
//     a future override of the CLI handler flips it. Wired in as
//     `argv.noCache` so the flag propagates to workers via
//     WorkerSerializableBuildCtx.argv without any additional plumbing.
//   * `BuildCtx.cacheDir?: string | null` — on-disk path for the parser
//     result cache. `null` disables the cache for the run.
//     `undefined` means "not yet set by the caller"; parseMarkdown in
//     parse.ts defaults it to `<argv.directory>/.quartz/.cache/parser`
//     so it matches `bin/brain-rebuild --clean-cache`'s rm target.
//     Optional so existing BuildCtx constructors (upstream handlers.ts
//     and test fixtures) compile without modification.
//     `WorkerSerializableBuildCtx = Omit<BuildCtx, "cfg" | "trie">` so
//     cacheDir auto-propagates to workers via the type — no worker.ts
//     edit needed. The serializableCtx literal in parse.ts does list
//     fields by hand and explicitly includes `cacheDir`.

import { QuartzConfig } from "../cfg"
import { QuartzPluginData } from "../plugins/vfile"
import { FileTrieNode } from "./fileTrie"
import { FilePath, FullSlug } from "./path"

export interface Argv {
  directory: string
  verbose: boolean
  output: string
  serve: boolean
  watch: boolean
  port: number
  wsPort: number
  remoteDevHost?: string
  concurrency?: number
  // brain: opt-out flag for the parser cache. When true, parseMarkdown
  // sets cacheDir = null so no cache reads or writes occur for this run.
  // Not currently wired from the upstream CLI handler (handlers.ts is not
  // overridden); a future override can expose `--no-cache` via yargs and
  // set this field. Until then it is always undefined = cache active.
  noCache?: boolean
}

export type BuildTimeTrieData = QuartzPluginData & {
  slug: string
  title: string
  filePath: string
}

export interface BuildCtx {
  buildId: string
  argv: Argv
  cfg: QuartzConfig
  allSlugs: FullSlug[]
  allFiles: FilePath[]
  trie?: FileTrieNode<BuildTimeTrieData>
  incremental: boolean
  // brain: on-disk directory for the parser result cache.
  //   string  — active cache at this path
  //   null    — cache explicitly disabled for this run (e.g. --no-cache)
  //   undefined — not yet set; parseMarkdown.ts defaults to
  //               <argv.directory>/.quartz/.cache/parser (matches the
  //               rm target in bin/brain-rebuild --clean-cache).
  // Optional so existing BuildCtx constructors compile without change.
  cacheDir?: string | null
}

export function trieFromAllFiles(allFiles: QuartzPluginData[]): FileTrieNode<BuildTimeTrieData> {
  const trie = new FileTrieNode<BuildTimeTrieData>([])
  allFiles.forEach((file) => {
    if (file.frontmatter) {
      trie.add({
        ...file,
        slug: file.slug!,
        title: file.frontmatter.title,
        filePath: file.filePath!,
      })
    }
  })

  return trie
}

export type WorkerSerializableBuildCtx = Omit<BuildCtx, "cfg" | "trie">
