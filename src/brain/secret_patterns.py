"""Canonical secret/credential regex registry shared by ingest and the git hook."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Why this module exists, and why it lives at ``src/brain/`` rather than under
# ``ingest/``.
#
# Secret detection predates this module: ``scripts/hooks/pre-commit`` has
# scanned staged diffs since the repo was PII-scrubbed. F4 adds a SECOND
# consumer -- the ingest-time guard (:mod:`brain.ingest.guard`) -- and CLAUDE.md
# is explicit that anything used in 2+ places gets extracted. So the patterns
# live here, in the package root, and both consumers read them from one place.
#
# The hook cannot ``import brain``: it must run offline, with no venv activated,
# from a ``git commit`` in any shell. So it keeps its own copy of the
# alternation, and :func:`egrep_alternation` reproduces that copy
# character-for-character. ``tests/test_secret_pattern_hook_parity.py`` asserts
# byte-equality against the marker-delimited block in the hook, which turns red
# the moment a pattern is added here without regenerating the hook line
# (``python -m brain.secret_patterns --emit-egrep``). One source of truth, no
# runtime dependency, no silent drift.
#
# CASE SENSITIVITY -- a deliberate, documented asymmetry with the hook. The
# hook greps with ``-i`` (one flag covering its whole deterministic stage,
# including the email heuristic). Python-side scanning is case-SENSITIVE:
# real credentials are case-exact, while a personal knowledge base is mostly
# prose, and ``-i`` would let e.g. a lowercase 16-char run after "akia" flag a
# sentence. The shared artifact is the pattern STRING; the matching flags are
# each consumer's own call.
#
# DELIBERATELY EXCLUDED: the hook's real-email heuristic
# (``scripts/hooks/pre-commit``) stays bash-only. See the note in
# :mod:`brain.ingest.guard` -- this corpus is largely Gmail and Krisp, so email
# addresses are the CONTENT, not a leak.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretPattern:
    """One credential shape: how to find it, what to call it, how to preview it."""

    kind: str
    """Stable machine identifier, e.g. ``"aws_access_key_id"``. Used as the
    redaction marker (``[REDACTED:<kind>]``) and as a JSON key, so it is part
    of the public contract -- renaming one is a breaking change."""

    regex: str
    """Raw pattern source, copied VERBATIM from the hook's alternation. POSIX-ERE
    and Python-``re`` compatible without translation (no ``\\d``, no lookaround,
    no lazy quantifiers), which is what lets one string serve both consumers."""

    label: str
    """Human-readable name for the CLI finding table."""

    preview_head: int
    """Leading characters of a match that are SAFE to echo verbatim.

    Contract: these characters must be pattern-fixed prefix, never entropy from
    the credential body. Every value below is chosen against the specific regex
    -- see :data:`SECRET_PATTERNS` for the one case where that forces 3 rather
    than the usual 4.
    """


# The twelve rows, in the hook's own order. Each ``regex`` is a byte-for-byte
# copy of its alternative in ``scripts/hooks/pre-commit``; reordering or
# reformatting them breaks the parity test on purpose.
SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        kind="aws_access_key_id",
        regex="AKIA[0-9A-Z]{16}",
        label="AWS access key ID",
        preview_head=4,
    ),
    SecretPattern(
        kind="aws_temp_access_key_id",
        regex="ASIA[0-9A-Z]{16}",
        label="AWS temporary access key ID",
        preview_head=4,
    ),
    SecretPattern(
        kind="private_key_header",
        regex="-----BEGIN [A-Z ]*PRIVATE KEY-----",
        label="PEM private key header",
        # 0, per spec: the match is the banner itself, and echoing any of it
        # invites echoing "just a bit more" later. The ``label`` column already
        # tells the user exactly what was found.
        preview_head=0,
    ),
    SecretPattern(
        kind="slack_token",
        regex="xox[baprs]-[0-9A-Za-z-]{10,}",
        label="Slack token",
        # "xox" + one of [baprs] -- the token TYPE, not secret bytes.
        preview_head=4,
    ),
    SecretPattern(
        kind="openai_key",
        regex="sk-[A-Za-z0-9]{20,}",
        label="OpenAI API key",
        # 3, NOT the usual 4. This is the one pattern whose fixed prefix is only
        # three characters ("sk-"), so a head of 4 would echo the first byte of
        # the secret itself. ``preview_head``'s contract is "safe prefix", and
        # one leaked byte is still a leaked byte, so the value follows the
        # contract rather than the convention.
        preview_head=3,
    ),
    SecretPattern(
        kind="openai_project_key",
        regex="sk-proj-[A-Za-z0-9_-]{20,}",
        label="OpenAI project key",
        preview_head=4,
    ),
    SecretPattern(
        kind="stripe_secret_key_live",
        regex="sk_live_[A-Za-z0-9]{20,}",
        label="Stripe live secret key",
        preview_head=4,
    ),
    SecretPattern(
        kind="stripe_restricted_key_live",
        regex="rk_live_[A-Za-z0-9]{20,}",
        label="Stripe live restricted key",
        preview_head=4,
    ),
    SecretPattern(
        kind="github_pat",
        regex="ghp_[A-Za-z0-9]{36}",
        label="GitHub personal access token",
        preview_head=4,
    ),
    SecretPattern(
        kind="github_pat_fine_grained",
        regex="github_pat_[A-Za-z0-9_]{20,}",
        label="GitHub fine-grained token",
        preview_head=4,
    ),
    SecretPattern(
        kind="gitlab_pat",
        regex="glpat-[A-Za-z0-9_-]{18,}",
        label="GitLab personal access token",
        preview_head=4,
    ),
    SecretPattern(
        kind="google_api_key",
        regex="AIza[0-9A-Za-z_-]{35}",
        label="Google API key",
        preview_head=4,
    ),
)

# Compiled once at import. ``scan_secrets`` runs every pattern against every
# line of every ingested document, so re-compiling per call would be the
# pipeline's dominant cost on a large corpus pass.
_COMPILED: tuple[tuple[SecretPattern, re.Pattern[str]], ...] = tuple(
    (p, re.compile(p.regex)) for p in SECRET_PATTERNS
)


def compiled_patterns() -> tuple[tuple[SecretPattern, re.Pattern[str]], ...]:
    """Return each pattern paired with its compiled regex, in registry order.

    The tuple is built once at import and shared; callers must treat both it and
    the ``SecretPattern`` instances (frozen dataclasses) as read-only.
    """
    return _COMPILED


def egrep_alternation() -> str:
    """Return the POSIX-ERE alternation the pre-commit hook greps with.

    Exactly ``"(" + "|".join(regexes) + ")"`` -- no flags, no anchors, no
    escaping. This string is the shared artifact between this module and
    ``scripts/hooks/pre-commit``; the parity test compares it byte-for-byte
    against the hook's marker-delimited literal.
    """
    return "(" + "|".join(p.regex for p in SECRET_PATTERNS) + ")"


def _emit_egrep_line() -> str:
    """Return the exact ``_SECRET_RE=...`` line to paste into the hook."""
    return f"_SECRET_RE='{egrep_alternation()}'"


def main(argv: list[str] | None = None) -> int:
    """Regenerate the hook's pattern line: ``python -m brain.secret_patterns --emit-egrep``.

    Deliberately a hand-rolled two-branch dispatch rather than ``argparse`` --
    this is a one-flag developer utility invoked when the parity test goes red,
    not a user-facing CLI, and it must stay importable with no side effects.
    """
    args = sys.argv[1:] if argv is None else argv
    if args == ["--emit-egrep"]:
        print(_emit_egrep_line())
        return 0
    print(
        "usage: python -m brain.secret_patterns --emit-egrep\n"
        "  Prints the generated _SECRET_RE line for scripts/hooks/pre-commit.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised via main() in tests
    raise SystemExit(main())
