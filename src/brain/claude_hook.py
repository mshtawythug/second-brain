"""Pure decision logic for the Claude Code Stop hook (`brain claude capture-hook`)."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import DEFAULT_VAULT_PATH

# ---------------------------------------------------------------------------
# The nudge text. A MODULE-LEVEL CONSTANT, and it must stay one.
#
# A transcript contains attacker-influenced text (a fetched web page, a hostile
# file, a pasted diff). Anything interpolated into `reason` becomes an
# instruction Claude executes on the next turn. Nothing read from the transcript
# is ever formatted into this string -- see `TranscriptStats`, which carries
# three primitives and structurally cannot transport text.
# ---------------------------------------------------------------------------
NUDGE_REASON: str = (
    "This session did substantive work but nothing was written back to the "
    "second brain.\n"
    "Do exactly ONE capture pass, then stop:\n"
    '1. Search first: `brain search "<2-4 keywords for what you learned>" '
    "--limit 5 --json`.\n"
    "2. If a hit already states it, do NOT write — say "
    '"already in the brain (<id-prefix>)" and stop.\n'
    "3. Otherwise write ONE durable note: `brain capture --json --tag <topic> "
    '--text "<the durable fact, decision, constraint, or gotcha>"`. '
    'Assert on the returned `"status": "ingested"`.\n'
    "4. If the only outcome was a routine edit, a lookup, or something obvious "
    "from the repo, skip and say so in one line.\n"
    "Do not start new work. Do not run more than one capture."
)

# Env knobs. Read from the injected mapping, never from `.env` -- see
# `load_thresholds`.
ENV_ENABLED = "BRAIN_HOOK_ENABLED"
ENV_MIN_TOOL_CALLS = "BRAIN_HOOK_MIN_TOOL_CALLS"
ENV_TRANSCRIPT_MAX_BYTES = "BRAIN_HOOK_TRANSCRIPT_MAX_BYTES"
ENV_SENTINEL_TTL_DAYS = "BRAIN_HOOK_SENTINEL_TTL_DAYS"

#: Tool calls below this floor, with no file mutation, read as a lookup session.
#: The one number here with no principled derivation; overridable so a user who
#: finds it noisy raises it instead of disabling the hook outright.
DEFAULT_MIN_TOOL_CALLS = 12
#: Tail window scanned on an oversized transcript (8 MiB).
DEFAULT_TRANSCRIPT_MAX_BYTES = 8_388_608
#: How long a session sentinel survives before pruning sweeps it.
DEFAULT_SENTINEL_TTL_DAYS = 7

#: Casefolded values that switch the hook off.
_FALSEY = frozenset({"false", "0", "no", "off"})

#: Subdirectory of ``$BRAIN_HOME/run`` holding one sentinel per nudged session.
SENTINEL_DIRNAME = "claude-hook"
_SENTINEL_SUFFIX = ".nudged"
#: Upper bound on entries examined by one prune sweep.
_PRUNE_MAX_ENTRIES = 1000

#: Built-in tools that mutate files on disk.
_MUTATING_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
#: Of those, the ones whose ``file_path`` is worth testing against the vault root.
_FILE_PATH_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit"})

# Every MCP tool that WRITES to the brain. Read-only tools (brain_search,
# brain_show, brain_list, brain_status, brain_ask, brain_graphrag_*, ...) are
# deliberately absent: reading is not writing, and that gap is the whole point
# of the hook.
_BRAIN_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "brain_capture",
        "brain_ingest_stdin",
        "brain_note_new",
        "brain_daily",
        "brain_edit",
        "brain_tag",
    }
)

# The Bash-side twin: CLI verbs that write. `search` / `show` / `graphrag` are
# absent for the same reason.
_BRAIN_WRITE_VERBS: tuple[str, ...] = (
    "capture",
    "ingest",
    "ingest-dir",
    "ingest-gmail",
    "ingest-stdin",
    "note",
    "daily",
    "edit",
    "tag",
    "mark-draft",
    "mark-published",
)
# The negative lookbehind rejects `mybrain capture` and `./brain capture` from a
# sibling repo; the lookahead rejects `brain tag-something` while keeping
# `brain note new` a match and `brain notes` a non-match.
_BRAIN_WRITE_RE = re.compile(
    r"(?<![\w./-])brain\s+(?:"
    + "|".join(re.escape(verb) for verb in _BRAIN_WRITE_VERBS)
    + r")(?![\w-])"
)

#: Everything outside this class is stripped from a session id before it is
#: used as a filename -- a hostile payload can never escape ``run_root``.
_SAFE_SESSION = re.compile(r"[^A-Za-z0-9._-]")
_MAX_SESSION_CHARS = 64


@dataclass(frozen=True)
class HookPayload:
    """One Claude Code Stop-hook stdin payload, defensively parsed."""

    session_id: str | None
    transcript_path: Path | None
    stop_hook_active: bool


@dataclass(frozen=True)
class HookThresholds:
    """Env-tunable heuristics; built once per invocation."""

    enabled: bool
    min_tool_calls: int
    transcript_max_bytes: int
    sentinel_ttl_days: int
    vault_root: Path


@dataclass(frozen=True)
class TranscriptStats:
    """What one transcript scan learned. Never carries transcript text.

    Three primitives, by construction: there is no field a hostile transcript
    could ride into :data:`NUDGE_REASON` on.
    """

    tool_calls: int
    mutated_files: bool
    wrote_to_brain: bool


@dataclass(frozen=True)
class HookDecision:
    """The hook's verdict. ``reason`` is non-empty iff ``block`` is True."""

    block: bool
    reason: str
    allow_because: str


