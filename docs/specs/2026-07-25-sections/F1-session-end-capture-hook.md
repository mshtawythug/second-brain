# F1 — Session-end capture hook and agent memory protocol

> Design section of `docs/specs/2026-07-25-agent-memory-safety-ui-design.md`.
> Global constraints (PII, production safety, quality gates, style) are inherited from
> section 4 of that document and are not restated here.

## Closing the write loop — a Claude Code session-end capture hook and an agent memory protocol

### 1. Goal

Agents read the brain reliably and write to it almost never, because writing is always the last step of a task and nothing forces it. This section closes that loop: a Claude Code `Stop` hook that fires **at most once per session**, only after the session did real work, and only when nothing was written back — nudging exactly one dedupe-then-capture pass. The decision heuristics live in unit-testable Python (`brain claude capture-hook`), not in bash; the installed shell script is a three-line shim. Alongside it ships `brain claude install-hooks` (a non-destructive `~/.claude/settings.json` merge), `brain capture --json` (a machine-readable confirmation the nudge can assert on), and a new `skills/brain-memory/SKILL.md` that states the read/write protocol. The user-visible outcome: after a substantive Claude Code session, one durable fact lands in the brain instead of evaporating — and after a read-only session, nothing happens at all.

### 2. Current state

**What exists.**

- `brain claude` is a real Typer sub-app with exactly one command. `claude_app = typer.Typer(name="claude", help="Claude Code integration.", no_args_is_help=True)` at `src/brain/cli.py:277-281`, registered at `src/brain/cli.py:282`. The single command `install-skill` is defined at `src/brain/cli.py:9213-9249`, delegating to `install_skill()` at `src/brain/cli_claude.py:20-35` and mapping `SkillInstallError` (`src/brain/cli_claude.py:11`) to `typer.Exit(code=1)` at `src/brain/cli.py:9246-9249`.
- The install idiom I will mirror exactly: default root `Path.home() / ".claude" / "skills"` (`src/brain/cli_claude.py:15`), byte-compare → `"skill up to date: {target}"` no-op (`src/brain/cli_claude.py:47-50`), `typer.confirm(..., abort=True)` unless `--force` (`src/brain/cli_claude.py:51-55`), and an uninstall that unlinks the file, warns on a non-empty dir, and `rmdir`s only when empty (`src/brain/cli_claude.py:60-81`).
- `brain capture` exists at `src/brain/_capture_command.py:47-133`: reads `--text` or stdin (`:77`), loads `Config` (`:86`), builds a deterministic auto-title via `capture_mod.make_capture_title` (`:96-102`, pure helper at `src/brain/capture.py:42`), normalizes `["inbox", *tag]` through `brain.tags.normalize_tags` (`:106`), authors a vault note via `create_vault_note(..., folder="capture")` (`src/brain/vault/note_builder.py:136-147`, called at `_capture_command.py:118-127`), and prints one human line at `:133`.
- The MCP twin `brain_capture` at `src/brain/mcp_server.py:851-917` already returns a dict: `{"document_id": ..., "status": "ingested"}` (`:915-918`).
- Reusable infrastructure: `atomic_write_text` (`src/brain/vault/_atomic.py:6-25`, sibling tempfile + `os.replace`, tempfile cleaned on any `BaseException`); `emit_json` (`src/brain/format.py:35-37`); `Config.load_minimal()` (`src/brain/config.py:741-753`, whose docstring already names `brain claude install-skill` as a filesystem-only caller that must not require `DATABASE_URL`); `_brain_home_root()` (`src/brain/config.py:410-430`); `_parse_positive_int_env()` (`src/brain/config.py:442-462`); `_default_vault_path()` (`src/brain/config.py:489-503`); `BrainError` (`src/brain/errors.py:16-17`).
- Package-data shipping precedent for a shell template: `"brain.templates.bin" = ["*.sh"]` (`pyproject.toml:105`) with the bytecode-strip counterpart at `pyproject.toml:125`, materialized by `ensure_shim()` (`src/brain/bin/_launcher.py:20-64`) using sha256 drift detection + `_atomic_write` (`src/brain/bin/_launcher.py:66-85`).
- `bin/brain-skills-sync` **does** enumerate `skills/*/` automatically — verified at `bin/brain-skills-sync:37-41` (`for d in "$SRC_SKILLS"/*/; do skills+=( "$(basename "$d")" ); done`), with the explicit comment "never a hardcoded (and therefore drift-prone) list". Its test derives the same set from the repo (`tests/test_brain_skills_sync.py:25-26`).
- `$BRAIN_HOME/run/` is already an established runtime-state directory for PID files (`src/brain/bin/monitor.py:36-52`).
- The transcript format is JSONL, one record per line; assistant records carry `message.content` as a list whose `{"type": "tool_use", "name": ..., "input": {...}}` blocks name each tool call. Verified empirically against a live transcript under `~/.claude/projects/<slug>/<session_id>.jsonl` (947 lines, `type` ∈ {assistant, user, attachment, mode, …}; tool names observed: `Bash`, `Read`).

**What is missing.**

- No hook of any kind. `~/.claude/settings.json` on this machine has keys `['env','attribution','statusLine','enabledPlugins','extraKnownMarketplaces','effortLevel','skipDangerousModePermissionPrompt','editorMode','teammateMode']` — **no `hooks` key at all**, so the "missing key" branch is the live case, not a hypothetical.
- `brain capture` has **no** `--json` flag (`src/brain/_capture_command.py:48-58` declares only `--title`, `--text`, `--tag/-t`). `brain capture list` has `--json` (`:413-414`) but the capture callback does not.
- There is no `skills/brain-memory/`. Existing skills: `brain-authoring`, `brain-graph`, `brain-maintenance`, `brain-todo`, `consult-brain`, `elicit-brain`, `ingest-brain`.
- There is no module anywhere that reads a hook payload or scans a transcript. `grep -rn "stop_hook_active\|transcript_path" src/` returns nothing.

### 3. User-visible surface

#### 3.1 `brain claude install-hooks`

```
brain claude install-hooks [--target PATH] [--force] [--uninstall] [--dry-run]
```

| Flag | Type | Default | Help text |
|---|---|---|---|
| `--target` | `Path \| None` | `None` → `~/.claude` | `Override the Claude Code config root (default ~/.claude); installs <target>/hooks/brain-capture-hook.sh and merges <target>/settings.json` |
| `--force` | `bool` | `False` | `Overwrite a differing hook script without prompting. Never bypasses the malformed-settings.json refusal.` |
| `--uninstall` | `bool` | `False` | `Remove the brain Stop hook entry and the hook script (settings.json is backed up first).` |
| `--dry-run` | `bool` | `False` | `Print what would change and exit without writing anything.` |

