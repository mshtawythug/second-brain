"""The git hook's regex literal must stay byte-identical to the Python registry.

``scripts/hooks/pre-commit`` cannot ``import brain`` — it runs offline, with no
venv, from a ``git commit`` in any shell. So it keeps its own copy of the
alternation, and this test is the mechanism that stops the copy drifting: add a
pattern to :mod:`brain.secret_patterns` without regenerating the hook line and
this goes red immediately, with the exact command to fix it.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from brain.secret_patterns import egrep_alternation, main
from tests.secret_fixtures import SYNTHETIC_POSITIVES

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "scripts" / "hooks" / "pre-commit"
ALLOWLIST_PATH = REPO_ROOT / ".pii-allowlist.txt"

_BEGIN_MARKER = "# --- BEGIN GENERATED: brain.secret_patterns.egrep_alternation()"
_END_MARKER = "# --- END GENERATED ---"

# The generated assignment, captured between the markers. Single-quoted so the
# shell does no expansion on the regex's braces and brackets.
_ASSIGNMENT_RE = re.compile(r"^_SECRET_RE='(?P<literal>.*)'$", re.MULTILINE)


def _hook_text() -> str:
    return HOOK_PATH.read_text(encoding="utf-8")


def _generated_block(text: str) -> str:
    """Return the text between the two markers, or fail with a useful message."""
    begin = text.find(_BEGIN_MARKER)
    end = text.find(_END_MARKER)
    assert begin != -1, f"BEGIN marker missing from {HOOK_PATH}"
    assert end != -1, f"END marker missing from {HOOK_PATH}"
    assert begin < end, "markers are out of order in the hook"
    return text[begin:end]


def test_hook_exists_and_is_executable() -> None:
    """A non-executable hook is silently skipped by git — worse than a missing one."""
    assert HOOK_PATH.is_file()
    assert HOOK_PATH.stat().st_mode & 0o111, f"{HOOK_PATH} is not executable"


def test_markers_appear_exactly_once() -> None:
    # --- exercise
    text = _hook_text()

    # --- verify
    assert text.count(_BEGIN_MARKER) == 1
    assert text.count(_END_MARKER) == 1


def test_hook_literal_equals_egrep_alternation() -> None:
    """The non-negotiable parity assertion."""
    # --- setup
    block = _generated_block(_hook_text())

    # --- exercise
    match = _ASSIGNMENT_RE.search(block)

    # --- verify
    assert match is not None, (
        "no _SECRET_RE='...' assignment between the generated markers"
    )
    assert match.group("literal") == egrep_alternation(), (
        "scripts/hooks/pre-commit has drifted from brain.secret_patterns.\n"
        "Regenerate with: python -m brain.secret_patterns --emit-egrep"
    )


def test_emit_egrep_reproduces_the_hook_line_verbatim(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--emit-egrep`` output must be paste-ready, not merely similar."""
    # --- setup
    block = _generated_block(_hook_text())
    hook_line = next(
        line for line in block.splitlines() if line.startswith("_SECRET_RE=")
    )

    # --- exercise
    exit_code = main(["--emit-egrep"])
    emitted = capsys.readouterr().out.rstrip("\n")

    # --- verify
    assert exit_code == 0
    assert emitted == hook_line


def test_main_rejects_unknown_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --- exercise
    exit_code = main(["--wat"])

    # --- verify
    assert exit_code == 2
    assert "usage:" in capsys.readouterr().err


def test_hook_still_greps_with_the_generated_variable() -> None:
    """Guard against the literal being regenerated but no longer used.

    Parity on a variable nothing reads would be a green test over a dead gate.
    """
    text = _hook_text()
    assert 'grep -oEi "${_SECRET_RE}"' in text


def test_hook_subtracts_the_allowlist_before_failing() -> None:
    """Stage 1 must honour .pii-allowlist.txt.

    It did not before F4 — only the semantic pass consulted the allowlist —
    which made ``tests/secret_fixtures.py`` uncommittable. Exact-line
    fixed-string matching (``-vixF``) is what keeps a short entry like "API"
    from allowing a longer real token.
    """
    text = _hook_text()
    assert "grep -vixF --" in text


# ---------------------------------------------------------------------------
# Behavioural gate tests — the hook actually RUN against a throwaway repo.
#
# The assertions above are structural: they check the hook's TEXT. These run it.
# The one that matters most is the negative — proving the allowlist subtracts
# specific known-fake TOKENS rather than exempting a file, because a file-scoped
# exemption would let a real credential added to that file later sail through.
# ---------------------------------------------------------------------------