def _positive_int(env: Mapping[str, str], env_var: str, default: int) -> int:
    """Parse a positive-int knob from ``env``, degrading to ``default``.

    Mirrors :func:`brain.config._parse_positive_int_env`'s semantics (unset or
    blank yields the default; anything non-parseable or ``< 1`` is rejected) but
    reads the *injected* mapping rather than ``os.environ``, which is what lets
    ``decide`` be tested with a plain dict and no monkey-patching. It also
    swallows the bad value instead of raising: this runs inside a Stop hook,
    where a typo must degrade to the default, never abort the user's session.
    """
    raw = env.get(env_var)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


def load_thresholds(env: Mapping[str, str]) -> HookThresholds:
    """Build the tunables from ``env``.

    Reads the injected mapping only -- never the ``.env`` chain. ``Config.load``
    would require ``DATABASE_URL`` and ``load_minimal`` would pay dotenv
    resolution on every Stop event; both would couple the hook to
    infrastructure it must survive the absence of. The four ``BRAIN_HOOK_*``
    knobs are therefore real environment variables, not ``.env`` entries.
    """
    raw_vault = env.get("BRAIN_VAULT_PATH")
    return HookThresholds(
        enabled=env.get(ENV_ENABLED, "").strip().casefold() not in _FALSEY,
        min_tool_calls=_positive_int(env, ENV_MIN_TOOL_CALLS, DEFAULT_MIN_TOOL_CALLS),
        transcript_max_bytes=_positive_int(
            env, ENV_TRANSCRIPT_MAX_BYTES, DEFAULT_TRANSCRIPT_MAX_BYTES
        ),
        sentinel_ttl_days=_positive_int(
            env, ENV_SENTINEL_TTL_DAYS, DEFAULT_SENTINEL_TTL_DAYS
        ),
        vault_root=Path(raw_vault).expanduser() if raw_vault else DEFAULT_VAULT_PATH,
    )