Note the deliberate difference from `install-skill`, whose `--target` is the *skills* root (`src/brain/cli.py:9215-9222`): here `--target` is the `.claude` config root, because two artifacts (a script and `settings.json`) must land under one parent. The help text spells this out.

Fresh install, no `hooks` key present (the live case):

```
hook script installed: /Users/you/.claude/hooks/brain-capture-hook.sh
backed up settings: /Users/you/.claude/settings.json.brain-backup-20260725T184102Z
Stop hook added to /Users/you/.claude/settings.json (timeout 10s)
capture nudge active — disable with BRAIN_HOOK_ENABLED=false or `brain claude install-hooks --uninstall`
```

Re-run (idempotent):

```
hook script up to date: /Users/you/.claude/hooks/brain-capture-hook.sh
Stop hook already present in /Users/you/.claude/settings.json
```

`--dry-run` on a fresh machine:

```
would install hook script: /Users/you/.claude/hooks/brain-capture-hook.sh
would add Stop hook entry to /Users/you/.claude/settings.json
(dry run — nothing written)
```

`--uninstall`:

```
backed up settings: /Users/you/.claude/settings.json.brain-backup-20260725T190551Z
removed Stop hook entry from /Users/you/.claude/settings.json
removed /Users/you/.claude/hooks/brain-capture-hook.sh
```

Malformed `settings.json` (exit 1, nothing written):

```
error: /Users/you/.claude/settings.json is not valid JSON (Expecting ',' delimiter: line 12 column 3) — refusing to rewrite it. Fix the file by hand, then re-run.
```

#### 3.2 `brain claude capture-hook` (plumbing)

```
brain claude capture-hook
```

No flags. Hidden from `brain claude --help` via `@claude_app.command("capture-hook", hidden=True)`. Reads one JSON object on stdin, writes at most one JSON object on stdout, **always exits 0**.

Stdin contract (Claude Code `Stop` payload; unknown keys ignored):

```json
{
  "session_id": "0dc8a75b-660f-45f3-ac6a-ba494554d9b3",
  "transcript_path": "/Users/you/.claude/projects/-Users-you-workspace-second-brain/0dc8a75b-....jsonl",
  "cwd": "/Users/you/workspace/second-brain",
  "hook_event_name": "Stop",
  "stop_hook_active": false
}
```

Stdout contract — **allow** (any of the eight allow branches): zero bytes, exit 0. **Nudge**: exactly one line, exit 0:

```json
{"decision":"block","reason":"This session did substantive work but nothing was written back to the second brain.\nDo exactly ONE capture pass, then stop:\n1. Search first: `brain search \"<2-4 keywords for what you learned>\" --limit 5 --json`.\n2. If a hit already states it, do NOT write — say \"already in the brain (<id-prefix>)\" and stop.\n3. Otherwise write ONE durable note: `brain capture --json --tag <topic> --text \"<the durable fact, decision, constraint, or gotcha>\"`. Assert on the returned `\"status\": \"ingested\"`.\n4. If the only outcome was a routine edit, a lookup, or something obvious from the repo, skip and say so in one line.\nDo not start new work. Do not run more than one capture."}
```

The `reason` is a **module-level constant**. No transcript text, no file paths, no user content is ever interpolated into it (see §7).

#### 3.3 `brain capture --json`

One new flag on the existing callback (`src/brain/_capture_command.py:48`):

| Flag | Type | Default | Help text |
|---|---|---|---|
| `--json` | `bool` | `False` | `Emit a machine-readable JSON confirmation instead of the human line.` |

Exact JSON shape:

```json
{
  "document_id": "3f2a1b9c-7d4e-4a11-9c02-5b6e8f0a1234",
  "id_prefix": "3f2a1b9c",
  "title": "2026-07-25-capture-hnsw-index-caps-at-2000-dims",
  "tags": ["inbox", "pgvector"],
  "vault_path": "capture/2026-07-25-capture-hnsw-index-caps-at-2000-dims.md",
  "status": "ingested"
}
```

`document_id` and `status` are keyed identically to the MCP tool's return (`src/brain/mcp_server.py:915-918`) so an agent asserts on one vocabulary regardless of surface. `vault_path` is the repo-relative path already stored on `documents.vault_path` (the same column `_writeback_routed_tags` selects at `src/brain/_capture_command.py:261-263`); it is `null` only in the pathological case where the row vanished between insert and read.

**Backward-compatibility risk and its guard.** `brain capture` today emits exactly `✓ captured {doc_id[:8]}  ({resolved_title})  [inbox]` (`src/brain/_capture_command.py:133`), and the empty-content path exits 1 with a red stderr message (`:79-84`). Wrappers, the demo GIF scripts, and any user alias parse that line. Guard: the JSON path is entered **only** when `--json` is passed, and the human branch keeps `typer.echo` on the identical f-string — the diff is a single `if json_output: emit_json(payload); return` inserted before line 133. Error paths (empty content, unconfigured vault, `VaultNoteSyncError`) keep their current stderr text and exit codes in **both** modes; `--json` never turns an error into JSON, because a hook asserting on `"status"` must fail loudly, not parse a success-shaped error. A regression test pins the human line byte-for-byte.

Second compat risk: `brain claude` currently has `no_args_is_help=True` with one subcommand (`src/brain/cli.py:277-281`). Adding two commands does not change `brain claude install-skill`'s behavior; `capture-hook` is `hidden=True` so `brain claude --help` gains exactly one visible line (`install-hooks`).

### 4. Module layout

| Path | New/changed | Purpose | Est. lines |
|---|---|---|---|
| `src/brain/claude_hook.py` | **new** | Pure Stop-hook decision logic: payload parse, transcript scan, substantive-work heuristic, sentinel. No Typer, no DB, no printing. | ~250 |
| `src/brain/claude_settings.py` | **new** | Pure `settings.json` read → merge/remove → serialize. No Typer, no printing; returns a result dataclass describing the change. | ~190 |
| `src/brain/cli_claude.py` | changed | Adds `install_hooks()` orchestration (script install + settings merge + backup + printing) beside the existing `install_skill()`. Adds `HookInstallError(BrainError)`. | 81 → ~215 |
| `src/brain/cli.py` | changed | Two Typer commands under `claude_app`: `install-hooks` and hidden `capture-hook`. Placed immediately after `install-skill` (`:9249`). | +~75 |
| `src/brain/_capture_command.py` | changed | `--json` flag on the callback + payload assembly. | 446 → ~475 |
| `src/brain/templates/claude/__init__.py` | **new** | Package marker so `importlib.resources.files("brain.templates.claude")` resolves. | 1 |
| `src/brain/templates/claude/brain-capture-hook.sh` | **new** | The shim. | 8 |
| `pyproject.toml` | changed | `"brain.templates.claude" = ["*.sh"]` under `[tool.setuptools.package-data]` (beside `pyproject.toml:105`) + the bytecode-strip twin beside `pyproject.toml:125`. | +2 |
| `skills/brain-memory/SKILL.md` | **new** | The read/write protocol skill. | ~130 |
| `skills/consult-brain/SKILL.md` | changed | One cross-link paragraph (mirrors the existing `brain-graph` hand-off at `:23-33`). | +4 |
| `docs/configuration.md` | changed | Add `brain-memory` to the hand-maintained symlink list at `:326-329`; document the four hook env vars. | +14 |
| `docs/cli-reference.md` | changed | `brain claude install-hooks`, `brain capture --json`. | +20 |
| `tests/test_claude_hook.py` | **new** | Decision-logic unit tests (no DB). | ~300 |
| `tests/test_claude_settings.py` | **new** | Merge/remove algorithm tests. | ~220 |
| `tests/test_claude_install_hooks.py` | **new** | CLI-level tests via `CliRunner` + `--target tmp_path`. | ~180 |
| `tests/test_cli_capture.py` | changed | `--json` shape + human-line regression. | +90 |

`src/brain/cli.py` is already ~9,300 lines, far over the 800-line cap; the split is a known deferred item (per the GraphRAG memory note). Adding 75 lines there is the consistent-placement choice since `claude_app` is defined at `:277`; all real logic goes in the two new sub-800-line modules.

### 5. Design detail

#### 5.1 Data flow

```
Claude Code Stop event
  └─> sh ~/.claude/hooks/brain-capture-hook.sh        (payload on stdin)
        └─> brain claude capture-hook                  (cli.py, thin)
              └─> claude_hook.decide(payload_bytes, env) -> HookDecision
                    ├─ parse payload            (json.loads, fail-open)
                    ├─ env gate                 (BRAIN_HOOK_ENABLED)
                    ├─ stop_hook_active gate
                    ├─ sentinel gate            ($BRAIN_HOME/run/claude-hook/)
                    └─ transcript scan          (TranscriptStats)
              └─> sys.stdout.write(json.dumps(...) + "\n")  when blocked
              └─> raise typer.Exit(code=0)      always
