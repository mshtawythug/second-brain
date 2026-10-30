"""The G1 log-privacy guard's allowlists (``tests/log_privacy_rules.py``).

Each entry is a deliberate exception to a rule, with its written reason. G1
(``tests/test_log_privacy_rules.py``) fails when an entry matches no site in
``src/brain``, so a stale entry cannot linger. Moved out of the rules module for
the 800-line ceiling (RM-28).
"""

from __future__ import annotations

#: ``(module, enclosing function, message) -> reason``. These are the only
#: places a traceback may be logged. A traceback carries the exception message,
#: so each site is a deliberate trade of that risk for diagnosability.
TRACEBACK_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "vault/watch.py",
        "on_any_event",
        "vault watcher: classify failed for %r",
    ): "the daemon's only diagnosis of an unexpected observer-thread failure",
    (
        "vault/watch.py",
        "on_any_event",
        "vault watcher: schedule failed for %s %s",
    ): "the same, for scheduling",
    (
        "vault/watch.py",
        "_schedule_debounced",
        "vault watcher: debounce timer failed to start; enqueued directly",
    ): "a thread-start failure: OS-level, no document text",
    (
        "vault/watch.py",
        "_enqueue",
        "vault watcher: on_event hook raised",
    ): "a test-only hook; production passes None",
    (
        "vault/watch.py",
        "_worker_loop",
        "vault watcher: worker failed on %s %s",
    ): "the daemon's only diagnosis of an unexpected job failure",
    (
        "mcp_server.py",
        "_wrap_db_error",
        "database error",
    ): "server-log detail by design; the MCP wire gets the class only (DB-08)",
    ("mcp_server.py", "_wrap_embed_error", "embedding failed"): "the same design as _wrap_db_error",
    (
        "mcp_server.py",
        "_wrap_graph_backend_error",
        "graph backend error",
    ): "the same design; spec D9 keeps Cypher off the wire, not out of the server log",
    (
        "ui/app.py",
        "_unhandled_error_handler",
        "brain ui: unhandled error serving %s",
    ): "last-resort handler; the browser gets the class only",
}

#: ``(module, enclosing function, message) -> reason``. The only places a value
#: DERIVED from a caught exception may be logged raw, because the derivation is
#: content-free by construction.
RAW_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "gaps.py",
        "record_search_query",
        "search-query logging skipped: search_queries.%s column missing (migration %s not "
        "applied) — run `brain init` to restore full `brain gaps` signal",
    ): "`missing` is a key of _ADDITIVE_COLUMNS (_missing_additive_column returns one or None)",
}

#: ``(module, enclosing function, whole statement) -> reason``. The only places a
#: tainted value may be stored where a log line can reach it (T2). Keyed on the
#: whole statement, so a different value stored to the same target is flagged.
STORE_ALLOWLIST: dict[tuple[str, str, str], str] = {
    ("ui/jobs.py", "_fail", "job.error = error"): (
        "`error` is a JobError (the parameter heuristic matches its name), and it is "
        "class-only by construction: every mapper builds the message from a fixed string, "
        "the class name, an allow-listed extension or describe_ask_failure"
    ),
    ("graph_rag/chunk_split.py", "on_truncated", "tally.entries.extend(salvaged)"): (
        "the salvaged entities ARE the extraction's output: the complete items of the "
        "truncated reply (ChatOutputTruncated.partial), returned to the graph build like "
        "any other chunk's entities; no exception text is in them"
    ),
    ("vault/frontmatter.py", "_shifted_past_the_fence", "setattr(exc, attr, shifted)"): (
        "re-stores the exception's own mark on the same exception, one line down (F1); "
        "nothing leaves the exception, and its callers log it through describe_failure"
    ),
    ("vault/sync.py", "sync_vault", "report.errors.append((walked_file.abs_path, str(e)))"): (
        "_SyncError's message is authored at its raise sites, each from fixed text, "
        "describe_failure, a constraint name or a count (Y5, Y6 pin two of them)"
    ),
    ("vault/sync.py", "sync_one_file", "report.errors.append((walked.abs_path, str(e)))"): (
        "the same _SyncError message as sync_vault's entry, for the single-file path"
    ),
}