def parse_payload(raw: bytes) -> HookPayload | None:
    """Parse the Stop payload, returning ``None`` on anything unusable.

    Fail-open by design: empty stdin, non-JSON bytes, and a JSON value that is
    not an object all yield ``None``, which the ladder reads as "allow".
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None

    session_id = parsed.get("session_id")
    transcript = parsed.get("transcript_path")
    return HookPayload(
        session_id=session_id if isinstance(session_id, str) and session_id else None,
        transcript_path=(
            Path(transcript) if isinstance(transcript, str) and transcript else None
        ),
        stop_hook_active=bool(parsed.get("stop_hook_active")),
    )


def is_brain_write_tool(name: str) -> bool:
    """True when ``name`` is a brain write tool, namespaced (MCP) or bare.

    MCP namespacing is user-chosen: ``claude mcp add brain -- brain-mcp`` yields
    ``mcp__brain__brain_capture``, but registering the same server as
    ``second-brain`` yields ``mcp__second-brain__brain_capture``. Matching on the
    segment after the final ``__`` recognizes both.
    """
    bare = name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name
    return bare in _BRAIN_WRITE_TOOLS


def bash_command_writes_to_brain(command: str) -> bool:
    """True when a Bash command line invokes a brain write verb."""
    return bool(_BRAIN_WRITE_RE.search(command))


def _is_within(path_text: str, root: Path) -> bool:
    """True when ``path_text`` sits inside ``root``.

    Purely lexical (``expanduser`` + ``is_relative_to``) -- no filesystem
    access, so a path read out of a transcript is never stat'ed.
    """
    try:
        candidate = Path(path_text).expanduser()
    except (ValueError, RuntimeError):
        return False
    return candidate.is_relative_to(root)


def _tool_input(block: Mapping[str, Any]) -> Mapping[str, Any]:
    """The block's ``input`` mapping, or an empty one when absent/malformed."""
    raw = block.get("input")
    return raw if isinstance(raw, Mapping) else {}


def _iter_tool_use_blocks(record: Any) -> list[Mapping[str, Any]]:
    """Every ``tool_use`` block in one transcript record.

    Sidechain (subagent) records are treated exactly like main-thread ones: a
    teammate's work is still this session's work, and the transcript file is
    already session-scoped.
    """
    if not isinstance(record, Mapping):
        return []
    message = record.get("message")
    if not isinstance(message, Mapping):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") == "tool_use"
        and isinstance(block.get("name"), str)
    ]


def scan_transcript(path: Path, *, thresholds: HookThresholds) -> TranscriptStats:
    """Count tool calls and detect brain writes in a JSONL transcript.

    Opened read-only, and only by a caller that already checked ``is_file()``.
    An oversized transcript is read tail-first: seek to the last
    ``transcript_max_bytes`` and discard the first (probably partial) line, so
    the worst case is bounded regardless of how long the session ran.

    A truncated or non-UTF-8 line is skipped, never fatal -- the harness may be
    mid-write when Stop fires.
    """
    tool_calls = 0
    mutated_files = False
    wrote_to_brain = False

    with path.open("rb") as handle:
        size = path.stat().st_size
        if size > thresholds.transcript_max_bytes:
            handle.seek(size - thresholds.transcript_max_bytes)
            handle.readline()  # Discard the partial line we landed inside.

        for raw_line in handle:
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                continue

            for block in _iter_tool_use_blocks(record):
                name = str(block["name"])
                tool_calls += 1

                if name in _MUTATING_TOOLS:
                    mutated_files = True
                if is_brain_write_tool(name):
                    wrote_to_brain = True

                if name == "Bash":
                    command = _tool_input(block).get("command")
                    if isinstance(command, str) and bash_command_writes_to_brain(command):
                        wrote_to_brain = True
                elif name in _FILE_PATH_TOOLS:
                    # Editing a vault note IS writing to the brain.
                    file_path = _tool_input(block).get("file_path")
                    if isinstance(file_path, str) and _is_within(
                        file_path, thresholds.vault_root
                    ):
                        wrote_to_brain = True

    return TranscriptStats(
        tool_calls=tool_calls, mutated_files=mutated_files, wrote_to_brain=wrote_to_brain
    )