```

#### 5.2 The shim (`src/brain/templates/claude/brain-capture-hook.sh`)

```sh
#!/bin/sh
# brain capture nudge — installed by `brain claude install-hooks`.
# Fails open: a missing `brain` or any crash must never block the session.
command -v brain >/dev/null 2>&1 || exit 0
brain claude capture-hook || exit 0
```

`brain claude capture-hook || exit 0` (not `exec`) is deliberate: `exec` would surface a Python traceback's exit code to Claude Code, and a non-zero Stop-hook exit is user-visible noise. Stdout is inherited, so the decision JSON still reaches Claude Code unmodified.

Installation mirrors `ensure_shim` (`src/brain/bin/_launcher.py:20-64`): read package-data bytes, sha256-compare against the installed file, atomically replace on drift, then `os.chmod(path, 0o755)`. Unlike `ensure_shim` it prompts (`typer.confirm(..., abort=True)`) on a differing file unless `--force`, matching `_install` at `src/brain/cli_claude.py:51-55`.

#### 5.3 `src/brain/claude_hook.py`

```python
"""Pure decision logic for the Claude Code Stop hook (`brain claude capture-hook`)."""
```

Dataclasses (frozen — the project's immutability rule):

```python
@dataclass(frozen=True)
class HookPayload:
    """One Claude Code Stop-hook stdin payload, defensively parsed."""
    session_id: str | None
    transcript_path: Path | None
    stop_hook_active: bool

@dataclass(frozen=True)
class HookThresholds:
    """Env-tunable heuristics; built once per invocation."""
    enabled: bool                 # BRAIN_HOOK_ENABLED           default True
    min_tool_calls: int           # BRAIN_HOOK_MIN_TOOL_CALLS    default 12
    transcript_max_bytes: int     # BRAIN_HOOK_TRANSCRIPT_MAX_BYTES default 8_388_608
    sentinel_ttl_days: int        # BRAIN_HOOK_SENTINEL_TTL_DAYS default 7
    vault_root: Path              # from _default_vault_path()

@dataclass(frozen=True)
class TranscriptStats:
    """What one transcript scan learned. Never carries transcript text."""
    tool_calls: int
    mutated_files: bool
    wrote_to_brain: bool

@dataclass(frozen=True)
class HookDecision:
    """The hook's verdict. `reason` is non-empty iff `block` is True."""
    block: bool
    reason: str
    allow_because: str            # short machine tag, for tests + --dry-run
```

Signatures:

```python
def load_thresholds(env: Mapping[str, str]) -> HookThresholds: ...
def parse_payload(raw: bytes) -> HookPayload | None: ...
def scan_transcript(path: Path, *, thresholds: HookThresholds) -> TranscriptStats: ...
def is_brain_write_tool(name: str) -> bool: ...
def bash_command_writes_to_brain(command: str) -> bool: ...
def sentinel_path(session_id: str, *, run_root: Path) -> Path | None: ...
def claim_sentinel(path: Path | None) -> bool: ...
def prune_sentinels(run_root: Path, *, ttl_days: int, now: datetime) -> int: ...
def decide(
    raw_stdin: bytes,
    *,
    env: Mapping[str, str],
    run_root: Path,
    now: datetime,
) -> HookDecision: ...
```

`decide` takes `env`, `run_root`, and `now` as arguments — dependency inversion, so tests inject a dict and a `tmp_path` with zero monkey-patching of production modules.

**The decision ladder** (first match wins; every allow branch records `allow_because`):

| # | Condition | Verdict | `allow_because` |
|---|---|---|---|
| 1 | stdin is not a JSON object | allow | `unparseable_payload` |
| 2 | `BRAIN_HOOK_ENABLED` ∈ {`false`,`0`,`no`,`off`} (casefolded) | allow | `disabled` |
| 3 | `stop_hook_active` truthy | allow | `stop_hook_active` |
| 4 | sentinel for `session_id` already exists | allow | `already_nudged` |
| 5 | `transcript_path` missing / unreadable / not a file | allow | `no_transcript` |
| 6 | `stats.wrote_to_brain` | allow | `already_wrote` |
| 7 | `not stats.mutated_files and stats.tool_calls < min_tool_calls` | allow | `not_substantive` |
| 8 | otherwise, and `claim_sentinel()` succeeded | **block** | — |
| 8b | otherwise, but `claim_sentinel()` lost the race | allow | `sentinel_race` |

**Transcript scan.** Open in binary. If `size > transcript_max_bytes`, `seek(size - transcript_max_bytes)` and discard the first (probably partial) line. Iterate lines; `json.loads` each inside `try/except (json.JSONDecodeError, UnicodeDecodeError): continue` (a truncated or non-UTF-8 line is skipped, never fatal). For each record, take `record.get("message")` when it is a dict, then `content` when it is a list; for each block with `block.get("type") == "tool_use"`:

- `tool_calls += 1`
- if `block["name"]` ∈ `{"Edit", "Write", "NotebookEdit", "MultiEdit"}` → `mutated_files = True`
- if `is_brain_write_tool(block["name"])` → `wrote_to_brain = True`
- if `block["name"] == "Bash"` and `bash_command_writes_to_brain(block.get("input", {}).get("command", ""))` → `wrote_to_brain = True`
- if `block["name"]` ∈ `{"Edit","Write","MultiEdit"}` and `Path(input["file_path"])` is relative to `thresholds.vault_root` → `wrote_to_brain = True` (editing a vault note *is* writing to the brain)

Sidechain (subagent) records are counted the same as main-thread records: a teammate's work is still this session's work, and the transcript file is already session-scoped.

**Enumerating every write surface.** `is_brain_write_tool` normalizes first, because MCP namespacing is user-chosen — `claude mcp add brain -- brain-mcp` (README.md:119) yields `mcp__brain__brain_capture`, but a user who registers it as `second-brain` yields `mcp__second-brain__brain_capture`:

```python
_BRAIN_WRITE_TOOLS: frozenset[str] = frozenset({
    "brain_capture",        # mcp_server.py:851
    "brain_ingest_stdin",   # mcp_server.py:787
    "brain_note_new",       # mcp_server.py:1152
    "brain_daily",          # mcp_server.py:1273
    "brain_edit",           # mcp_server.py:985
    "brain_tag",            # mcp_server.py:923
})

