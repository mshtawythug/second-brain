"""Pure-logic tests for the ingest secret guard (:mod:`brain.ingest.guard`)."""
from __future__ import annotations

import pytest

from brain.config import _VALID_SECRET_GUARDS
from brain.errors import SecretGuardError
from brain.ingest.guard import (
    VALID_GUARD_MODES,
    apply_guard,
    format_findings,
    redact_secrets,
    scan_secrets,
)
from brain.secret_patterns import SECRET_PATTERNS, SecretPattern
from tests.secret_fixtures import (
    CLEAN_PROSE,
    SYNTHETIC_AWS_KEY,
    SYNTHETIC_NEGATIVES,
    SYNTHETIC_POSITIVES,
)

_TITLE = "Deploy runbook"


# ---------------------------------------------------------------------------
# Config lockstep
# ---------------------------------------------------------------------------


def test_guard_modes_match_the_config_enum() -> None:
    """A mode config accepts but ``apply_guard`` rejects would be a startup trap."""
    assert VALID_GUARD_MODES == _VALID_SECRET_GUARDS


# ---------------------------------------------------------------------------
# scan_secrets — location, ordering, and the false-positive floor
# ---------------------------------------------------------------------------


def test_scan_reports_one_indexed_line_and_column() -> None:
    # --- setup: the key starts at column 6 of line 3 ("key: " is 5 chars).
    text = f"first line\n\nkey: {SYNTHETIC_AWS_KEY}\nlast line\n"

    # --- exercise
    findings = scan_secrets(text)

    # --- verify
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "aws_access_key_id"
    assert finding.line == 3
    assert finding.col_start == 6
    assert finding.col_end == 5 + len(SYNTHETIC_AWS_KEY)


def test_scan_is_deterministically_ordered_by_line_then_column() -> None:
    # --- setup: three findings deliberately out of registry order.
    text = (
        f"{SYNTHETIC_POSITIVES['github_pat']}\n"
        f"pad {SYNTHETIC_POSITIVES['aws_access_key_id']} "
        f"{SYNTHETIC_POSITIVES['gitlab_pat']}\n"
    )

    # --- exercise
    findings = scan_secrets(text)

    # --- verify
    assert [(f.line, f.kind) for f in findings] == [
        (1, "github_pat"),
        (2, "aws_access_key_id"),
        (2, "gitlab_pat"),
    ]
    assert scan_secrets(text) == findings


@pytest.mark.parametrize("kind", sorted(SYNTHETIC_POSITIVES), ids=str)
def test_scan_finds_every_pattern(kind: str) -> None:
    # --- exercise
    findings = scan_secrets(f"value = {SYNTHETIC_POSITIVES[kind]}\n")

    # --- verify
    assert [f.kind for f in findings] == [kind]


@pytest.mark.parametrize("pattern", SECRET_PATTERNS, ids=lambda p: p.kind)
def test_findings_carry_the_registry_label(pattern: SecretPattern) -> None:
    """``label`` is the human name the Wave-3 sweep table renders.

    Nothing prints it yet — ``format_findings`` shows ``kind``, per spec — so
    without this the field could drift from its pattern unnoticed until F6
    surfaces it.
    """
    finding = scan_secrets(SYNTHETIC_POSITIVES[pattern.kind])[0]
    assert finding.label == pattern.label
    assert finding.label, "every pattern needs a non-empty human label"


def test_clean_prose_produces_no_findings() -> None:
    """The false-positive floor: credential vocabulary without credential shapes."""
    assert scan_secrets(CLEAN_PROSE) == []


def test_near_miss_values_produce_no_findings() -> None:
    """Truncated look-alikes must not flag — this is what makes ``warn`` bearable."""
    text = "\n".join(SYNTHETIC_NEGATIVES.values())
    assert scan_secrets(text) == []


@pytest.mark.parametrize(
    "text", ["", "   ", "\n\n\t\n"], ids=["empty", "spaces", "blank-lines"]
)
def test_empty_and_whitespace_input_is_a_no_op(text: str) -> None:
    assert scan_secrets(text) == []


def test_a_single_long_line_is_scanned_without_blowing_up() -> None:
    """Line-scoped scanning is what bounds a newline-free PDF extraction."""
    # --- setup
    text = ("x" * 200_000) + " " + SYNTHETIC_AWS_KEY + " " + ("y" * 200_000)

    # --- exercise
    findings = scan_secrets(text)

    # --- verify
    assert [f.kind for f in findings] == ["aws_access_key_id"]
    assert findings[0].line == 1


# ---------------------------------------------------------------------------
# Preview safety — the one thing that must never leak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", SECRET_PATTERNS, ids=lambda p: p.kind)
def test_preview_never_contains_full_secret(pattern: SecretPattern) -> None:
    """For EVERY pattern: the matched string is absent from preview AND output.

    Both surfaces are checked because they leak to different places — the
    preview into JSON and programmatic consumers, the formatted block into
    terminal scrollback and CI logs.
    """
    # --- setup
    secret = SYNTHETIC_POSITIVES[pattern.kind]

    # --- exercise
    findings = scan_secrets(f"token = {secret}\n")
    rendered = format_findings(findings, title=_TITLE, mode="warn")

    # --- verify
    assert len(findings) == 1
    assert secret not in findings[0].preview
    assert secret not in rendered


