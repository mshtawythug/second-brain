"""Ingest-time secret detection and redaction (F4)."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, get_args

from brain.errors import SecretGuardError
from brain.secret_patterns import compiled_patterns

# ---------------------------------------------------------------------------
# Scope, and one deliberate omission.
#
# This module is pure logic: no DB, no filesystem, no network, no logging. It
# takes a string and returns findings or a new string. That is what lets the
# guard sit on the hottest path in the pipeline (every ingested document, before
# hashing) without owning any failure mode of its own.
#
# DELIBERATELY NOT SCANNED: email addresses. The pre-commit hook flags them
# because it polices SOURCE CODE, where a real address is a leak. This corpus is
# largely Gmail threads and Krisp transcripts -- addresses are the content.
# Enabling the heuristic here would produce a finding on essentially every
# document and train the user to ignore the guard entirely, which would cost
# more than it buys. The email heuristic stays bash-only.
#
# The guard is a GUARD RAIL, not a security control. It matches twelve
# well-known credential shapes and nothing else: a base64'd token, a password in
# prose, or an internal token format all pass straight through.
# ---------------------------------------------------------------------------

GuardMode = Literal["warn", "redact", "reject", "off"]
"""The four values ``BRAIN_SECRET_GUARD`` accepts.

Kept in lockstep with ``brain.config._VALID_SECRET_GUARDS`` by
``tests/test_ingest_guard.py::test_guard_modes_match_the_config_enum`` -- the
config module owns env parsing, this module owns behaviour, and the two sets
drifting apart would let a config-valid mode reach an ``apply_guard`` that
rejects it.
"""

VALID_GUARD_MODES: frozenset[str] = frozenset(get_args(GuardMode))

# Maximum mask characters appended after ``preview_head``. With the largest
# ``preview_head`` of 4 this caps a preview at 24 characters, so the number of
# asterisks cannot be used to recover the credential's exact length.
_MAX_MASK_CHARS = 20

_REDACTION_TEMPLATE = "[REDACTED:{kind}]"


@dataclass(frozen=True)
class SecretFinding:
    """One credential-shaped match, located and safely summarized.

    ``line`` / ``col_start`` / ``col_end`` are 1-indexed and inclusive, matching
    what an editor's "go to line:column" expects. ``preview`` is masked and is
    the ONLY representation of the match that ever leaves this module.
    """

    kind: str
    label: str
    line: int
    col_start: int
    col_end: int
    preview: str


@dataclass(frozen=True)
class GuardOutcome:
    """What the guard decided for one document.

    ``content`` is what the caller must store -- identical to the input under
    every mode except ``redact``. ``redacted`` says whether it was rewritten, so
    a caller never has to compare strings to find out.
    """

    content: str
    findings: tuple[SecretFinding, ...]
    redacted: bool


def _preview(matched: str, preview_head: int) -> str:
    """Build the masked preview for ``matched``.

    The rule is fixed and deliberately dumb: keep ``preview_head`` leading
    characters (pattern-fixed prefix only -- see
    :attr:`brain.secret_patterns.SecretPattern.preview_head`), then append one
    asterisk per remaining character up to :data:`_MAX_MASK_CHARS`. Nothing is
    ever derived from the tail of the match, which is where a credential's
    entropy lives.
    """
    kept = matched[:preview_head]
    masked = max(0, min(len(matched) - preview_head, _MAX_MASK_CHARS))
    return kept + "*" * masked


def scan_secrets(text: str) -> list[SecretFinding]:
    """Return every credential-shaped match in ``text``, deterministically ordered.

    Scanning is LINE-SCOPED -- ``text`` is split first, then each pattern runs
    against each line. Two reasons: ``line``/``col`` fall out for free and are
    trivially correct, and a pathological single-line blob (a 12 MB PDF text
    extraction with no newlines) still bounds each regex to that one line. All
    twelve patterns are anchored-prefix plus a bounded or simple-greedy
    character class with no nested quantifiers, so matching is linear -- no
    catastrophic backtracking is reachable.

    Ordering is ``(line, col_start, kind)``. Callers -- and the redactor below --
    depend on it being stable across runs.
    """
    findings: list[SecretFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, compiled in compiled_patterns():
            for match in compiled.finditer(line):
                findings.append(
                    SecretFinding(
                        kind=pattern.kind,
                        label=pattern.label,
                        line=lineno,
                        col_start=match.start() + 1,
                        col_end=match.end(),
                        preview=_preview(match.group(0), pattern.preview_head),
                    )
                )
    findings.sort(key=lambda f: (f.line, f.col_start, f.kind))
    return findings


def _redact_line(line: str, hits: Sequence[SecretFinding]) -> str:
    """Replace each hit's span in ``line`` with its redaction marker.

    Applied RIGHT-TO-LEFT so every column index stays valid as the string is
    rebuilt -- replacing left-to-right would shift the offsets of every span
    still to be handled, since the marker's length differs from the match's.

    Overlapping spans are dropped rather than double-substituted. The twelve
    current patterns cannot overlap in practice (each has a distinct literal
    prefix), but a future pattern could, and a double substitution would splice
    a marker into the middle of another marker and corrupt the body. Skipping is
    lossless here: the surviving span is the rightmost, and the leftmost span it
    overlapped is inside the region the marker already covers.
    """
    kept: list[SecretFinding] = []
    boundary = len(line) + 1
    for finding in sorted(hits, key=lambda f: (f.col_start, f.kind), reverse=True):
        if finding.col_end < boundary:
            kept.append(finding)
            boundary = finding.col_start
    # ``kept`` is already right-to-left, so each splice leaves earlier spans put.
    for finding in kept:
        line = (
            line[: finding.col_start - 1]
            + _REDACTION_TEMPLATE.format(kind=finding.kind)
            + line[finding.col_end :]
        )
    return line


def redact_secrets(text: str) -> tuple[str, list[SecretFinding]]:
    """Return ``(redacted_text, findings)``; ``text`` itself is never mutated.

    Splitting with ``keepends=True`` preserves the exact line terminators, so a
    document with CRLF endings or no trailing newline round-trips byte-for-byte
    apart from the replaced spans. Column indices agree with
    :func:`scan_secrets` because no pattern can match a line terminator.

    Idempotent: ``redact_secrets(redact_secrets(x)[0])[0] == redact_secrets(x)[0]``.
    The marker ``[REDACTED:<kind>]`` is shaped so it cannot itself match any
    pattern -- asserted for every kind in the test suite, because that is a
    property of the *kind names* and would quietly break if one were renamed.
    """
    findings = scan_secrets(text)
    if not findings:
        return text, findings

    hits_by_line: dict[int, list[SecretFinding]] = {}
    for finding in findings:
        hits_by_line.setdefault(finding.line, []).append(finding)

    rebuilt = [
        _redact_line(line, hits_by_line[lineno]) if lineno in hits_by_line else line
        for lineno, line in enumerate(text.splitlines(keepends=True), start=1)
    ]
    return "".join(rebuilt), findings


def format_findings(
    findings: Sequence[SecretFinding], *, title: str, mode: str
) -> str:
    """Render findings as the multi-line block the CLI prints to stderr.

    ``mode`` is a DISPLAY mode: the four :data:`GuardMode` values plus
    ``"allow"``, which reports a guard bypassed for this invocation
    (``--allow-secrets`` or the note's ``allow_secrets: true`` frontmatter).
    ``"allow"`` is not a config value and is never accepted by
    :func:`apply_guard` -- only the caller that knows a bypass applied passes it
    here, which is how the frontmatter opt-out (resolved inside the pipeline,
    invisible to the CLI) still gets an accurate message.

    Returns ``""`` for no findings so callers can print unconditionally.
    """
    if not findings:
        return ""

    count = f"{len(findings)} finding(s)"
    if mode == "reject":
        header = (
            f'✗  secret guard: refusing to ingest "{title}" — {count} '
            f"(BRAIN_SECRET_GUARD=reject)"
        )
        footer = (
            "   Fix the source, or pass --allow-secrets, or add "
            "`allow_secrets: true` to the note's frontmatter."
        )
    elif mode == "redact":
        header = (
            f'⚠  secret guard: {count} in "{title}" — REDACTED before storage '
            f"(BRAIN_SECRET_GUARD=redact)"
        )
        footer = (
            "   The stored body no longer contains them; the source file is "
            "untouched."
        )
    elif mode == "allow":
        header = (
            f'⚠  secret guard: {count} in "{title}" — stored UNCHANGED '
            f"(guard bypassed for this document)"
        )
        footer = (
            "   Findings are still reported; --allow-secrets and "
            "`allow_secrets: true` suppress the action, not the evidence."
        )
    else:
        header = (
            f'⚠  secret guard: {count} in "{title}" — stored UNCHANGED '
            f"(BRAIN_SECRET_GUARD={mode})"
        )
        footer = (
            "   Re-run with BRAIN_SECRET_GUARD=redact to strip them, or "
            "=reject to refuse."
        )

    rows = [
        f"   line {f.line}, col {f.col_start}-{f.col_end}   {f.kind:<26} {f.preview}"
        for f in findings
    ]
    return "\n".join([header, *rows, footer])


def apply_guard(
    content: str, *, mode: str, allow: bool, title: str
) -> GuardOutcome:
    """Scan ``content`` and apply ``mode``, returning what the caller must store.

    Mode semantics:

    - ``off``   -- no scan at all; returns the input with no findings.
    - ``warn``  -- scan, report, store UNCHANGED. The default, because it is
      lossless and reversible: these regexes do false-positive on legitimate
      prose, and silently mutating (``redact``) or aborting a 900-file walk
      (``reject``) are both worse failures than a loud message.
    - ``redact`` -- replace each match with ``[REDACTED:<kind>]``.
    - ``reject`` -- raise :class:`~brain.errors.SecretGuardError` carrying the
      formatted findings block.

    ``allow=True`` (``--allow-secrets``, or ``allow_secrets: true`` in a note's
    frontmatter) downgrades ``redact`` and ``reject`` to report-only. It never
    suppresses the findings themselves: an escape hatch that hides the evidence
    is a worse hatch than none.

    Raises :class:`ValueError` on an unknown ``mode``. Config validates
    ``BRAIN_SECRET_GUARD`` at load, so reaching this is a programming error in a
    library caller, not user input.
    """
    if mode not in VALID_GUARD_MODES:
        raise ValueError(
            f"secret guard mode must be one of "
            f"{'/'.join(sorted(VALID_GUARD_MODES))} (got {mode!r})"
        )

    if mode == "off":
        return GuardOutcome(content=content, findings=(), redacted=False)

    if allow:
        return GuardOutcome(
            content=content, findings=tuple(scan_secrets(content)), redacted=False
        )

    if mode == "redact":
        redacted_text, findings = redact_secrets(content)
        return GuardOutcome(
            content=redacted_text,
            findings=tuple(findings),
            redacted=bool(findings),
        )

    findings = scan_secrets(content)
    if mode == "reject" and findings:
        raise SecretGuardError(format_findings(findings, title=title, mode="reject"))
    return GuardOutcome(content=content, findings=tuple(findings), redacted=False)
