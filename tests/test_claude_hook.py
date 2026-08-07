"""Decision logic for the Claude Code Stop hook (`brain claude capture-hook`).

Pure logic: no database, no network, no ``Config.load()``. ``decide`` takes its
environment, its runtime-state root, and its clock as arguments, so every test
here injects a plain dict and a ``tmp_path`` — there is nothing to monkey-patch.

All transcript content is synthetic.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from brain.claude_hook import (
    DEFAULT_MIN_TOOL_CALLS,
    NUDGE_REASON,
    SENTINEL_DIRNAME,
    HookThresholds,
    bash_command_writes_to_brain,
    claim_sentinel,
    decide,
    is_brain_write_tool,
    load_thresholds,
    parse_payload,
    prune_sentinels,
    scan_transcript,
    sentinel_path,
)

NOW = datetime(2026, 7, 25, 18, 41, 2, tzinfo=UTC)
DAY_SECONDS = 86_400


# ---------------------------------------------------------------------------
# Synthetic transcript builders
# ---------------------------------------------------------------------------


def _tool_use(name: str, **tool_input: Any) -> dict[str, Any]:
    """One synthetic assistant record carrying a single ``tool_use`` block."""
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]},
    }


def _write_transcript(path: Path, records: list[dict[str, Any]]) -> Path:
    """Write ``records`` as JSONL, the shape Claude Code writes."""
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )
    return path


def _payload(
    transcript: Path | str, *, session_id: str | None = "s-1", active: bool = False
) -> bytes:
    body: dict[str, Any] = {
        "transcript_path": str(transcript),
        "cwd": "/tmp/synthetic-project",
        "hook_event_name": "Stop",
        "stop_hook_active": active,
    }
    if session_id is not None:
        body["session_id"] = session_id
    return json.dumps(body).encode()


def _substantive_records() -> list[dict[str, Any]]:
    """15 tool calls, one file mutation, no brain write — the nudge-worthy shape."""
    records = [_tool_use("Read", file_path=f"/tmp/synthetic/mod_{i}.py") for i in range(13)]
    records.append(_tool_use("Bash", command='brain search "rrf fusion" --limit 5'))
    records.append(_tool_use("Edit", file_path="/tmp/synthetic/mod_0.py"))
    return records


def _nudge_worthy(tmp_path: Path) -> Path:
    return _write_transcript(tmp_path / "session.jsonl", _substantive_records())


def _thresholds(**overrides: Any) -> HookThresholds:
    base: dict[str, Any] = {
        "enabled": True,
        "min_tool_calls": DEFAULT_MIN_TOOL_CALLS,
        "transcript_max_bytes": 8_388_608,
        "sentinel_ttl_days": 7,
        "vault_root": Path("/tmp/synthetic-vault"),
    }
    return HookThresholds(**{**base, **overrides})


# ---------------------------------------------------------------------------
# The thesis
# ---------------------------------------------------------------------------


def test_substantive_session_without_brain_write_is_nudged(tmp_path: Path) -> None:
    """The thesis: real work + nothing written back == exactly one capture nudge."""
    transcript = _nudge_worthy(tmp_path)

    decision = decide(_payload(transcript), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.block is True
    assert "brain capture" in decision.reason


# ---------------------------------------------------------------------------
# Ladder branches 1-3 — cheap gates before any transcript I/O
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'"a string"', b"null", b"\xff\xfe"])
def test_unparseable_stdin_allows(tmp_path: Path, raw: bytes) -> None:
    decision = decide(raw, env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.block is False
    assert decision.allow_because == "unparseable_payload"


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", "Off", "  no  "])
def test_disabled_by_env(tmp_path: Path, value: str) -> None:
    transcript = _nudge_worthy(tmp_path)

    decision = decide(
        _payload(transcript),
        env={"BRAIN_HOOK_ENABLED": value},
        run_root=tmp_path / "run",
        now=NOW,
    )

    assert decision.block is False
    assert decision.allow_because == "disabled"


@pytest.mark.parametrize("value", ["true", "1", "yes", "", "anything-else"])
def test_non_falsey_env_leaves_hook_enabled(tmp_path: Path, value: str) -> None:
    """Only the four documented spellings disable it; nothing else silently does."""
    transcript = _nudge_worthy(tmp_path)

    decision = decide(
        _payload(transcript),
        env={"BRAIN_HOOK_ENABLED": value},
        run_root=tmp_path / "run",
        now=NOW,
    )

    assert decision.block is True


def test_stop_hook_active_allows_immediately(tmp_path: Path) -> None:
    """The infinite-loop guard: it must precede the sentinel, and write nothing."""
    transcript = _nudge_worthy(tmp_path)
    run_root = tmp_path / "run"

    decision = decide(
        _payload(transcript, active=True), env={}, run_root=run_root, now=NOW
    )

    assert decision.block is False
    assert decision.allow_because == "stop_hook_active"
    assert not (run_root / SENTINEL_DIRNAME).exists()


# ---------------------------------------------------------------------------
# Ladder branches 5-7 — transcript evidence
# ---------------------------------------------------------------------------


def test_missing_transcript_allows(tmp_path: Path) -> None:
    decision = decide(
        _payload(tmp_path / "gone.jsonl"), env={}, run_root=tmp_path / "run", now=NOW
    )

    assert decision.allow_because == "no_transcript"


def test_directory_transcript_allows(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-file"
    directory.mkdir()

    decision = decide(_payload(directory), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.allow_because == "no_transcript"


def test_absent_transcript_key_allows(tmp_path: Path) -> None:
    raw = json.dumps({"session_id": "s-1", "stop_hook_active": False}).encode()

    decision = decide(raw, env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.allow_because == "no_transcript"


def test_readonly_session_below_threshold_is_silent(tmp_path: Path) -> None:
    records = [_tool_use("Read", file_path=f"/tmp/synthetic/f{i}.py") for i in range(5)]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(_payload(transcript), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.block is False
    assert decision.allow_because == "not_substantive"


def test_threshold_override(tmp_path: Path) -> None:
    """Proves the knob is wired — the same 5-call transcript now blocks."""
    records = [_tool_use("Read", file_path=f"/tmp/synthetic/f{i}.py") for i in range(5)]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(
        _payload(transcript),
        env={"BRAIN_HOOK_MIN_TOOL_CALLS": "3"},
        run_root=tmp_path / "run",
        now=NOW,
    )

    assert decision.block is True


@pytest.mark.parametrize("bad", ["banana", "-4", "0", "", "   ", "3.5"])
def test_bad_threshold_value_falls_back_to_default(tmp_path: Path, bad: str) -> None:
    """A typo in a hook knob must degrade to the default, never raise."""
    records = [_tool_use("Read", file_path=f"/tmp/synthetic/f{i}.py") for i in range(5)]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(
        _payload(transcript),
        env={"BRAIN_HOOK_MIN_TOOL_CALLS": bad},
        run_root=tmp_path / "run",
        now=NOW,
    )

    # Default 12 still applies, so 5 calls stay below the floor.
    assert decision.allow_because == "not_substantive"


def test_single_edit_is_substantive_without_call_count(tmp_path: Path) -> None:
    """A file mutation bypasses the tool-call floor entirely."""
    records = [
        _tool_use("Read", file_path="/tmp/synthetic/a.py"),
        _tool_use("Write", file_path="/tmp/synthetic/b.py"),
    ]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(_payload(transcript), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.block is True


# ---------------------------------------------------------------------------
# Branch 6 — every write surface that should count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__brain__brain_capture",
        "mcp__second-brain__brain_capture",
        "mcp__work__brain_note_new",
        "mcp__brain__brain_ingest_stdin",
        "mcp__brain__brain_daily",
        "mcp__brain__brain_tag",
        "brain_edit",
        "brain_capture",
    ],
)
def test_mcp_namespaced_brain_write_counts_as_write(tmp_path: Path, tool_name: str) -> None:
    records = [*_substantive_records(), _tool_use(tool_name, text="synthetic claim")]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(_payload(transcript), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.block is False
    assert decision.allow_because == "already_wrote"


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__brain__brain_search",
        "mcp__brain__brain_show",
        "mcp__brain__brain_status",
        "mcp__brain__brain_graphrag_search",
        "mcp__brain__brain_ask",
        "brain_backlinks",
    ],
)
def test_mcp_read_tools_do_not_count_as_write(tmp_path: Path, tool_name: str) -> None:
    """Read tools count toward the floor but never set ``wrote_to_brain``."""
    assert is_brain_write_tool(tool_name) is False

    records = [*_substantive_records(), _tool_use(tool_name, query="synthetic")]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(_payload(transcript), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.block is True


@pytest.mark.parametrize(
    "command",
    [
        'brain capture --text "synthetic"',
        "echo hi | brain capture",
        "brain ingest-stdin --source slack",
        'brain note new "Synthetic Title"',
        "brain daily",
        "brain tag abc123 +synthetic",
        "brain edit abc123",
        "brain mark-draft abc123",
        "brain mark-published abc123",
        "brain ingest-gmail --label synthetic",
        "brain ingest-dir /tmp/synthetic",
        "cd /tmp && brain capture --text x",
    ],
)
def test_bash_brain_write_verbs_count_as_write(tmp_path: Path, command: str) -> None:
    assert bash_command_writes_to_brain(command) is True

    records = [*_substantive_records(), _tool_use("Bash", command=command)]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(_payload(transcript), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.allow_because == "already_wrote"


@pytest.mark.parametrize(
    "command",
    [
        'brain search "synthetic"',
        "brain show abc123",
        "brain status",
        'brain graphrag search "synthetic"',
        "brain notes",
        "brain tag-something abc123",
        'mybrain capture --text "x"',
        "./other/brain-ish capture",
        "./brain capture",
        "/usr/local/bin/brain capture",
    ],
)
def test_bash_read_verbs_do_not_count_as_write(tmp_path: Path, command: str) -> None:
    """Reads, near-miss verbs, and sibling-repo binaries must all still nudge."""
    assert bash_command_writes_to_brain(command) is False

    records = [*_substantive_records(), _tool_use("Bash", command=command)]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(_payload(transcript), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.block is True


def test_vault_file_edit_counts_as_write(tmp_path: Path) -> None:
    """Editing a vault note IS writing to the brain."""
    vault = tmp_path / "synthetic-vault"
    records = [
        *_substantive_records(),
        _tool_use("Write", file_path=str(vault / "capture" / "synthetic-note.md")),
    ]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(
        _payload(transcript),
        env={"BRAIN_VAULT_PATH": str(vault)},
        run_root=tmp_path / "run",
        now=NOW,
    )

    assert decision.allow_because == "already_wrote"


def test_edit_outside_vault_still_nudges(tmp_path: Path) -> None:
    vault = tmp_path / "synthetic-vault"
    records = [
        *_substantive_records(),
        _tool_use("Write", file_path=str(tmp_path / "elsewhere" / "notes.md")),
    ]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(
        _payload(transcript),
        env={"BRAIN_VAULT_PATH": str(vault)},
        run_root=tmp_path / "run",
        now=NOW,
    )

    assert decision.block is True


# ---------------------------------------------------------------------------
# Sentinel — once per session
# ---------------------------------------------------------------------------


def test_sentinel_makes_nudge_once_per_session(tmp_path: Path) -> None:
    transcript = _nudge_worthy(tmp_path)
    run_root = tmp_path / "run"

    first = decide(_payload(transcript), env={}, run_root=run_root, now=NOW)
    second = decide(_payload(transcript), env={}, run_root=run_root, now=NOW)

    assert first.block is True
    assert second.block is False
    assert second.allow_because == "already_nudged"


def test_distinct_sessions_each_get_one_nudge(tmp_path: Path) -> None:
    transcript = _nudge_worthy(tmp_path)
    run_root = tmp_path / "run"

    first = decide(_payload(transcript, session_id="s-a"), env={}, run_root=run_root, now=NOW)
    second = decide(_payload(transcript, session_id="s-b"), env={}, run_root=run_root, now=NOW)

    assert first.block is True
    assert second.block is True


def test_sentinel_path_sanitizes_traversal(tmp_path: Path) -> None:
    path = sentinel_path("../../etc/passwd", run_root=tmp_path)

    assert path is not None
    assert path.parent == tmp_path / SENTINEL_DIRNAME
    assert ".." not in path.parts


@pytest.mark.parametrize("hostile", ["///", "", "\\/\\/", "@@@", "  "])
def test_unusable_session_id_yields_no_sentinel(tmp_path: Path, hostile: str) -> None:
    """An id that sanitizes to empty gets no sentinel at all.

    Writing to a shared guessable path would silence every session that
    produced an unusable id, so the gate is skipped instead.
    """
    assert sentinel_path(hostile, run_root=tmp_path) is None


@pytest.mark.parametrize("hostile", ["/../..", "../../etc/passwd", "a/../../b"])
def test_traversal_ids_sanitize_to_a_safe_filename(tmp_path: Path, hostile: str) -> None:
    """Separators are stripped, so what survives is one filename, not a path."""
    path = sentinel_path(hostile, run_root=tmp_path)

    assert path is not None
    assert path.parent == tmp_path / SENTINEL_DIRNAME
    assert "/" not in path.name
    assert path.resolve().parent == (tmp_path / SENTINEL_DIRNAME).resolve()


def test_sentinel_path_truncates_long_ids(tmp_path: Path) -> None:
    path = sentinel_path("a" * 500, run_root=tmp_path)

    assert path is not None
    assert path.stem == "a" * 64


def test_traversal_session_id_still_writes_inside_run_root(tmp_path: Path) -> None:
    """End-to-end: a hostile id nudges, but its sentinel lands in run_root."""
    transcript = _nudge_worthy(tmp_path)
    run_root = tmp_path / "run"

    decision = decide(
        _payload(transcript, session_id="../../.ssh/authorized_keys"),
        env={},
        run_root=run_root,
        now=NOW,
    )

    assert decision.block is True
    written = list((run_root / SENTINEL_DIRNAME).iterdir())
    assert len(written) == 1
    assert written[0].parent == run_root / SENTINEL_DIRNAME


def test_concurrent_claim_only_one_wins(tmp_path: Path) -> None:
    transcript = _nudge_worthy(tmp_path)
    run_root = tmp_path / "run"
    claimed = sentinel_path("s-1", run_root=run_root)
    assert claimed is not None

    assert claim_sentinel(claimed) is True
    assert claim_sentinel(claimed) is False

    decision = decide(_payload(transcript), env={}, run_root=run_root, now=NOW)
    # The pre-existing sentinel is caught by branch 4 before the transcript read.
    assert decision.allow_because == "already_nudged"


def test_sentinel_race_allows(tmp_path: Path) -> None:
    """Branch 8b: the sentinel appears between the exists() check and the claim.

    Simulated by making the sentinel directory a *file*, so ``mkdir`` raises
    ``OSError`` and the claim fails while ``exists()`` reported False.
    """
    transcript = _nudge_worthy(tmp_path)
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / SENTINEL_DIRNAME).write_text("not a directory", encoding="utf-8")

    decision = decide(_payload(transcript), env={}, run_root=run_root, now=NOW)

    assert decision.block is False
    assert decision.allow_because == "sentinel_race"


def test_claim_sentinel_on_none_is_false() -> None:
    assert claim_sentinel(None) is False


def test_missing_session_id_still_nudges_without_sentinel(tmp_path: Path) -> None:
    """An unusual harness with no session id: nudge once, write no sentinel."""
    transcript = _nudge_worthy(tmp_path)
    run_root = tmp_path / "run"

    decision = decide(
        _payload(transcript, session_id=None), env={}, run_root=run_root, now=NOW
    )

    assert decision.block is True
    assert not (run_root / SENTINEL_DIRNAME).exists()


def test_prune_removes_stale_sentinels_only(tmp_path: Path) -> None:
    directory = tmp_path / SENTINEL_DIRNAME
    directory.mkdir()
    fresh = directory / "fresh.nudged"
    stale = directory / "stale.nudged"
    fresh.touch()
    stale.touch()
    os.utime(fresh, (NOW.timestamp() - DAY_SECONDS, NOW.timestamp() - DAY_SECONDS))
    os.utime(stale, (NOW.timestamp() - 30 * DAY_SECONDS, NOW.timestamp() - 30 * DAY_SECONDS))

    removed = prune_sentinels(tmp_path, ttl_days=7, now=NOW)

    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_prune_on_missing_directory_is_zero(tmp_path: Path) -> None:
    assert prune_sentinels(tmp_path / "absent", ttl_days=7, now=NOW) == 0


def test_prune_survives_an_unremovable_entry(tmp_path: Path) -> None:
    """Housekeeping must never be the reason a Stop hook raises."""
    directory = tmp_path / SENTINEL_DIRNAME
    directory.mkdir()
    # A subdirectory cannot be `unlink`ed — the per-entry OSError guard.
    stale_dir = directory / "stale-dir.nudged"
    stale_dir.mkdir()
    stale_file = directory / "stale-file.nudged"
    stale_file.touch()
    old = NOW.timestamp() - 30 * DAY_SECONDS
    os.utime(stale_dir, (old, old))
    os.utime(stale_file, (old, old))

    removed = prune_sentinels(tmp_path, ttl_days=7, now=NOW)

    assert removed == 1
    assert stale_dir.exists()
    assert not stale_file.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_claim_sentinel_on_an_unwritable_directory_is_false(tmp_path: Path) -> None:
    """A hook that cannot record its own state must not nudge forever."""
    directory = tmp_path / SENTINEL_DIRNAME
    directory.mkdir()
    directory.chmod(0o500)
    try:
        assert claim_sentinel(directory / "s.nudged") is False
    finally:
        directory.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_transcript_allows(tmp_path: Path) -> None:
    """Permission-denied between the is_file() check and the open."""
    transcript = _nudge_worthy(tmp_path)
    transcript.chmod(0o000)
    try:
        decision = decide(
            _payload(transcript), env={}, run_root=tmp_path / "run", now=NOW
        )
    finally:
        transcript.chmod(0o600)

    assert decision.allow_because == "no_transcript"


def test_unparseable_file_path_is_not_a_vault_write(tmp_path: Path) -> None:
    """A NUL-bearing path raises inside Path handling; it must not escape."""
    vault = tmp_path / "synthetic-vault"
    records = [*_substantive_records(), _tool_use("Write", file_path="/tmp/bad\x00path.md")]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(
        _payload(transcript),
        env={"BRAIN_VAULT_PATH": str(vault)},
        run_root=tmp_path / "run",
        now=NOW,
    )

    assert decision.block is True


# ---------------------------------------------------------------------------
# Transcript robustness
# ---------------------------------------------------------------------------


def test_truncated_jsonl_line_is_skipped(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    body = "".join(f"{json.dumps(record)}\n" for record in _substantive_records())
    transcript.write_text(f'{body}{{"type": "assistant", "mess', encoding="utf-8")

    decision = decide(_payload(transcript), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.block is True


def test_malformed_records_are_ignored(tmp_path: Path) -> None:
    """Records with the wrong shape must not raise, and must not be counted."""
    weird: list[Any] = [
        {"type": "assistant"},
        {"type": "assistant", "message": "a string"},
        {"type": "assistant", "message": {"content": "not a list"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": 7}]}},
        ["not", "a", "mapping"],
        "a bare string record",
    ]
    transcript = _write_transcript(tmp_path / "session.jsonl", weird)

    stats = scan_transcript(transcript, thresholds=_thresholds())

    assert stats.tool_calls == 0
    assert stats.mutated_files is False
    assert stats.wrote_to_brain is False


def test_tool_use_with_non_mapping_input_is_counted_safely(tmp_path: Path) -> None:
    record = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": "oops"}]},
    }
    transcript = _write_transcript(tmp_path / "session.jsonl", [record])

    stats = scan_transcript(transcript, thresholds=_thresholds())

    assert stats.tool_calls == 1
    assert stats.wrote_to_brain is False


def test_empty_transcript_is_not_substantive(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")

    decision = decide(_payload(transcript), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.allow_because == "not_substantive"


def test_oversized_transcript_reads_tail_only(tmp_path: Path) -> None:
    """A tiny window forces the seek path; the tail records still decide."""
    transcript = tmp_path / "session.jsonl"
    padding = "".join(
        json.dumps(_tool_use("Read", file_path=f"/tmp/synthetic/pad_{i}.py")) + "\n"
        for i in range(400)
    )
    tail = "".join(json.dumps(record) + "\n" for record in _substantive_records())
    transcript.write_text(padding + tail, encoding="utf-8")

    decision = decide(
        _payload(transcript),
        env={"BRAIN_HOOK_TRANSCRIPT_MAX_BYTES": str(len(tail.encode()) + 40)},
        run_root=tmp_path / "run",
        now=NOW,
    )

    assert decision.block is True


def test_multiple_tool_use_blocks_in_one_record_all_count(tmp_path: Path) -> None:
    record = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/a"}},
                {"type": "text", "text": "thinking"},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/b"}},
            ]
        },
    }
    transcript = _write_transcript(tmp_path / "session.jsonl", [record])

    stats = scan_transcript(transcript, thresholds=_thresholds())

    assert stats.tool_calls == 2


def test_sidechain_records_count_the_same(tmp_path: Path) -> None:
    """A teammate's work is still this session's work."""
    records = [{**record, "isSidechain": True} for record in _substantive_records()]
    transcript = _write_transcript(tmp_path / "session.jsonl", records)

    decision = decide(_payload(transcript), env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.block is True


# ---------------------------------------------------------------------------
# Prompt-injection regression — the assertion that must never be weakened
# ---------------------------------------------------------------------------


def test_reason_never_contains_transcript_text(tmp_path: Path) -> None:
    """No transcript-derived text may reach `reason`; it is a fixed constant."""
    marker = "INJECTED-PAYLOAD-DO-NOT-ECHO"
    records = [
        *_substantive_records(),
        _tool_use("Bash", command=f"echo {marker}"),
        _tool_use("Edit", file_path=f"/tmp/synthetic/{marker}.py"),
        _tool_use(marker, note=marker),
    ]
    transcript = _write_transcript(tmp_path / f"{marker}.jsonl", records)

    decision = decide(
        _payload(transcript, session_id=marker),
        env={},
        run_root=tmp_path / "run",
        now=NOW,
    )

    assert decision.block is True
    assert marker not in decision.reason
    assert marker not in decision.allow_because
    assert decision.reason == NUDGE_REASON


def test_allow_decisions_carry_no_reason_text(tmp_path: Path) -> None:
    decision = decide(b"not json", env={}, run_root=tmp_path / "run", now=NOW)

    assert decision.reason == ""


# ---------------------------------------------------------------------------
# Helper-level units
# ---------------------------------------------------------------------------


def test_parse_payload_defaults() -> None:
    payload = parse_payload(b"{}")

    assert payload is not None
    assert payload.session_id is None
    assert payload.transcript_path is None
    assert payload.stop_hook_active is False


def test_parse_payload_rejects_non_string_fields() -> None:
    payload = parse_payload(
        json.dumps({"session_id": 42, "transcript_path": ["a"], "stop_hook_active": 1}).encode()
    )

    assert payload is not None
    assert payload.session_id is None
    assert payload.transcript_path is None
    assert payload.stop_hook_active is True


def test_load_thresholds_defaults() -> None:
    thresholds = load_thresholds({})

    assert thresholds.enabled is True
    assert thresholds.min_tool_calls == DEFAULT_MIN_TOOL_CALLS
    assert thresholds.transcript_max_bytes == 8_388_608
    assert thresholds.sentinel_ttl_days == 7


def test_load_thresholds_reads_injected_env_not_process_env() -> None:
    """The mapping is the only source — that is what keeps the hook DB-free."""
    thresholds = load_thresholds(
        {
            "BRAIN_HOOK_MIN_TOOL_CALLS": "3",
            "BRAIN_HOOK_SENTINEL_TTL_DAYS": "30",
            "BRAIN_VAULT_PATH": "/tmp/synthetic-vault",
        }
    )

    assert thresholds.min_tool_calls == 3
    assert thresholds.sentinel_ttl_days == 30
    assert thresholds.vault_root == Path("/tmp/synthetic-vault")