def sentinel_path(session_id: str, *, run_root: Path) -> Path | None:
    """Return the sentinel path for ``session_id``, or None when unusable.

    The id is sanitized to ``[A-Za-z0-9._-]`` and truncated to 64 chars so a
    hostile payload can never escape ``run_root`` via ``../``. An id that
    sanitizes to empty yields None -- the caller then skips the sentinel
    entirely rather than writing to a guessable shared path.
    """
    safe = _SAFE_SESSION.sub("", session_id)[:_MAX_SESSION_CHARS]
    return (run_root / SENTINEL_DIRNAME / f"{safe}{_SENTINEL_SUFFIX}") if safe else None


def claim_sentinel(path: Path | None) -> bool:
    """Atomically claim ``path``; False when it already exists or cannot be made.

    ``O_CREAT | O_EXCL`` makes the once-per-session guarantee hold under
    concurrent Stop events (parallel teammates finishing together) without a
    lock: exactly one caller wins. An ``OSError`` -- read-only filesystem,
    unwritable ``$BRAIN_HOME`` -- also returns False, because a hook that cannot
    record its own state must not nudge on every Stop forever.
    """
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False
    os.close(fd)
    return True


def prune_sentinels(run_root: Path, *, ttl_days: int, now: datetime) -> int:
    """Unlink sentinels older than ``ttl_days``; return how many were removed.

    Bounded to the first :data:`_PRUNE_MAX_ENTRIES` entries and swallowing
    ``OSError`` per entry: housekeeping must never be the reason a Stop hook
    raises.
    """
    directory = run_root / SENTINEL_DIRNAME
    cutoff = (now - timedelta(days=ttl_days)).timestamp()
    removed = 0
    try:
        entries = sorted(directory.iterdir())[:_PRUNE_MAX_ENTRIES]
    except OSError:
        return 0

    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _allow(reason_tag: str) -> HookDecision:
    """An allow verdict carrying only a short machine tag."""
    return HookDecision(block=False, reason="", allow_because=reason_tag)


def decide(
    raw_stdin: bytes,
    *,
    env: Mapping[str, str],
    run_root: Path,
    now: datetime,
) -> HookDecision:
    """Decide whether this Stop event earns exactly one capture nudge.

    ``env``, ``run_root``, and ``now`` are arguments rather than globals so the
    whole ladder is testable with a dict and a ``tmp_path``. The function
    performs no database, network, or ``Config.load()`` work -- its verdict is
    identical whether Postgres is up or down, which is the property the safety
    argument rests on.

    First match wins:

    1. unparseable payload   2. disabled          3. ``stop_hook_active``
    4. already nudged        5. no transcript     6. already wrote
    7. not substantive       8. otherwise block -- unless the claim lost a race.

    Order matters: (3) MUST precede (4) so a nudge-then-stop cycle terminates
    even when the sentinel could not be written.
    """
    payload = parse_payload(raw_stdin)
    if payload is None:
        return _allow("unparseable_payload")

    thresholds = load_thresholds(env)
    if not thresholds.enabled:
        return _allow("disabled")

    if payload.stop_hook_active:
        return _allow("stop_hook_active")

    # A payload with no session id gets no sentinel at all: skip the gate, skip
    # the claim. One nudge is the safe default for an unusual harness.
    sentinel = (
        sentinel_path(payload.session_id, run_root=run_root)
        if payload.session_id is not None
        else None
    )
    if sentinel is not None and sentinel.exists():
        return _allow("already_nudged")

    if payload.transcript_path is None or not payload.transcript_path.is_file():
        return _allow("no_transcript")

    try:
        stats = scan_transcript(payload.transcript_path, thresholds=thresholds)
    except OSError:
        # Rotated or permission-denied between the is_file() check and the open.
        return _allow("no_transcript")

    if stats.wrote_to_brain:
        return _allow("already_wrote")

    if not stats.mutated_files and stats.tool_calls < thresholds.min_tool_calls:
        return _allow("not_substantive")

    if payload.session_id is not None:
        if not claim_sentinel(sentinel):
            return _allow("sentinel_race")
        prune_sentinels(run_root, ttl_days=thresholds.sentinel_ttl_days, now=now)

    return HookDecision(block=True, reason=NUDGE_REASON, allow_because="")