# A credential-shaped value that is deliberately NOT in .pii-allowlist.txt.
# Built by concatenation so this source file contains no literal that matches
# the gate's own regex — otherwise this test would need allowlisting to exist,
# which is exactly the circularity it is here to disprove.
_NOT_ALLOWLISTED_KEY = "AKIA" + ("Z" * 16)

# Stage 2 shells out to `claude -p`. Restricting PATH to the system directories
# keeps it out of reach, so these tests exercise stage 1 in isolation: the hook
# prints a "'claude' not found" warning and exits on the deterministic verdict.
_MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

needs_git_and_isolated_stage_one = pytest.mark.skipif(
    shutil.which("git", path=_MINIMAL_PATH) is None
    or shutil.which("claude", path=_MINIMAL_PATH) is not None,
    reason="needs git on the minimal PATH, and `claude` absent from it",
)


@pytest.fixture
def hook_repo(tmp_path: Path) -> Path:
    """A throwaway git repo wired to the REAL hook and the REAL allowlist."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "hooks").mkdir(parents=True)
    # copy2 preserves the executable bit, which git requires.
    shutil.copy2(HOOK_PATH, repo / "scripts" / "hooks" / "pre-commit")
    shutil.copy2(ALLOWLIST_PATH, repo / ".pii-allowlist.txt")
    for args in (
        ["git", "init", "-q", "."],
        ["git", "config", "user.email", "tester@example.com"],
        ["git", "config", "user.name", "Tester"],
        ["git", "config", "core.hooksPath", "scripts/hooks"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    return repo


def _stage_and_run_hook(repo: Path) -> subprocess.CompletedProcess[str]:
    """Stage everything, then run the hook exactly as ``git commit`` would."""
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    return subprocess.run(
        [str(repo / "scripts" / "hooks" / "pre-commit")],
        cwd=repo,
        env={"PATH": _MINIMAL_PATH, "HOME": str(repo)},
        capture_output=True,
        text=True,
    )


@needs_git_and_isolated_stage_one
def test_stage_one_allows_the_allowlisted_synthetic_fixtures(hook_repo: Path) -> None:
    """The whole point of the allowlist fix: fixtures must be committable."""
    # --- setup
    shutil.copy2(REPO_ROOT / "tests" / "secret_fixtures.py", hook_repo / "fixtures.py")

    # --- exercise
    result = _stage_and_run_hook(hook_repo)

    # --- verify
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    # Confirms stage 2 really was out of reach, so this asserts stage 1's verdict.
    assert "'claude' not found" in result.stderr


@needs_git_and_isolated_stage_one
def test_stage_one_blocks_a_non_allowlisted_key_in_the_same_file(
    hook_repo: Path,
) -> None:
    """THE negative: the allowlist subtracts TOKENS, not files.

    The staged file carries an allowlisted synthetic fixture *and* a credential
    that is not on the list. If the fix had been implemented as a path
    exclusion, this would pass and the gate would be silently dead for that
    file forever after.
    """
    # --- setup
    allowlisted = SYNTHETIC_POSITIVES["aws_access_key_id"]
    (hook_repo / "mixed.py").write_text(
        f'KNOWN_FAKE = "{allowlisted}"\nOOPS = "{_NOT_ALLOWLISTED_KEY}"\n'
    )

    # --- exercise
    result = _stage_and_run_hook(hook_repo)

    # --- verify
    assert result.returncode == 1
    assert "possible secret / API key" in result.stderr


@needs_git_and_isolated_stage_one
def test_the_same_file_passes_once_the_non_allowlisted_key_is_removed(
    hook_repo: Path,
) -> None:
    """Makes the negative above non-vacuous.

    Same path, same allowlisted token, only the offending line removed. Without
    this pair, the block could have come from the file merely *containing* a
    credential rather than from the specific non-allowlisted one.
    """
    # --- setup
    allowlisted = SYNTHETIC_POSITIVES["aws_access_key_id"]
    (hook_repo / "mixed.py").write_text(f'KNOWN_FAKE = "{allowlisted}"\n')

    # --- exercise
    result = _stage_and_run_hook(hook_repo)

    # --- verify
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


@needs_git_and_isolated_stage_one
def test_stage_one_blocks_when_the_allowlist_file_is_absent(
    hook_repo: Path,
) -> None:
    """A missing allowlist must not degrade into an empty-pattern pass-through."""
    # --- setup
    (hook_repo / ".pii-allowlist.txt").unlink()
    (hook_repo / "leak.py").write_text(f'K = "{_NOT_ALLOWLISTED_KEY}"\n')

    # --- exercise
    result = _stage_and_run_hook(hook_repo)

    # --- verify
    assert result.returncode == 1
    assert "possible secret / API key" in result.stderr


@needs_git_and_isolated_stage_one
def test_stage_one_still_blocks_real_looking_emails(hook_repo: Path) -> None:
    """The email heuristic is untouched by the allowlist change — prove it.

    The address is ASSEMBLED in this file and only joined when written into the
    throwaway repo. It has to be: the hook's email stage (unlike the secret
    stage) never consults ``.pii-allowlist.txt``, so a contiguous real-looking
    address in this source would make this very file uncommittable with no
    in-band remedy but ``--no-verify`` — which disables every other gate too.
    The generated ``contacts.py`` still carries the joined form, which is what
    the hook must catch.
    """
    # --- setup
    real_looking = "person@" + "realcompany" + ".io"
    (hook_repo / "contacts.py").write_text(f'OWNER = "{real_looking}"\n')

    # --- exercise
    result = _stage_and_run_hook(hook_repo)

    # --- verify
    assert result.returncode == 1
    assert "real-looking email address" in result.stderr


# ---------------------------------------------------------------------------
# The allowlist must never exempt a REAL PEM banner (C7 iteration 2).
#
# `.pii-allowlist.txt` used to carry the real RSA banner verbatim,
# because that was the literal in `tests/secret_fixtures.py`. On a pasted
# private key the banner is the ONLY text matching any pattern — the base64
# body matches nothing — so subtracting it emptied `secret_hits` and stage 1
# never fired. A genuine key committed silently, and stage 2 was additionally
# told the banner was safe. The fixture now uses a synthetic banner no real key
# can have; these two tests are what stop the real one coming back.
# ---------------------------------------------------------------------------

#: The banner is ASSEMBLED rather than written literally, and that is not
#: squeamishness — the gate this test exercises now blocks a real PEM banner
#: anywhere in a staged diff, comments and test data included. Writing it out
#: here would make this very file uncommittable, and the "obvious" fix for that
#: would be to allowlist the banner, which is precisely the hole being closed.
#: The f-string's source text does not match the pattern; its VALUE does.
_RSA_KIND = "RSA"
_RSA_BANNER = f"-----BEGIN {_RSA_KIND} PRIVATE KEY-----"

#: Shaped exactly like a real PEM RSA key: correct banner, base64-ish body,
#: correct footer. The bytes are not a key and decode to nothing usable.
_GENUINE_SHAPED_RSA_KEY = (
    f"{_RSA_BANNER}\n"
    "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n"
    "KUpRKfFLfRYC9AIKjbJTWit+CqvjWYzvQwECAwEAAQJAIJLixBy2qpFoS4DSmoEm\n"
    f"-----END {_RSA_KIND} PRIVATE KEY-----\n"
)


@needs_git_and_isolated_stage_one
def test_stage_one_blocks_a_genuine_rsa_private_key_header(hook_repo: Path) -> None:
    """A real PEM RSA banner must be caught, not exempted.

    This is the regression that matters: it fails the moment anyone re-adds
    the real RSA banner to the allowlist — which is exactly how
    the hole would come back, since adding it is the obvious way to quiet the
    hook after pasting a fixture.
    """
    # --- setup
    (hook_repo / "leaked_key.pem").write_text(_GENUINE_SHAPED_RSA_KEY)

    # --- exercise
    result = _stage_and_run_hook(hook_repo)

    # --- verify
    assert result.returncode == 1, (
        "a genuine-shaped RSA private key was NOT blocked — check whether a real "
        "PEM banner has been re-added to .pii-allowlist.txt"
    )
    assert "possible secret / API key" in result.stderr


def test_no_allowlist_entry_is_a_real_pem_banner() -> None:
    """Static guard: no entry may be the banner of any real key type.

    Complements the behavioural test above. That one proves the RSA case is
    caught today; this one closes the whole family in one assertion, so adding
    ``EC``/``DSA``/``OPENSSH``/``ENCRYPTED``/bare ``PRIVATE KEY`` fails too
    without needing a test per variant.
    """
    # --- setup
    entries = {
        line.strip().lower()
        for line in (REPO_ROOT / ".pii-allowlist.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    real_banners = {
        f"-----begin {kind}private key-----"
        for kind in ("", "rsa ", "ec ", "dsa ", "openssh ", "encrypted ")
    }

    # --- verify
    assert not (entries & real_banners), (
        f"allowlist exempts a REAL PEM banner: {sorted(entries & real_banners)}. "
        "The banner is the only part of a pasted key that matches any pattern, "
        "so allowlisting it disables the gate for genuine keys."
    )
