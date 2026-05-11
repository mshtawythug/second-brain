#!/usr/bin/env bash
# Force a one-shot Quartz rebuild + atomic swap (blue/green serve edition).
# Use when you want a clean rebuild on demand — e.g. after a config or
# overlay change that the watcher won't pick up because nothing in the
# vault tree changed.
#
# Default behavior:
#   1. Run `brain vault export` so any DB-side changes (e.g. new ingested
#      docs that haven't been mirrored into the vault yet) are materialized
#      as Markdown files Quartz can render.
#   2. Run `brain vault prune-orphans --apply` to delete `_ingested/`
#      mirror files whose frontmatter id has no matching `documents` row
#      (e.g. test fixtures that leaked from pytest into prod, or stale
#      mirrors left behind after a `brain rm`). Safe by construction:
#      files without parseable frontmatter or with ids that resolve to a
#      live DB row are NEVER touched.
#   3. Apply the brain Quartz overlay (`quartz_overrides/`) into the
#      workspace via `brain vault render --overlay --no-build`, so any
#      edits to overlay sources land in `<vault>/.quartz/` before the
#      build picks them up. (`brain-up` does the same on cold start —
#      this mirrors that behaviour for in-place rebuilds.)
#   4. Run `python -m brain.wiki.build_swap` once: builds into a fresh
#      `<vault>/.quartz/builds/<id>/`, atomically retargets `current/`,
#      and prunes old build dirs. The build watcher (started by
#      `brain-up`) keeps running — no restart needed because the swap
#      is atomic and the watcher only cares about vault changes.
#
# Flags:
#   --no-export         skip the DB→vault export step (use if you only edited
#                       vault files directly). IMPLIES --no-sync-summaries
#                       since sync-summaries is also a DB→vault write.
#   --no-sync-summaries skip the summary-frontmatter sync step (use if you
#                       know no docs have been enriched out-of-band since
#                       the last rebuild).
#   --no-prune    skip the prune-orphans step (use if you intentionally
#                 want to keep DB-orphaned files around for inspection).
#   --no-overlay  skip the overlay re-apply step (use if you haven't
#                 touched `quartz_overrides/` since the last apply).
#   --no-build    skip the one-shot build (use if the running watcher
#                 will pick up the changes anyway after the export).
#   --clean-cache  wipe the parser cache (`<vault>/.quartz/.cache/parser/`)
#                  before building. Use to debug cache issues or measure a
#                  cold-build wall-time baseline (see Plan A spec).
#
# Env overrides (same as brain-up):
#   BRAIN_VAULT_PATH        (default: ~/brain-vault)
#   BRAIN_WIKI_PORT         (default: 8080)
#   BRAIN_WIKI_KEEP_BUILDS  (default: 3)
#   BRAIN_NO_OVERLAY        (default: 0; set 1 to skip the overlay step)

set -euo pipefail

VAULT="${BRAIN_VAULT_PATH:-$HOME/brain-vault}"
KEEP="${BRAIN_WIKI_KEEP_BUILDS:-3}"

# Pick the Python interpreter once. BRAIN_PY (env var) wins; otherwise
# prefer python3 on PATH. The installed-shim flow (in $BRAIN_HOME/bin/)
# always has BRAIN_PY exported by the Python launcher in
# src/brain/bin/_launcher.py — but we keep the python3 fallback for
# users who invoke the shims directly outside the launcher (e.g. via
# launchd plists that themselves set PATH but not BRAIN_PY).
if [[ -n "${BRAIN_PY:-}" ]]; then
    PY="$BRAIN_PY"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
else
    echo "no python interpreter found (set BRAIN_PY=… to override)" >&2
    exit 1
fi

DO_EXPORT=1
DO_SYNC_SUMMARIES=1
DO_PRUNE=1
DO_OVERLAY=1
DO_BUILD=1
DO_CLEAN_CACHE=0

for arg in "$@"; do
    case "$arg" in
        --no-export)          DO_EXPORT=0; DO_SYNC_SUMMARIES=0 ;;
        --no-sync-summaries)  DO_SYNC_SUMMARIES=0 ;;
        --no-prune)           DO_PRUNE=0 ;;
        --no-overlay)         DO_OVERLAY=0 ;;
        --no-build)           DO_BUILD=0 ;;
        --clean-cache)        DO_CLEAN_CACHE=1 ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//; /^set -euo/d'
            exit 0
            ;;
        *)
            echo "unknown flag: $arg" >&2
            echo "try: $(basename "$0") --help" >&2
            exit 2
            ;;
    esac
done

if ! command -v brain >/dev/null 2>&1; then
    echo 'brain CLI not on PATH — install second-brain first' >&2
    exit 1
fi

if [[ "$DO_EXPORT" == '1' ]]; then
    echo "🔄 exporting DB → vault ($VAULT)..."
    brain vault export --to "$VAULT"
fi

if [[ "$DO_SYNC_SUMMARIES" == '1' ]]; then
    # Export skips files whose body hash matches the DB (idempotent fast
    # path). That means a doc enriched out-of-band via `brain enrich
    # --backfill` never reaches the wiki via export alone — its body
    # didn't change, only `documents.summary`. `sync-summaries` rewrites
    # mirror frontmatter for those rows. Idempotent — exits as no-op when
    # on-disk frontmatter already matches the DB summary.
    echo "🔄 syncing summary frontmatter for enriched docs..."
    brain vault sync-summaries --vault "$VAULT" || {
        echo 'sync-summaries: failed (continuing — frontmatter may be stale)' >&2
    }
fi

if [[ "$DO_PRUNE" == '1' ]]; then
    echo "🔄 pruning DB-orphan + stale mirror files..."
    brain vault prune-orphans --apply --include-stale --vault "$VAULT" || {
        echo 'prune-orphans: failed (continuing — files left in place)' >&2
    }
fi

if [[ "$DO_OVERLAY" == '1' && "${BRAIN_NO_OVERLAY:-0}" != '1' ]]; then
    # `--no-build` applies the overlay to the workspace without running
    # `npx quartz build` — the build step below owns the actual build.
    # Mirrors the apply_overlay() helper in `bin/brain-up`.
    echo '🔄 applying quartz overlay...'
    if ! brain vault render --overlay --no-build --vault "$VAULT" 2>&1; then
        echo 'overlay: failed — build will use whatever is in the workspace' >&2
    fi
elif [[ "${BRAIN_NO_OVERLAY:-0}" == '1' ]]; then
    echo 'overlay: skipped (BRAIN_NO_OVERLAY=1)'
fi

if [[ "$DO_CLEAN_CACHE" == '1' ]]; then
    echo '🔄 cleaning parser cache...'
    rm -rf "$VAULT/.quartz/.cache/parser"
fi

if [[ "$DO_BUILD" == '1' ]]; then
    echo '🔄 running one-shot Quartz build + atomic swap...'
    BRAIN_WIKI_RELOAD=1 "$PY" -m brain.wiki.build_swap \
        --vault "$VAULT" --keep "$KEEP"
else
    echo '✅ vault refreshed — the running build watcher will pick up changes.'
fi