def is_brain_write_tool(name: str) -> bool:
    """True when `name` is a brain write tool, namespaced (MCP) or bare."""
    bare = name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name
    return bare in _BRAIN_WRITE_TOOLS
```

Read-only MCP tools (`brain_search`, `brain_show`, `brain_list`, `brain_status`, `brain_backlinks`, `brain_brief`, `brain_ask`, `brain_graphrag_*`, `brain_timeline`, `brain_resurface`, `brain_gaps`, `brain_review_*`, `brain_orphans`, `brain_links`, `brain_link_proposal`) are deliberately **absent** — reading is not writing, and that is the whole point of the hook.

The Bash-side twin covers the same verbs plus the CLI-only ones:

```python
_BRAIN_WRITE_VERBS = (
    "capture", "ingest", "ingest-dir", "ingest-gmail", "ingest-stdin",
    "note", "daily", "edit", "tag", "mark-draft", "mark-published",
)
_BRAIN_WRITE_RE = re.compile(
    r"(?<![\w./-])brain\s+(?:" + "|".join(re.escape(v) for v in _BRAIN_WRITE_VERBS) + r")(?![\w-])"
)
```

The lookbehind stops `mybrain capture` / `./brain capture` false positives from a sibling repo; the lookahead stops `brain tag-something` and, critically, keeps `brain note new` matching while `brain notes` does not. `brain capture list` and `brain capture review` do match — acceptable and arguably correct: touching the inbox is engaging with memory. `brain search` / `brain show` / `brain graphrag` are not in the verb list.

**Sentinel.** Directory `run_root / "claude-hook"` where `run_root = _brain_home_root() / "run"` — the same tree PID files already use (`src/brain/bin/monitor.py:49-52`). File `<sanitized-session-id>.nudged`, empty. Sanitization:

```python
_SAFE_SESSION = re.compile(r"[^A-Za-z0-9._-]")

def sentinel_path(session_id: str, *, run_root: Path) -> Path | None:
    """Return the sentinel path for `session_id`, or None when unusable.

    The id is sanitized to `[A-Za-z0-9._-]` and truncated to 64 chars so a
    hostile payload can never escape `run_root` via `../`. An id that
    sanitizes to empty yields None — the caller then skips the sentinel
    entirely rather than writing to a guessable shared path.
    """
    safe = _SAFE_SESSION.sub("", session_id)[:64]
    return (run_root / "claude-hook" / f"{safe}.nudged") if safe else None
```

`claim_sentinel` creates with `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)` and returns `False` on `FileExistsError` — this makes the once-per-session guarantee hold under concurrent Stop events (parallel teammates) without a lock. `OSError` on the create (read-only FS, missing `$BRAIN_HOME`) also returns `False` → allow, because a hook that cannot record its own state must not nudge repeatedly.

`prune_sentinels` runs after a successful claim: iterate `run_root / "claude-hook"`, unlink entries whose `st_mtime` is older than `ttl_days`, bounded to the first 1000 entries, swallowing `OSError` per entry. `session_id is None` → skip the sentinel gate and skip the claim; the ladder still evaluates and may block (a payload with no session id is an unusual harness, and one nudge is the safe default).

**Threshold parsing.** Reuse `brain.config._parse_positive_int_env` (`src/brain/config.py:442-462`) for the three integer knobs, wrapped so a typo cannot break the session:

```python
def _positive_int(env_var: str, default: int) -> int:
    """Parse a positive-int knob, degrading to `default` on a bad value.

    Reuses `brain.config._parse_positive_int_env` (the project-wide idiom) but
    swallows `ConfigError`: this runs inside a Stop hook, where a typo in
    `.env` must degrade to the default, never abort the user's session.
    """
    try:
        return _parse_positive_int_env(env_var, default)
    except ConfigError:
        return default
```

Note this reads `os.environ` only, not the `.env` chain (`Config._load_field_dict`, `src/brain/config.py:756`), which is intentional: `Config.load()` would require `DATABASE_URL` and `load_minimal()` would pay dotenv-resolution cost on every Stop. Users set hook knobs as real env vars. Documented in `docs/configuration.md`.

#### 5.4 `src/brain/claude_settings.py`

```python
"""Non-destructive merge/removal of the brain Stop hook in Claude Code settings.json."""

_MARKERS: tuple[str, ...] = ("brain-capture-hook", "brain claude capture-hook")
_DEFAULT_TIMEOUT_SECONDS: int = 10

@dataclass(frozen=True)
class SettingsMerge:
    """Result of a merge/remove: the new document plus what changed."""
    document: dict[str, Any]
    changed: bool
    action: str          # "added" | "updated" | "unchanged" | "removed" | "absent"

class SettingsFormatError(BrainError):
    """settings.json exists but is not a JSON object we can safely edit."""
```

```python
def read_settings(path: Path) -> dict[str, Any]:
    """Return the parsed settings document, or {} when missing/empty.

    Missing file -> {}. File whose contents are whitespace-only -> {}.
    Invalid JSON, or valid JSON that is not an object, raises
    SettingsFormatError — we never silently discard a user's config.
    """