@pytest.mark.parametrize("pattern", SECRET_PATTERNS, ids=lambda p: p.kind)
def test_preview_is_a_fixed_prefix_followed_only_by_asterisks(
    pattern: SecretPattern,
) -> None:
    # --- setup
    secret = SYNTHETIC_POSITIVES[pattern.kind]
    head = pattern.preview_head

    # --- exercise
    preview = scan_secrets(secret)[0].preview

    # --- verify
    assert preview[:head] == secret[:head]
    assert set(preview[head:]) <= {"*"}
    # Capped so the asterisk count cannot pin down the credential's length.
    assert len(preview) <= 24


def test_preview_length_saturates_so_long_secrets_are_indistinguishable() -> None:
    """Past the mask cap, two different-length credentials preview identically.

    The cap bounds length disclosure; it does not erase it. A credential shorter
    than ``preview_head + 20`` still yields one asterisk per character — which
    leaks nothing for the fixed-length patterns (``AKIA[0-9A-Z]{16}`` already
    pins the length at 20) and at most a few bits for the open-ended ones. What
    matters, and what is asserted here, is that arbitrarily long secrets all
    collapse to the same 24-character preview.
    """
    # --- setup: two open-ended OpenAI-project keys, 30 and 80 characters.
    shorter = "sk-proj-" + ("A" * 30)
    longer = "sk-proj-" + ("A" * 80)

    # --- exercise
    shorter_preview = scan_secrets(shorter)[0].preview
    longer_preview = scan_secrets(longer)[0].preview

    # --- verify
    assert shorter_preview == longer_preview
    assert len(shorter_preview) == 24


@pytest.mark.parametrize("kind", sorted(SYNTHETIC_POSITIVES), ids=str)
def test_no_preview_ever_exceeds_the_cap(kind: str) -> None:
    """The hard bound, checked against a deliberately oversized credential."""
    # --- setup: pad the canonical fixture's tail so every pattern saturates.
    oversized = SYNTHETIC_POSITIVES[kind] + ("A" * 100)

    # --- exercise
    findings = scan_secrets(oversized)

    # --- verify
    assert findings, f"padded {kind} fixture stopped matching"
    assert all(len(f.preview) <= 24 for f in findings)


# ---------------------------------------------------------------------------
# redact_secrets
# ---------------------------------------------------------------------------


def test_redact_replaces_the_span_with_a_kind_marker() -> None:
    # --- setup
    text = f"key: {SYNTHETIC_AWS_KEY}\n"

    # --- exercise
    redacted, findings = redact_secrets(text)

    # --- verify
    assert redacted == "key: [REDACTED:aws_access_key_id]\n"
    assert [f.kind for f in findings] == ["aws_access_key_id"]


def test_redact_does_not_mutate_its_input() -> None:
    # --- setup
    text = f"key: {SYNTHETIC_AWS_KEY}\n"
    before = str(text)

    # --- exercise
    redact_secrets(text)

    # --- verify
    assert text == before


@pytest.mark.parametrize("kind", sorted(SYNTHETIC_POSITIVES), ids=str)
def test_redaction_is_idempotent_for_every_kind(kind: str) -> None:
    """``redact(redact(x)) == redact(x)``.

    This is a property of the KIND NAMES: the marker must not itself match any
    pattern. ``[REDACTED:github_pat_fine_grained]`` is the near miss — its tail
    after ``github_pat_`` is 12 characters where the regex needs 20 — so a
    rename could quietly break this.
    """
    # --- setup
    text = f"value = {SYNTHETIC_POSITIVES[kind]}\n"

    # --- exercise
    once, _ = redact_secrets(text)
    twice, second_findings = redact_secrets(once)

    # --- verify
    assert twice == once
    assert second_findings == []


def test_redact_handles_multiple_hits_on_one_line() -> None:
    """Right-to-left splicing: a left-hand replacement must not shift the right one."""
    # --- setup
    first = SYNTHETIC_POSITIVES["aws_access_key_id"]
    second = SYNTHETIC_POSITIVES["gitlab_pat"]
    text = f"{first} and {second}\n"

    # --- exercise
    redacted, findings = redact_secrets(text)

    # --- verify
    assert redacted == "[REDACTED:aws_access_key_id] and [REDACTED:gitlab_pat]\n"
    assert len(findings) == 2


def test_redact_preserves_crlf_and_a_missing_trailing_newline() -> None:
    # --- setup
    text = f"alpha\r\nkey: {SYNTHETIC_AWS_KEY}\r\nomega"

    # --- exercise
    redacted, _ = redact_secrets(text)

    # --- verify
    assert redacted == "alpha\r\nkey: [REDACTED:aws_access_key_id]\r\nomega"


