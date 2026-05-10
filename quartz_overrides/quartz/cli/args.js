// Brain wiki overlay — quartz/cli/args.js
//
// This file shadows the upstream ``~/brain-vault/.quartz/quartz/cli/args.js``.
// All upstream argument definitions are preserved verbatim.  The only addition
// is ``BuildPartialArgv`` for the T4 ``build-partial`` subcommand.
//
// T4 addition (Plan B v3 — per-file emit fast path):
//   ``BuildPartialArgv`` — yargs argument spec for ``build-partial``:
//     --directory  (-d)  vault root (where .quartz/.cache/fastpath lives)
//     --output     (-o)  build output dir (where HTML is written + .build-id lives)
//     --slug       (-s)  Quartz slug to partially rebuild (required)
//     --verbose    (-v)  extra logging

export const CommonArgv = {
  directory: {
    string: true,
    alias: ["d"],
    default: "content",
    describe: "directory to look for content files",
  },
  verbose: {
    boolean: true,
    alias: ["v"],
    default: false,
    describe: "print out extra logging information",
  },
}

export const CreateArgv = {
  ...CommonArgv,
  source: {
    string: true,
    alias: ["s"],
    describe: "source directory to copy/create symlink from",
  },
  strategy: {
    string: true,
    alias: ["X"],
    choices: ["new", "copy", "symlink"],
    describe: "strategy for content folder setup",
  },
  links: {
    string: true,
    alias: ["l"],
    choices: ["absolute", "shortest", "relative"],
    describe: "strategy to resolve links",
  },
}

export const SyncArgv = {
  ...CommonArgv,
  commit: {
    boolean: true,
    default: true,
    describe: "create a git commit for your unsaved changes",
  },
  message: {
    string: true,
    alias: ["m"],
    describe: "option to override the default Quartz commit message",
  },
  push: {
    boolean: true,
    default: true,
    describe: "push updates to your Quartz fork",
  },
  pull: {
    boolean: true,
    default: true,
    describe: "pull updates from your Quartz fork",
  },
}

export const BuildArgv = {
  ...CommonArgv,
  output: {
    string: true,
    alias: ["o"],
    default: "public",
    describe: "output folder for files",
  },
  serve: {
    boolean: true,
    default: false,
    describe: "run a local server to live-preview your Quartz",
  },
  watch: {
    boolean: true,
    default: false,
    describe: "watch for changes and rebuild automatically",
  },
  baseDir: {
    string: true,
    default: "",
    describe: "base path to serve your local server on",
  },
  port: {
    number: true,
    default: 8080,
    describe: "port to serve Quartz on",
  },
  wsPort: {
    number: true,
    default: 3001,
    describe: "port to use for WebSocket-based hot-reload notifications",
  },
  remoteDevHost: {
    string: true,
    default: "",
    describe: "A URL override for the websocket connection if you are not developing on localhost",
  },
  bundleInfo: {
    boolean: true,
    default: false,
    describe: "show detailed bundle information",
  },
  concurrency: {
    number: true,
    describe: "how many threads to use to parse notes",
  },
}

// brain overlay (T4) — argument spec for the `build-partial` subcommand.
// build-partial re-parses a single changed slug and emits HTML for that slug
// only, updating manifest.json + contentmap.json + .build-id atomically.
// All options mirror the corresponding BuildArgv options; --slug is new and required.
export const BuildPartialArgv = {
  directory: {
    string: true,
    alias: ["d"],
    default: "content",
    describe: "vault root directory (where .quartz/.cache/fastpath/ lives)",
  },
  output: {
    string: true,
    alias: ["o"],
    default: "public",
    describe: "build output directory (where HTML is written and .build-id lives)",
  },
  slug: {
    string: true,
    alias: ["s"],
    describe: "Quartz FullSlug of the page to partially rebuild (required)",
    demandOption: true,
  },
  verbose: {
    boolean: true,
    alias: ["v"],
    default: false,
    describe: "print out extra logging information",
  },
}