def merge_stop_hook(document: Mapping[str, Any], *, command: str,
                    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS) -> SettingsMerge:
    """Return a NEW document with the brain Stop hook present exactly once."""

def remove_stop_hook(document: Mapping[str, Any]) -> SettingsMerge:
    """Return a NEW document with every brain Stop hook entry removed."""

def serialize(document: Mapping[str, Any]) -> str:
    """json.dumps(indent=2) + trailing newline — the Claude Code house style."""
```

**Exact merge algorithm.**

1. `data = read_settings(path)`. Missing → `{}`. Whitespace-only → `{}`. Unparseable, or parses to a non-`dict` → `SettingsFormatError` → `HookInstallError` → exit 1, nothing written.
2. `hooks = data.get("hooks", {})`. If present and not a `dict` → `SettingsFormatError` ("`hooks` is a `list`, expected an object"). Refusing beats guessing.
3. `stop = hooks.get("Stop", [])`. If present and not a `list` → `SettingsFormatError`.
4. Scan for an existing brain entry: for each `group` in `stop` that is a `dict`, for each `entry` in `group.get("hooks", [])` that is a `dict` with `entry.get("type") == "command"` and a `str` `entry.get("command")` containing **any** marker in `_MARKERS`. Marker matching (not path equality) means a user who relocated the script, or who inlined `brain claude capture-hook` directly, is still recognized — no duplicate entry.
5. If found: if its `command` and `timeout` already equal the canonical values → `action="unchanged"`, `changed=False`. Otherwise produce a **new** dict with those two fields corrected → `action="updated"`.
6. If not found: append a new group at the **end** of `stop` so any pre-existing user Stop hook keeps running first:
   ```json
   {"hooks": [{"type": "command", "command": "/Users/you/.claude/hooks/brain-capture-hook.sh", "timeout": 10}]}
   ```
   `matcher` is omitted — `Stop` has no matcher semantics, and emitting `""` invites confusion.
7. Rebuild bottom-up with new objects at every level (`{**data, "hooks": {**hooks, "Stop": new_stop}}`) so the caller's document is never mutated. Every unrelated key — `env`, `statusLine`, `enabledPlugins`, `PreToolUse`, `PostToolUse`, other `Stop` groups — is carried through untouched by construction.
8. **Backup, then write.** When `changed` and the file existed with non-empty content, copy the *original bytes* to `path.with_name(f"{path.name}.brain-backup-{now:%Y%m%dT%H%M%SZ}")` (UTC) before writing. Then `atomic_write_text(path, serialize(document))` — reusing `src/brain/vault/_atomic.py:6`, whose sibling-tempfile + `os.replace` guarantees a crash cannot leave a half-written settings file. Reformatting to `indent=2` is why the backup is unconditional on change.
9. `--dry-run` stops after step 7 and prints the would-be action.

`remove_stop_hook` filters out matching entries, drops a group whose `hooks` list becomes empty, drops the `"Stop"` key when its list becomes empty, and drops `"hooks"` when that dict becomes empty — leaving no rubble. `action="absent"` when nothing matched.

#### 5.5 `brain capture --json`

Insert before the current echo at `src/brain/_capture_command.py:133`, inside the existing `with connect(...)` block so the `vault_path` read shares the connection:

```python
    if json_output:
        row = conn.execute(
            "SELECT vault_path FROM documents WHERE id = %s", (doc_id,)
        ).fetchone()
        emit_json({
            "document_id": doc_id,
            "id_prefix": doc_id[:8],
            "title": resolved_title,
            "tags": tags,
            "vault_path": row[0] if row else None,
            "status": "ingested",
        })
        return
    typer.echo(f"✓ captured {doc_id[:8]}  ({resolved_title})  [inbox]")
```

The one SQL statement is parameterized (`%s` + tuple), matching the identical read at `src/brain/_capture_command.py:261-263`. `emit_json` is `src/brain/format.py:35`.

#### 5.6 `skills/brain-memory/SKILL.md`

Frontmatter matches the `skills/` family exactly (`name:` + folded `description:` ending in `MANDATORY TRIGGERS:`, per `skills/consult-brain/SKILL.md:1-14` and `skills/ingest-brain/SKILL.md:1-18`) — **not** the `description:`/`when_to_use:` shape of the packaged `brain.templates.skill/SKILL.md` that `tests/test_claude_install_skill.py:137-138` asserts on. Two different skill families; do not cross them.

```yaml
---
name: brain-memory
description: >
  The read/write protocol for the user's second brain — search before
  answering, and write back the durable residue of a session. Use when a
  session produced a decision, constraint, or hard-won gotcha worth keeping,
  when a Stop-hook nudge asks for a capture pass, or when the user says to
  remember something. For retrieving and synthesizing existing content, use
  `consult-brain`; for bulk-importing external content, use `ingest-brain`.
  MANDATORY TRIGGERS: remember this, write this down, save this to my brain,
  note this for later, capture this decision, add this to memory, log this
  learning, don't forget that, keep this in the brain, capture pass, session
  capture, what did we learn.
---
```

Heading structure mirrors the sibling skills (`# Brain Memory`, a 2-3 sentence lede with the division-of-responsibility sentence and cross-links, then `## When this fires`, `## The read half`, `## The write half`, `## Dedupe before you write`, `## What is NOT worth writing`, `## Citing memory that changed the answer`, `## Operational notes`). Target ~130 lines — the same weight class as `consult-brain` (216) and `ingest-brain` (171).

**Division of responsibility, one sentence** (goes in the lede, and its mirror image is added to `skills/consult-brain/SKILL.md` beside the existing `brain-graph` hand-off at `:23-33`): *"`consult-brain` is the read protocol — how to retrieve and synthesize; `brain-memory` is the write protocol — when and how the durable residue of a session goes back in, plus the dedupe rule that stops the brain filling with near-duplicates."*

Content commitments:

- **Search before answering** — one `brain search "<terms>" --limit 5 --json` before answering anything about the user's own history, work, or prior decisions. Defers the retrieval mechanics to `consult-brain` rather than restating its filter table.
- **What IS worth writing** — a decision and the reason behind it; a constraint discovered the hard way (e.g. "pgvector caps HNSW at 2000 dims, so the 4096-dim backend has no index"); a fact about a person, system, or commitment that will not be obvious from the repo in three months; a correction to something already in the brain.
- **What is NOT worth writing** — a summary of what you just did; anything a `git log` or the code itself already says; restating a doc already in the brain; speculation; anything the user did not confirm; transient state ("the test is currently failing").
- **Dedupe before you write** — `brain search "<the claim in 3-5 words>" --limit 5 --json` first; if a hit already states it, say `already in the brain (<id-prefix>)` and stop; if a hit *contradicts* it, `brain edit` the existing note rather than adding a second competing one.
- **The write call** — `brain capture --json --tag <topic> --text "<one durable claim>"`, assert `"status": "ingested"` in the response, report the `id_prefix`. One capture per pass, one claim per capture.
- **Citing memory that materially changed an answer** — when a retrieved doc changed what you said, name it inline (`from your 2026-05-20 note (3f2a1b9c) …`) and log the signal with `brain rate <id-prefix> useful` (the same convention as `skills/consult-brain/SKILL.md:124-133`).

