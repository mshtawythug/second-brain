"""The ``--pick`` preview filter (RM-52, sec-121 F1): nothing that can drive a terminal gets out.

Real ``fzf`` passes iTerm2, tmux-wrapped, sixel, kitty and OSC 8 sequences straight to the terminal,
and ``brain show`` prints a document raw. All data is synthetic.
"""
from __future__ import annotations

import stat
import subprocess
import time
from pathlib import Path

import pytest

from brain.preview_filter import clean_line
from brain.search_pick import fzf_argv

pytestmark = pytest.mark.nodb

ID = "0a1b2c3d-1111-4222-8333-444455556666"
CANARY = "cHduZWQ"
HOSTILE = {
    "iterm2-copy": f"\x1b]1337;Copy=:{CANARY}=\x07",
    "iterm2-uservar-st": f"\x1b]1337;SetUserVar=x={CANARY}\x1b\\",
    "osc52": f"\x1b]52;c;{CANARY}=\x07",
    "tmux-osc52": f"\x1bPtmux;\x1b\x1b]52;c;{CANARY}=\x07\x1b\\",
    "sixel": f"\x1bPq#0;2;0;0;0#0~~{CANARY}\x1b\\",
    "kitty": f"\x1b_Gf=100,a=T;{CANARY}\x1b\\",
    "osc8": f"\x1b]8;;https://{CANARY}.invalid\x1b\\",
    "csi": f"\x1b[2J\x1b[38;5;1m\x1b[?1049h{CANARY}"[: -len(CANARY)],
}


@pytest.mark.parametrize("payload", HOSTILE.values(), ids=HOSTILE.keys())
def test_a_whole_sequence_goes_with_its_payload(payload: str) -> None:
    # tmux's wrapper is the one exception: stopping a string at any ESC is what keeps the regex
    # linear, so its inert word stays. Its payload does not.
    leftover = "tmux;" if payload.startswith("\x1bPtmux;") else ""
    assert clean_line(f"before{payload}after\n") == f"before{leftover}after\n"


@pytest.mark.parametrize("c1", ["\u009b", "\u009d", "\u0090", "\u009e", "\u009f", "\u0085"])
def test_c1_controls_go_and_the_words_around_them_stay(c1: str) -> None:
    assert clean_line(f"before{c1}31m after\n") == "before31m after\n"


@pytest.mark.parametrize(
    "c0", [chr(n) for n in range(0x20) if n not in (0x09, 0x0a, 0x1b)] + ["\x7f"]
)
def test_c0_controls_and_del_go(c0: str) -> None:
    assert clean_line(f"a{c0}b") == "ab"


@pytest.mark.parametrize("escape", ["\x1b", "\x1bc", "\x1b(B", "\x1b7"])
def test_a_bare_escape_or_a_short_escape_sequence_goes(escape: str) -> None:
    assert clean_line(f"a{escape}\n") == "a\n"


@pytest.mark.parametrize(
    "bidi",
    [
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    ],
)
def test_bidi_overrides_go(bidi: str) -> None:
    assert clean_line(f"a{bidi}b") == "ab"


def test_tag_characters_go() -> None:
    hidden = "".join(chr(0xe0000 + ord(char)) for char in "hidden")
    assert clean_line(f"a\U000e0001{hidden}\U000e007fb") == "ab"


@pytest.mark.parametrize(
    "ordinary",
    ["می\u200cخواهم", "क\u094d\u200dष", "family \U0001f468\u200d\U0001f469 emoji"],
    ids=["zwnj-persian", "zwj-devanagari", "zwj-emoji"],
)
def test_zero_width_joiners_stay_because_real_text_needs_them(ordinary: str) -> None:
    assert clean_line(ordinary) == ordinary


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_unicode_line_separators_become_newlines(separator: str) -> None:
    assert clean_line(f"a{separator}b") == "a\nb"


@pytest.mark.parametrize(
    "unterminated", ["\x1b]1337;Copy=:x", "\x1bPq#0", "\x1b_Gf=100"]
)
def test_an_unterminated_sequence_leaves_no_control_and_keeps_the_following_words(
    unterminated: str,
) -> None:
    cleaned = clean_line(f"before{unterminated} after\n")
    assert "\x1b" not in cleaned
    assert cleaned.startswith("before")
    assert cleaned.endswith(" after\n")


