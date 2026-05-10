#!/usr/bin/env -S node --no-deprecation
// Brain wiki overlay — quartz/bootstrap-cli.mjs
//
// This file shadows the upstream ``~/brain-vault/.quartz/quartz/bootstrap-cli.mjs``.
// All upstream commands are preserved verbatim.  The only addition is the
// ``build-partial`` command, which wires to ``partialBuildContent`` (exported from
// the handlers overlay at quartz/cli/handlers.js via build_partial_handler.js).
//
// T4 addition (Plan B v3 — per-file emit fast path):
//   ``build-partial``  — partial rebuild for a single changed slug.
//   Usage: node bootstrap-cli.mjs build-partial --slug <slug> [--directory <vault>] [--output <dir>]
//   Handler: partialBuildContent (from ./cli/handlers.js → ./cli/build_partial_handler.js)
//   Args:    BuildPartialArgv (from ./cli/args.js)

import yargs from "yargs"
import { hideBin } from "yargs/helpers"
import {
  handleBuild,
  handleCreate,
  handleUpdate,
  handleRestore,
  handleSync,
  partialBuildContent,
} from "./cli/handlers.js"
import { CommonArgv, BuildArgv, CreateArgv, SyncArgv, BuildPartialArgv } from "./cli/args.js"
import { version } from "./cli/constants.js"

yargs(hideBin(process.argv))
  .scriptName("quartz")
  .version(version)
  .usage("$0 <cmd> [args]")
  .command("create", "Initialize Quartz", CreateArgv, async (argv) => {
    await handleCreate(argv)
  })
  .command("update", "Get the latest Quartz updates", CommonArgv, async (argv) => {
    await handleUpdate(argv)
  })
  .command(
    "restore",
    "Try to restore your content folder from the cache",
    CommonArgv,
    async (argv) => {
      await handleRestore(argv)
    },
  )
  .command("sync", "Sync your Quartz to and from GitHub.", SyncArgv, async (argv) => {
    await handleSync(argv)
  })
  .command("build", "Build Quartz into a bundle of static HTML files", BuildArgv, async (argv) => {
    await handleBuild(argv)
  })
  // brain overlay (T4): fast partial rebuild for a single changed slug.
  // Re-parses one file, emits its HTML (skipping ContentIndex), and bumps .build-id.
  .command(
    "build-partial",
    "Partial rebuild for a single changed slug (brain fast path)",
    BuildPartialArgv,
    async (argv) => {
      await partialBuildContent(argv)
    },
  )
  .showHelpOnFail(false)
  .help()
  .strict()
  .demandCommand().argv