**Registration.** Nothing beyond dropping the directory in `skills/`: `bin/brain-skills-sync:37-41` enumerates `skills/*/` and `tests/test_brain_skills_sync.py:25-26` derives its expectation the same way, so the new skill is picked up and tested automatically. The **only** hand-maintained list is the symlink block in `docs/configuration.md:326-329`, which must gain a `brain-memory` line.

### 6. Edge cases and failure modes

1. **`stop_hook_active: true`** (Claude Code re-firing Stop after our own block). Allow immediately, branch 3, before any file I/O. This is the infinite-loop guard and it must precede the sentinel check so a nudge-then-immediate-stop cycle terminates even if the sentinel could not be written.
2. **`settings.json` is malformed JSON.** `SettingsFormatError` → `HookInstallError` → exit 1 with the parser's message and the file path. Nothing is written, no backup is made, `--force` does **not** override. Rationale: `--force` means "overwrite a stale hook script"; it never means "discard the user's editor config".
3. **`settings.json` has `hooks` as a list, or `hooks.Stop` as an object.** Same refusal path with a shape-specific message ("`hooks.Stop` is an object, expected a list"). Guessing at a repair risks silently disabling a working hook.
4. **`brain` not on `PATH`** (venv deactivated, uv shell, pipx uninstalled). `command -v brain >/dev/null 2>&1 || exit 0` in the shim. Zero output, zero delay, session ends normally.
5. **Postgres down / Ollama down / `DATABASE_URL` unset.** `claude_hook.decide` performs **no** DB, network, or `Config.load()` work — it is pure filesystem + env. The verdict is identical whether the DB is up or down. If the nudge fires and the subsequent `brain capture` then fails, that failure happens inside Claude's turn where it is visible and recoverable; the hook itself never blocks on infrastructure.
6. **Huge transcript.** A 400 MB `.jsonl` is read tail-first: `seek(size - transcript_max_bytes)`, discard the first partial line, scan the last 8 MiB. Worst case is bounded at ~8 MiB of `json.loads` (tens of milliseconds), well inside the 10 s hook timeout.
7. **Truncated / non-UTF-8 transcript line** (the harness was mid-write). `except (json.JSONDecodeError, UnicodeDecodeError): continue` per line. A partial tail line is skipped, never fatal.
8. **`transcript_path` absent, deleted, rotated, or a directory.** Branch 5 → allow, `allow_because="no_transcript"`. We never nudge on evidence we could not read.
9. **Two Stop events race** (parallel teammates finishing together). `os.O_CREAT | os.O_EXCL` makes exactly one claim succeed; the loser takes branch 8b and allows. Exactly one nudge.
10. **Hostile / weird `session_id`** (`"../../.ssh/authorized_keys"`). `_SAFE_SESSION.sub("", ...)` strips `/` and the id becomes `....sshauthorized_keys`, truncated to 64 chars, resolving inside `run_root/claude-hook/`. An id that sanitizes to empty yields `None` → sentinel skipped entirely, never a shared guessable path.
11. **User already has a `Stop` hook.** Our group is appended at the end of the existing `Stop` list; their entries are carried through by object construction, not by mutation. A pre-existing entry that already references either marker is *updated in place*, never duplicated.
12. **MCP server registered under a name other than `brain`.** `is_brain_write_tool` matches on the segment after the final `__`, so `mcp__second-brain__brain_capture` and `mcp__work-brain__brain_capture` both count as writes.
13. **User relocated the hook script** or inlined `brain claude capture-hook` as the command. Marker-substring matching (`_MARKERS`) recognizes both, so a re-run reports "already present" rather than appending a second nudge.
14. **`brain capture --json` on a document row that vanished** between `create_vault_note` and the `vault_path` read (concurrent `brain rm`). `row` is `None` → `"vault_path": null`; `document_id` and `status` are still correct and the exit code stays 0.
15. **`--uninstall` when no entry exists.** `action="absent"` → prints `no brain Stop hook found in <path>`; the hook script is still removed if present; the `hooks/` directory is `rmdir`'d only when empty, with a stderr warning otherwise — the exact contract of `_uninstall` at `src/brain/cli_claude.py:60-81`.

### 7. Security and safety

| Risk | Guard |
|---|---|
| **Prompt injection through the transcript.** A transcript contains attacker-influenced text (a fetched web page, a hostile file). If any of it reached the `reason` string it would become an instruction Claude executes. | `reason` is a **module-level constant**. `scan_transcript` returns only three primitives (`int`, `bool`, `bool`) — `TranscriptStats` structurally cannot carry text. Nothing read from the transcript is ever interpolated, logged, or emitted. |
| **Path traversal via `session_id`.** | `_SAFE_SESSION` strips everything outside `[A-Za-z0-9._-]`, truncates to 64, and returns `None` on an empty result. The sentinel can only ever land inside `run_root/claude-hook/`. |
| **Clobbering the user's `settings.json`.** | Timestamped backup of the original bytes before any change; refusal (exit 1, no write) on malformed JSON or an unexpected shape; `atomic_write_text` so a crash mid-write cannot truncate; merge builds new objects and never mutates the parsed document; `--dry-run` to preview. |
| **A hook that fires too often is worse than none.** | Five independent brakes: `BRAIN_HOOK_ENABLED`, `stop_hook_active`, the once-per-session sentinel, the already-wrote check, and the substantive-work floor. A read-only session with 11 tool calls and no file mutation is silent. |
| **Blocking the user's session on infrastructure failure.** | The shim `|| exit 0`s any crash; `capture-hook` wraps its whole body in `except Exception` → exit 0; the decision path touches no DB, no network, no `Config.load()`; the settings entry carries `"timeout": 10`. Worst case the hook is a no-op. |
| **PII / content leakage.** | The hook logs nothing at all — no stdout on allow, no stderr, no log file. `brain capture --json` echoes `title`, which for an auto-title derives from content (`src/brain/capture.py:42-49`); this is exactly the exposure the existing human line at `src/brain/_capture_command.py:133` already has, so `--json` adds no new leak. The mcp_server warning at `src/brain/mcp_server.py:893-896` ("never log `resolved_title`") is honored: nothing new logs it. |
| **Writing outside the user's config root.** | Every path is derived from `target_root` (default `~/.claude`) or `$BRAIN_HOME/run/`. No absolute path from the payload is ever opened for writing — `transcript_path` is opened **read-only** and only after `.is_file()`. |
| **Executable-bit smuggling.** | The installed script is written from package data (`brain.templates.claude`) only, sha256-compared, and `chmod 0o755`. Its content is never assembled from user input. |
| **Test suite touching the real `~/.claude` or the production DB.** | Every test passes `--target tmp_path` / injects `run_root=tmp_path`; `claude_hook` and `claude_settings` have no DB dependency at all, so no test in this section connects to Postgres except the `brain capture --json` integration test, which uses the standard `test_db` fixture (`tests/conftest.py:370`) on port 5434. |