def test_redact_leaves_clean_text_byte_identical() -> None:
    # --- exercise
    redacted, findings = redact_secrets(CLEAN_PROSE)

    # --- verify
    assert redacted == CLEAN_PROSE
    assert findings == []


# ---------------------------------------------------------------------------
# apply_guard — mode semantics and the escape hatches
# ---------------------------------------------------------------------------


def test_off_mode_does_not_even_scan() -> None:
    # --- setup
    text = f"key: {SYNTHETIC_AWS_KEY}\n"

    # --- exercise
    outcome = apply_guard(text, mode="off", allow=False, title=_TITLE)

    # --- verify
    assert outcome.content == text
    assert outcome.findings == ()
    assert outcome.redacted is False


def test_warn_mode_reports_but_stores_unchanged() -> None:
    # --- setup
    text = f"key: {SYNTHETIC_AWS_KEY}\n"

    # --- exercise
    outcome = apply_guard(text, mode="warn", allow=False, title=_TITLE)

    # --- verify
    assert outcome.content == text
    assert outcome.redacted is False
    assert [f.kind for f in outcome.findings] == ["aws_access_key_id"]


def test_redact_mode_rewrites_the_content() -> None:
    # --- exercise
    outcome = apply_guard(
        f"key: {SYNTHETIC_AWS_KEY}\n", mode="redact", allow=False, title=_TITLE
    )

    # --- verify
    assert outcome.content == "key: [REDACTED:aws_access_key_id]\n"
    assert outcome.redacted is True


def test_redact_mode_on_clean_content_reports_not_redacted() -> None:
    """``redacted`` must be False when nothing changed, or callers re-hash for nothing."""
    outcome = apply_guard(CLEAN_PROSE, mode="redact", allow=False, title=_TITLE)
    assert outcome.redacted is False
    assert outcome.content == CLEAN_PROSE


def test_reject_mode_raises_with_the_findings_block() -> None:
    # --- exercise / verify
    with pytest.raises(SecretGuardError) as exc:
        apply_guard(
            f"key: {SYNTHETIC_AWS_KEY}\n", mode="reject", allow=False, title=_TITLE
        )

    message = str(exc.value)
    assert "refusing to ingest" in message
    assert _TITLE in message
    assert "aws_access_key_id" in message
    # The refusal message is printed to a terminal — it must not carry the key.
    assert SYNTHETIC_AWS_KEY not in message


def test_reject_mode_passes_clean_content_through() -> None:
    outcome = apply_guard(CLEAN_PROSE, mode="reject", allow=False, title=_TITLE)
    assert outcome.content == CLEAN_PROSE
    assert outcome.findings == ()


@pytest.mark.parametrize("mode", ["reject", "redact"], ids=str)
def test_allow_downgrades_the_action_but_still_reports(mode: str) -> None:
    """The escape hatch suppresses the ACTION, never the evidence."""
    # --- setup
    text = f"key: {SYNTHETIC_AWS_KEY}\n"

    # --- exercise
    outcome = apply_guard(text, mode=mode, allow=True, title=_TITLE)

    # --- verify
    assert outcome.content == text
    assert outcome.redacted is False
    assert [f.kind for f in outcome.findings] == ["aws_access_key_id"]


def test_unknown_mode_raises_value_error() -> None:
    with pytest.raises(ValueError, match="must be one of"):
        apply_guard("body", mode="paranoid", allow=False, title=_TITLE)


# ---------------------------------------------------------------------------
# format_findings
# ---------------------------------------------------------------------------


def test_format_findings_returns_empty_string_for_no_findings() -> None:
    assert format_findings([], title=_TITLE, mode="warn") == ""


@pytest.mark.parametrize(
    ("mode", "expected_marker"),
    [
        ("warn", "stored UNCHANGED"),
        ("redact", "REDACTED before storage"),
        ("reject", "refusing to ingest"),
        ("allow", "guard bypassed for this document"),
    ],
    ids=str,
)
def test_format_findings_header_reflects_the_mode(
    mode: str, expected_marker: str
) -> None:
    # --- setup
    findings = scan_secrets(f"key: {SYNTHETIC_AWS_KEY}\n")

    # --- exercise
    rendered = format_findings(findings, title=_TITLE, mode=mode)

    # --- verify
    assert expected_marker in rendered.splitlines()[0]
    assert "1 finding(s)" in rendered


def test_format_findings_lists_one_row_per_finding_with_location() -> None:
    # --- setup
    text = (
        f"{SYNTHETIC_POSITIVES['aws_access_key_id']}\n"
        f"{SYNTHETIC_POSITIVES['gitlab_pat']}\n"
    )
    findings = scan_secrets(text)

    # --- exercise
    rows = format_findings(findings, title=_TITLE, mode="warn").splitlines()[1:-1]

    # --- verify
    assert len(rows) == 2
    assert "line 1, col 1-" in rows[0]
    assert "aws_access_key_id" in rows[0]
    assert "line 2, col 1-" in rows[1]
    assert "gitlab_pat" in rows[1]