@pytest.mark.parametrize(
    "ordinary",
    [
        "季度计划\n",
        "Çà et là, résumé\n",
        "Planning 🚀 notes\n",
        "family 👨‍👩‍👧 emoji\n",
        "col one\tcol two\n",
        "\n",
        "# Title [not a sequence] (ok) ~ 100%\n",
        "שלום hebrew and مرحبا arabic\n",
    ],
)
def test_ordinary_text_is_untouched(ordinary: str) -> None:
    assert clean_line(ordinary) == ordinary


@pytest.mark.parametrize(
    "hostile",
    [
        "\x1b]" * 200_000,
        "\x1bP" * 200_000,
        "\x1b_" * 200_000,
        "\x1bP" + "\x1b\x1b" * 200_000,
        "\x1bP" + "\x1b\x1bP" * 32_000,
        "\x1b]" + "\x1b\x1b]" * 32_000,
        "\x1b[" + "0" * 200_000,
    ],
    ids=[
        "osc-starts",
        "dcs-starts",
        "apc-starts",
        "doubled-escapes",
        "dcs-doubled-repeated",
        "osc-doubled-repeated",
        "csi-params",
    ],
)
def test_a_pathological_line_stays_linear(hostile: str) -> None:
    """96 KB of a repeated form took 45s when DCS allowed doubled ESCs (sec-121b N1)."""
    started = time.monotonic()
    cleaned = clean_line(hostile)
    assert time.monotonic() - started < 5.0
    assert "\x1b" not in cleaned


# --- The real pipeline: the argv's preview string, through /bin/sh, with a stub `brain` ----------


def _stub_brain(tmp_path: Path, body: bytes) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "body.bin").write_bytes(body)
    stub = bin_dir / "brain"
    stub.write_text(f'#!/bin/sh\necho "args:$#:$2"\ncat "{bin_dir}/body.bin"\n', encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _preview_string() -> str:
    argv = fzf_argv("/fake/fzf")
    return argv[argv.index("--preview") + 1]


def _run_preview(bin_dir: Path, cwd: Path) -> bytes:
    command = _preview_string().replace("{1}", f"'{ID}'")
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        cwd=cwd,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=30,
        check=True,
    ).stdout


def test_the_preview_string_is_fixed_but_for_the_field_index() -> None:
    preview = _preview_string()
    assert preview.startswith("brain show {1} 2>&1 | ")
    assert preview.count("{") == 1
    assert "--query" not in preview


def test_the_preview_string_cleans_what_brain_show_prints(tmp_path: Path) -> None:
    payload = "".join(HOSTILE.values())
    body = f"# {payload}Title{payload}\n\nbody{payload} line\u009b\n".encode()
    out = _run_preview(_stub_brain(tmp_path, body), tmp_path)
    # One inert "tmux;" per payload: test_a_whole_sequence_goes_with_its_payload says why.
    assert out == f"args:2:{ID}\n# tmux;Titletmux;\n\nbodytmux; line\n".encode()


def test_the_preview_string_keeps_ordinary_text(tmp_path: Path) -> None:
    body = "# 季度计划\n\nÇà et là\tcol\n\nlast\n".encode()
    out = _run_preview(_stub_brain(tmp_path, body), tmp_path)
    assert out == f"args:2:{ID}\n".encode() + body


def test_raw_8_bit_controls_become_replacement_characters_not_controls(tmp_path: Path) -> None:
    out = _run_preview(_stub_brain(tmp_path, b"raw\x9b31m \x9d0;x\xff\n"), tmp_path)
    assert b"\x9b" not in out.replace("\ufffd".encode(), b"")
    assert b"\x9d" not in out.replace("\ufffd".encode(), b"")
    assert b"31m" in out


def test_the_filter_ignores_a_planted_module_in_the_working_directory(tmp_path: Path) -> None:
    planted = tmp_path / "cwd" / "brain"
    planted.mkdir(parents=True)
    (planted / "__init__.py").write_text("", encoding="utf-8")
    (planted / "preview_filter.py").write_text("print('PLANTED')\n", encoding="utf-8")
    out = _run_preview(_stub_brain(tmp_path, b"clean\n"), tmp_path / "cwd")
    assert b"PLANTED" not in out
    assert b"clean" in out


def test_a_missing_brain_shows_the_shell_error_in_the_pane(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert b"brain" in _run_preview(empty, tmp_path)