### 8. Test plan

**Migration:** none. This section requires **no** schema change — nothing is persisted to Postgres beyond what `brain capture` already writes. Neither `024_agent_attribution.sql` nor `025_document_sensitivity.sql` is touched here.

**`tests/test_claude_hook.py`** (pure logic, no DB, target 95%):

- ⛳ **RED-FIRST — the test that proves the gap:** `test_substantive_session_without_brain_write_is_nudged` — build a synthetic transcript with 15 `tool_use` blocks (`Read`, `Bash` running `brain search …`, one `Edit`), call `decide(payload, env={}, run_root=tmp_path, now=...)`, assert `decision.block is True` and `"brain capture" in decision.reason`. Fails at import today (`brain.claude_hook` does not exist) and continues to fail until the ladder is implemented. This is the whole thesis of the section in one assertion.
- `test_stop_hook_active_allows_immediately` — `stop_hook_active: true` with a nudge-worthy transcript → `block is False`, `allow_because == "stop_hook_active"`, and the sentinel file was **not** created.
- `test_disabled_by_env` — `env={"BRAIN_HOOK_ENABLED": "false"}` → allow; parameterized over `false/0/no/off/FALSE`.
- `test_readonly_session_below_threshold_is_silent` — 5 `Read` calls, no mutation → `allow_because == "not_substantive"`.
- `test_threshold_override` — same 5-call transcript with `BRAIN_HOOK_MIN_TOOL_CALLS=3` → blocks. Proves the knob is wired.
- `test_bad_threshold_value_falls_back_to_default` — `BRAIN_HOOK_MIN_TOOL_CALLS="banana"` → default 12 applied, no exception escapes.
- `test_single_edit_is_substantive_without_call_count` — 2 tool calls, one of them `Write` → blocks.
- `test_mcp_namespaced_brain_capture_counts_as_write` — parameterized over `mcp__brain__brain_capture`, `mcp__second-brain__brain_capture`, `mcp__work__brain_note_new`, bare `brain_edit` → `allow_because == "already_wrote"`.
- `test_bash_brain_write_verbs_count_as_write` — parameterized over `brain capture --text x`, `echo hi | brain capture`, `brain ingest-stdin --source slack`, `brain note new "T"`, `brain daily`, `brain tag abc123 +x`.
- `test_bash_read_verbs_do_not_count_as_write` — parameterized over `brain search "x"`, `brain show abc123`, `brain status`, `brain graphrag search "x"`, and the negative-lookbehind cases `mybrain capture` and `./other/brain-ish capture` → still blocks.
- `test_vault_file_edit_counts_as_write` — `Write` whose `file_path` is under the configured vault root → `already_wrote`; a `Write` outside it → still nudged.
- `test_sentinel_makes_nudge_once_per_session` — call `decide` twice with the same payload; first blocks, second returns `allow_because == "already_nudged"`.
- `test_sentinel_path_sanitizes_traversal` — `session_id="../../etc/passwd"` → the returned path's `.parent` is `run_root/"claude-hook"`; `session_id="///"` → `None`.
- `test_concurrent_claim_only_one_wins` — pre-create the sentinel, then `claim_sentinel` → `False` and `decide` → `allow_because == "sentinel_race"`.
- `test_prune_removes_stale_sentinels_only` — two sentinels with `os.utime`-set mtimes (1 day, 30 days), `ttl_days=7` → the old one is gone, the fresh one survives.
- `test_unparseable_stdin_allows` — `b""`, `b"not json"`, `b"[]"` → allow, `allow_because == "unparseable_payload"`.
- `test_missing_transcript_allows` — `transcript_path` pointing at a nonexistent file, and at a directory → `no_transcript`.
- `test_truncated_jsonl_line_is_skipped` — a transcript whose last line is a half-written object → the earlier lines still count, no exception.
- `test_oversized_transcript_reads_tail_only` — write >`transcript_max_bytes` of padding lines followed by the real records, with a tiny `BRAIN_HOOK_TRANSCRIPT_MAX_BYTES` → the tail records are counted and the run completes.
- `test_reason_never_contains_transcript_text` — seed the transcript with the sentinel string `"INJECTED-PAYLOAD-DO-NOT-ECHO"` in a `Bash` command and a file path; assert it is absent from `decision.reason`. This is the prompt-injection regression test.

**`tests/test_claude_settings.py`** (pure logic, target 95%):

- `test_missing_file_yields_empty_document` and `test_whitespace_only_file_yields_empty_document`.
- `test_malformed_json_raises` / `test_top_level_list_raises` / `test_hooks_not_object_raises` / `test_stop_not_list_raises` — each asserts `SettingsFormatError` and that the file on disk is byte-unchanged.
- `test_merge_into_document_without_hooks_key` — reproduces the **live** shape from this machine (`env`, `statusLine`, `enabledPlugins`, … and no `hooks`) → `action == "added"` and every original key survives with its original value.
- `test_merge_preserves_existing_stop_and_other_events` — a document with a user `Stop` group and a `PostToolUse` block → both intact, ours appended **last**.
- `test_merge_is_idempotent` — merging twice yields `action == "unchanged"` and an identical document.
- `test_merge_updates_stale_command_in_place` — an existing entry whose `command` points at an old path but contains a marker → `action == "updated"`, `len(stop) == 1` (no duplicate).
- `test_merge_recognizes_inlined_command` — `"command": "brain claude capture-hook"` → recognized, not duplicated.
- `test_merge_does_not_mutate_input` — `deepcopy` the input, merge, assert the original is unchanged (the immutability rule, enforced).
- `test_remove_drops_entry_group_and_empty_keys` — after removal a document that had only our hook has neither `Stop` nor `hooks`; one that also had a user hook keeps it.
- `test_remove_when_absent_reports_absent`.
- `test_serialize_round_trips` — `json.loads(serialize(d)) == d`, output ends with `\n`, uses 2-space indent.

**`tests/test_claude_install_hooks.py`** (CLI via `typer.testing.CliRunner`, `--target tmp_path`, no DB):

- `test_fresh_install_writes_script_and_settings` — script exists, mode `& 0o111`, bytes equal package data; `settings.json` parses and contains one Stop entry; stdout carries `hook script installed` and `Stop hook added`.
- `test_reinstall_is_idempotent` — second run prints `up to date` / `already present`, script mtime unchanged (mirroring `tests/test_claude_install_skill.py:43-49`).
- `test_force_overwrites_drifted_script` — pre-write different bytes, `--force` → package bytes restored, no prompt.
- `test_backup_created_before_rewrite` — a pre-existing `settings.json` → exactly one `settings.json.brain-backup-*` sibling whose bytes equal the pre-run original.
- `test_malformed_settings_exits_1_and_writes_nothing` — `exit_code == 1`, `"not valid JSON"` in output, `settings.json` byte-unchanged, **no** backup created, script not installed.
- `test_dry_run_writes_nothing` — no script, no settings file, no backup; stdout has `would install` / `(dry run`.
- `test_uninstall_removes_entry_and_script` — round-trip install → uninstall → `settings.json` has no `hooks` key and the script is gone.
- `test_uninstall_preserves_user_stop_hook` — a foreign Stop entry survives uninstall verbatim.
- `test_capture_hook_command_allows_on_garbage_stdin` — `runner.invoke(app, ["claude","capture-hook"], input="not json")` → `exit_code == 0`, empty stdout.
- `test_capture_hook_command_emits_single_line_json_on_block` — feed a nudge-worthy payload; assert stdout is exactly one line, `json.loads` succeeds, `["decision"] == "block"`. Guards against Rich soft-wrapping (`brain claude capture-hook` deliberately uses `sys.stdout.write(json.dumps(...))`, **not** `emit_json`, for this reason).
- `test_shim_template_is_shipped` — `resource_files("brain.templates.claude") / "brain-capture-hook.sh"` reads; content contains `command -v brain` and `|| exit 0`.

**`tests/test_cli_capture.py`** (extend; uses the existing `test_db` fixture at `tests/conftest.py:370`):

- `test_capture_json_shape` — `brain capture --json --text "<synthetic>" --tag pgvector` → parsed stdout has exactly the six documented keys; `document_id` is a valid UUID; `id_prefix == document_id[:8]`; `"inbox"` in `tags`; `vault_path` starts with `capture/`; `status == "ingested"`.
- `test_capture_json_suppresses_human_line` — `"✓ captured"` absent from stdout.
- `test_capture_human_output_unchanged` — the **backward-compat regression test**: without `--json`, stdout matches `^✓ captured [0-9a-f]{8}  \(.+\)  \[inbox\]$`.
- `test_capture_json_error_path_is_not_json` — `--json --text "   "` → `exit_code == 1`, stdout is not valid JSON, stderr carries the existing red message. A hook must not mistake an error for a success payload.

**`tests/test_brain_skills_sync.py`**: no change needed — `_expected_skills()` (`:25-26`) enumerates `skills/*/`, so `brain-memory` is covered automatically the moment the directory exists. A one-line `test_brain_memory_skill_frontmatter` is added asserting the file starts with `---\nname: brain-memory` and its `description` contains `MANDATORY TRIGGERS:`, matching the family convention at `skills/consult-brain/SKILL.md:1-14`.

**Coverage.** `claude_hook.py` and `claude_settings.py` are pure logic → 95% floor. The `cli_claude.py` additions and the two Typer commands → 85%. Every branch of the eight-step ladder has a named test, so the module reaches 95% without contrivance.

### 9. Open questions (each with the recommended answer)

1. **Is 12 tool calls the right substantive-work floor?** — **Recommended: yes, keep the reference project's 12 as the default**, exposed as `BRAIN_HOOK_MIN_TOOL_CALLS`. It is the one number in this design with no principled derivation, and the cost of being wrong is asymmetric: too low and the hook becomes noise the user disables permanently; too high and we merely miss some captures. Ship 12, revisit after a week of real sessions.

2. **Should the hook nudge on *every* Stop, or once per session?** — **Recommended: once per session, via the `O_EXCL` sentinel.** The reference implementation relies on the transcript scan alone, which re-nudges forever if the agent declines. A declined nudge is a decision, not a failure; re-asking is the fastest route to the user turning the hook off.

3. **Should `brain capture --json` return `"status": "ingested"` or `"status": "captured"`?** — **Recommended: `"ingested"`**, matching `src/brain/mcp_server.py:915-918` exactly. "Captured" reads better but would give agents two vocabularies for one operation; the skill tells them to assert on `"ingested"` and that string then works on both surfaces.

4. **Should the hook count MCP read tools (`brain_search`) toward the tool-call floor?** — **Recommended: yes, count them.** They are tool calls and they indicate engagement. They do **not** set `wrote_to_brain` — only the six write tools and the write verbs do. A session that searched 15 times and learned something is exactly the session worth nudging.

5. **Should `brain claude install-hooks` be folded into `brain setup`?** — **Recommended: no, keep it opt-in.** `brain setup --profile` already installs launchd agents behind an explicit opt-in; a `Stop` hook mutates a config file outside the project and changes the behavior of every Claude Code session on the machine, including in unrelated repos. It must be a deliberate, separately-reversible act. Mention it in the `brain setup` completion output as a suggested next step.

6. **Should the hook be scoped to the second-brain repo (via the payload's `cwd`)?** — **Recommended: no, keep it global.** Memory-worthy sessions happen everywhere — arguably *more* outside this repo. The `cwd` is available in the payload if a future `BRAIN_HOOK_CWD_ALLOWLIST` proves necessary; do not build it speculatively (YAGNI).

7. **Should the four `BRAIN_HOOK_*` knobs become `Config` fields?** — **Recommended: no.** `Config.load()` requires `DATABASE_URL` (`src/brain/config.py:790-791`) and `load_minimal()` pays the full four-file dotenv-resolution cost (`src/brain/config.py:764-788`) on *every* Stop event. `claude_hook` reads `os.environ` directly through the wrapped `_parse_positive_int_env` (`src/brain/config.py:442`), keeping the hook DB-independent by construction — which is precisely the property §7 depends on. Document the vars in `docs/configuration.md` and note they must be real env vars, not `.env` entries.

8. **Should `brain doctor` report hook health?** — **Recommended: yes, but as a follow-up, not in this section.** A one-line soft check ("Stop hook: installed / not installed") is cheap and discoverable, but `brain doctor` is out of this section's blast radius and adding it here risks a merge collision with the SAFETY section. File it as a follow-up task.
