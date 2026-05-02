"""Unit tests for ``brain.wiki.build_swap``.

Exercises the build + swap + GC pipeline against a stub ``npx`` script.
The stub is a real executable Python file written into ``tmp_path`` and
invoked through :func:`subprocess.run` — no monkey-patching of
production code, just an alternate ``npx_path`` argument.

The stub reads two env vars to decide what to do:

- ``STUB_OUTPUT_FLAG`` (default ``1``): whether ``--help`` advertises
  ``--output`` (controls which build method :mod:`build_swap` picks).
- ``STUB_FAIL`` (default ``0``): if ``1``, exit non-zero after partial
  output (lets us assert the cleanup-on-failure path).

Tests set these via :func:`pytest.MonkeyPatch.setenv` so both the probe
subprocess (which inherits the parent's env) and the build subprocess
see the same flags. Each test gets its own ``tmp_path`` and a freshly
cleared probe cache (see ``_clear_probe_cache``) to keep behavior
independent across tests.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from brain.wiki.build_swap import (
    BuildResult,
    _cached_probe,
    _garbage_collect,
    build_and_swap,
    main,
)
from brain.wiki.errors import BrainWikiBuildError, BrainWikiError

# ---------------------------------------------------------------------------
# Stub npx — written into tmp_path so each test runs against a fresh exe.
# ---------------------------------------------------------------------------


_STUB_SOURCE = '''\
#!/usr/bin/env python3
"""Stub ``npx`` for build_swap tests.

Reads ``STUB_OUTPUT_FLAG`` / ``STUB_FAIL`` env vars to decide what to do
without monkey-patching production code. Argv shape mirrors what
build_swap invokes:

  npx quartz build --help
  npx quartz build --directory <vault> [--output <out>]
"""
from __future__ import annotations

import os
import pathlib
import sys


def _emit_help() -> int:
    output_flag = os.environ.get("STUB_OUTPUT_FLAG", "1") == "1"
    text = "Usage: quartz build [options]\\n"
    if output_flag:
        text += "  --output <dir>   write site to dir\\n"
    text += "  --directory <dir>  read content from dir\\n"
    sys.stdout.write(text)
    return 0


def _emit_site(target: pathlib.Path, *, partial: bool) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "index.html").write_text("<html></html>", encoding="utf-8")
    (target / "404.html").write_text("not found", encoding="utf-8")
    if partial:
        # Half-written file that should get cleaned up on failure.
        (target / "broken.html").write_text("<half", encoding="utf-8")


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] != ["quartz"]:
        sys.stderr.write(f"unexpected argv: {argv}\\n")
        return 2
    if argv[1:2] != ["build"]:
        sys.stderr.write(f"unexpected argv: {argv}\\n")
        return 2
    if "--help" in argv:
        return _emit_help()

    fail = os.environ.get("STUB_FAIL", "0") == "1"

    output: pathlib.Path | None = None
    if "--output" in argv:
        idx = argv.index("--output")
        output = pathlib.Path(argv[idx + 1])

    if output is None:
        output = pathlib.Path.cwd() / "public"

    _emit_site(output, partial=fail)

    if fail:
        sys.stderr.write("simulated build failure\\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _stub_npx(tmp_path: Path) -> Path:
    """Write an executable stub-npx script into ``tmp_path``.

    Returns the path the test should pass to ``build_and_swap`` as
    ``npx_path``. Behaviour is parameterized via env vars
    (``STUB_OUTPUT_FLAG``, ``STUB_FAIL``) so a single source covers
    every test scenario — the test sets them via
    :func:`pytest.MonkeyPatch.setenv` before calling.
    """
    stub = tmp_path / "npx-stub"
    stub.write_text(_STUB_SOURCE, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create a vault + ``.quartz`` workspace under ``tmp_path``.

    Returns ``(vault, quartz_dir)``. The workspace gets a stub
    ``quartz.config.ts`` so :func:`build_swap._check_workspace` lets us
    through.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hello.md").write_text("# hello\n")
    quartz = vault / ".quartz"
    quartz.mkdir()
    (quartz / "quartz.config.ts").write_text("export default {}\n")
    return vault, quartz


@pytest.fixture(autouse=True)
def _clear_probe_cache() -> None:
    """Drop the per-process help-probe cache between tests.

    Each test uses a unique ``tmp_path`` so cache keys don't collide,
    but clearing keeps the cache small and the test ordering robust
    against future stub-path reuse.
    """
    _cached_probe.cache_clear()


# ---------------------------------------------------------------------------
# Tests — happy paths.
# ---------------------------------------------------------------------------


def test_happy_path_output_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--output`` is supported → output-flag method runs end-to-end."""
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    vault, quartz = _make_workspace(tmp_path)
    npx = _stub_npx(tmp_path)

    result = build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))

    assert isinstance(result, BuildResult)
    assert result.method == "output-flag"
    assert result.build_dir.is_dir()
    assert result.build_dir.parent == quartz / "builds"
    assert result.build_id == result.build_dir.name
    # .build-id contents = build_id + newline.
    assert (result.build_dir / ".build-id").read_text() == f"{result.build_id}\n"
    # current symlink points at the relative target.
    current = quartz / "current"
    assert current.is_symlink()
    assert os.readlink(current) == f"builds/{result.build_id}"
    # First build: nothing pruned.
    assert result.pruned == []
    assert result.elapsed_seconds >= 0.0


def test_happy_path_rename_public_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--output`` absent → rename-public fallback writes to public/ then renames."""
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "0")
    vault, quartz = _make_workspace(tmp_path)
    npx = _stub_npx(tmp_path)

    result = build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))

    assert result.method == "rename-public"
    assert result.build_dir.is_dir()
    assert (result.build_dir / "index.html").is_file()
    # public/ should no longer exist — it was renamed into the build dir.
    assert not (quartz / "public").exists()


def test_first_build_no_existing_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold start: ``current`` symlink doesn't exist; build creates it."""
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    vault, quartz = _make_workspace(tmp_path)
    npx = _stub_npx(tmp_path)

    assert not (quartz / "current").exists()
    result = build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))
    assert (quartz / "current").is_symlink()
    assert (quartz / "current").resolve() == result.build_dir.resolve()


# ---------------------------------------------------------------------------
# Tests — invariants on the build_id + build_dir layout.
# ---------------------------------------------------------------------------


def test_build_id_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build id = ``YYYYMMDD-HHMMSS-<6 hex>`` exactly."""
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    vault, quartz = _make_workspace(tmp_path)
    npx = _stub_npx(tmp_path)
    result = build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))
    assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{6}$", result.build_id), result.build_id


def test_build_dir_under_builds_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build dir is exactly ``<quartz_dir>/builds/<build_id>``."""
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    vault, quartz = _make_workspace(tmp_path)
    npx = _stub_npx(tmp_path)
    result = build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))
    assert result.build_dir == (quartz / "builds" / result.build_id)


# ---------------------------------------------------------------------------
# Tests — atomicity + GC.
# ---------------------------------------------------------------------------


def test_atomicity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent readers of ``current/.build-id`` never see a missing/partial file.

    Three reader threads each do 50 reads of ``current/.build-id``
    while the main thread runs a build that swaps the symlink. A
    ``threading.Barrier`` aligns the start so reads happen in parallel
    with the rename. We assert no read raised FileNotFoundError and
    that every value read was a complete, valid build id.
    """
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    vault, quartz = _make_workspace(tmp_path)
    npx = _stub_npx(tmp_path)

    # Seed an initial build so readers always have something to read.
    first = build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))

    barrier = threading.Barrier(4)  # 3 readers + main
    errors: list[BaseException] = []
    values: list[str] = []
    values_lock = threading.Lock()

    def _reader() -> None:
        barrier.wait()
        for _ in range(50):
            try:
                v = (quartz / "current" / ".build-id").read_text(encoding="utf-8")
            except FileNotFoundError as exc:
                errors.append(exc)
                return
            with values_lock:
                values.append(v.strip())
            time.sleep(0.001)

    readers = [threading.Thread(target=_reader, name=f"reader-{i}") for i in range(3)]
    for r in readers:
        r.start()

    barrier.wait()
    second = build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))

    for r in readers:
        r.join(timeout=5.0)

    assert not errors, f"reader saw missing-file error: {errors}"
    valid = {first.build_id, second.build_id}
    assert set(values) <= valid, (
        f"unexpected build_id values read: {set(values) - valid}"
    )


def test_gc_keeps_n_plus_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``keep=2`` over 5 builds → after GC at most ``keep`` dirs remain.

    Each build immediately becomes ``current``, so the active is
    always among the most-recent — ``keep`` survives, no extra slot
    needed for the active.
    """
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    vault, quartz = _make_workspace(tmp_path)
    npx = _stub_npx(tmp_path)

    for i in range(5):
        build_and_swap(
            vault, quartz_dir=quartz, keep=2, npx_path=str(npx)
        )
        # mtime resolution is 1s on some filesystems — give each build
        # a distinct mtime so GC sort order is deterministic.
        time.sleep(0.05)
        assert len(list((quartz / "builds").iterdir())) <= 2, (
            f"after build {i + 1}: dir count exceeds keep+1"
        )

    builds = sorted((quartz / "builds").iterdir())
    assert len(builds) == 2


def test_gc_never_deletes_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct GC unit test: a stale ``current`` target survives the keep cut.

    Drives :func:`_garbage_collect` directly with a hand-built builds
    tree — the only way to construct a state where ``current`` points
    at a dir that's NOT in the keep window. (``build_and_swap`` always
    retargets ``current`` at the freshly-built dir before GC, so via
    that path the active is *always* the most recent.)
    """
    _vault, quartz = _make_workspace(tmp_path)
    builds = quartz / "builds"
    builds.mkdir()

    # Lay down 3 build dirs with distinct mtimes — oldest first.
    base = time.time() - 10_000
    for i, name in enumerate(["aaa", "bbb", "ccc"]):
        d = builds / name
        d.mkdir()
        # Older index → older mtime.
        os.utime(d, (base + i, base + i))

    # Pin the OLDEST dir as ``current`` — exactly the case the
    # active-spare rule must defend.
    (quartz / "current").symlink_to(Path("builds") / "aaa")

    pruned = _garbage_collect(quartz, keep=1)

    survivors = {p.name for p in builds.iterdir()}
    # ``ccc`` is the most recent (keep=1); ``aaa`` is the active. Both
    # must survive. ``bbb`` is the only candidate for pruning.
    assert "aaa" in survivors, "active build was deleted — invariant broken!"
    assert "ccc" in survivors
    assert "bbb" not in survivors
    assert {p.name for p in pruned} == {"bbb"}


def test_gc_with_no_active_symlink(tmp_path: Path) -> None:
    """GC handles a missing ``current`` symlink gracefully (cold start)."""
    _vault, quartz = _make_workspace(tmp_path)
    builds = quartz / "builds"
    builds.mkdir()

    base = time.time() - 1_000
    for i, name in enumerate(["a", "b", "c", "d"]):
        d = builds / name
        d.mkdir()
        os.utime(d, (base + i, base + i))

    pruned = _garbage_collect(quartz, keep=2)
    survivors = {p.name for p in builds.iterdir()}
    # 4 dirs, keep=2, no active → drop the 2 oldest.
    assert survivors == {"c", "d"}
    assert {p.name for p in pruned} == {"a", "b"}


# ---------------------------------------------------------------------------
# Tests — failure handling.
# ---------------------------------------------------------------------------


def test_build_failure_no_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing build must not retarget ``current`` and must clean up."""
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    vault, quartz = _make_workspace(tmp_path)
    npx = _stub_npx(tmp_path)

    # Seed a known-good build so we can observe that ``current``
    # didn't move after the failing build.
    good = build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))
    assert (quartz / "current").is_symlink()
    pre_target = os.readlink(quartz / "current")
    pre_count = len(list((quartz / "builds").iterdir()))

    monkeypatch.setenv("STUB_FAIL", "1")
    with pytest.raises(BrainWikiBuildError):
        build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))

    # Symlink still points at the good build.
    assert os.readlink(quartz / "current") == pre_target
    assert (quartz / "current").resolve() == good.build_dir.resolve()
    # Orphan partial build dir was cleaned up — count unchanged.
    post_count = len(list((quartz / "builds").iterdir()))
    assert post_count == pre_count


def test_quartz_dir_missing(tmp_path: Path) -> None:
    """Missing ``quartz.config.ts`` raises :class:`BrainWikiError`."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hello.md").write_text("# hi\n")

    with pytest.raises(BrainWikiError) as exc_info:
        build_and_swap(vault)
    assert "quartz" in str(exc_info.value).lower()


def test_quartz_workspace_dir_absent(tmp_path: Path) -> None:
    """Passing a path that isn't a directory raises :class:`BrainWikiError`."""
    vault = tmp_path / "vault"
    vault.mkdir()
    bogus = tmp_path / "nope"

    with pytest.raises(BrainWikiError):
        build_and_swap(vault, quartz_dir=bogus)


def test_timeout_raises_build_error(tmp_path: Path) -> None:
    """A subprocess that exceeds ``timeout_seconds`` surfaces as a build error."""
    vault, quartz = _make_workspace(tmp_path)

    # Stub that sleeps forever — written inline so we don't have to
    # plumb yet another env var into the shared stub.
    stub = tmp_path / "npx-sleep"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "if '--help' in sys.argv:\n"
        "    print('--output supported')\n"
        "    sys.exit(0)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    with pytest.raises(BrainWikiBuildError):
        build_and_swap(
            vault,
            quartz_dir=quartz,
            npx_path=str(stub),
            timeout_seconds=0.5,
        )
    # No build dir lingering after timeout.
    builds_dir = quartz / "builds"
    if builds_dir.exists():
        assert not any(builds_dir.iterdir())


def test_npx_not_found_surfaces_build_error(tmp_path: Path) -> None:
    """Pointing at a non-existent ``npx_path`` raises :class:`BrainWikiBuildError`.

    The probe falls back to ``rename-public`` on a missing binary, but
    the build subprocess itself then fails with FileNotFoundError —
    which we wrap in :class:`BrainWikiBuildError` so callers don't need
    to know about subprocess internals.
    """
    vault, quartz = _make_workspace(tmp_path)
    bogus = tmp_path / "does-not-exist"

    with pytest.raises((BrainWikiBuildError, BrainWikiError)):
        build_and_swap(vault, quartz_dir=quartz, npx_path=str(bogus))
    builds_dir = quartz / "builds"
    if builds_dir.exists():
        assert not any(builds_dir.iterdir())


# ---------------------------------------------------------------------------
# Tests — probe caching.
# ---------------------------------------------------------------------------


def test_probe_runs_only_once_per_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The help-probe is cached so subsequent builds skip ``--help``.

    Two consecutive ``build_and_swap`` calls against the same workspace
    should issue at most one ``--help`` probe — otherwise we'd add a
    full ``npx`` warmup tax to every build.
    """
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    vault, quartz = _make_workspace(tmp_path)
    npx = _stub_npx(tmp_path)

    build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))
    info_after_one = _cached_probe.cache_info()
    build_and_swap(vault, quartz_dir=quartz, npx_path=str(npx))
    info_after_two = _cached_probe.cache_info()

    assert info_after_one.misses == 1
    # Second call hit the cache rather than incurring another miss.
    assert info_after_two.misses == 1
    assert info_after_two.hits >= 1


# ---------------------------------------------------------------------------
# Sanity check on the stub itself — keeps test failures pointing at the
# right layer when the stub is wrong rather than the production code.
# ---------------------------------------------------------------------------


def test_stub_help_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub's ``--help`` advertises ``--output`` when STUB_OUTPUT_FLAG=1."""
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    npx = _stub_npx(tmp_path)
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(npx), "quartz", "build", "--help"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0
    assert "--output" in result.stdout


# ---------------------------------------------------------------------------
# Tests — ``python -m brain.wiki.build_swap`` CLI entry point.
#
# These tests drive :func:`brain.wiki.build_swap.main` directly via in-process
# argument parsing rather than spawning a subprocess. ``build_and_swap`` is
# stubbed via ``mocker.patch`` (a real test double, not monkey-patching of
# production code) so the CLI shape can be asserted without depending on a
# real Quartz install.
# ---------------------------------------------------------------------------


def test_main_prints_build_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main()`` prints the build id + elapsed time on success and returns 0."""
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    vault, quartz = _make_workspace(tmp_path)
    npx = _stub_npx(tmp_path)

    # Run the real build through main() but with the stub npx + isolated
    # workspace — exercises the full code path including parsing, build,
    # swap, GC, and stdout formatting.
    monkeypatch.setattr(
        "brain.wiki.build_swap.build_and_swap",
        lambda v, *, quartz_dir=None, keep=3: build_and_swap(
            v, quartz_dir=quartz_dir, keep=keep, npx_path=str(npx)
        ),
    )

    rc = main(
        [
            "--vault",
            str(vault),
            "--quartz-dir",
            str(quartz),
            "--keep",
            "2",
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    # Stdout has one line: ``<build_id> (<elapsed>s, method=<m>)``.
    line = captured.out.strip()
    assert re.match(
        r"^\d{8}-\d{6}-[0-9a-f]{6} \(\d+\.\d{2}s, method=(output-flag|rename-public)\)$",
        line,
    ), line


def test_main_returns_nonzero_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A wrapped :class:`BrainWikiError` from the build → exit 1 with stderr msg."""
    vault, quartz = _make_workspace(tmp_path)
    monkeypatch.setenv("STUB_OUTPUT_FLAG", "1")
    monkeypatch.setenv("STUB_FAIL", "1")
    npx = _stub_npx(tmp_path)

    monkeypatch.setattr(
        "brain.wiki.build_swap.build_and_swap",
        lambda v, *, quartz_dir=None, keep=3: build_and_swap(
            v, quartz_dir=quartz_dir, keep=keep, npx_path=str(npx)
        ),
    )

    rc = main(["--vault", str(vault), "--quartz-dir", str(quartz)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "build failed" in captured.err.lower()


def test_main_argument_validation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing ``--vault`` exits 2 (argparse) — not a wrapped 1.

    argparse calls ``sys.exit(2)`` on argument parse errors before
    ``main`` returns, so the test asserts on the SystemExit code rather
    than ``main``'s return.
    """
    with pytest.raises(SystemExit) as exc_info:
        main([])
    # argparse exits with 2 on unknown/missing arguments.
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "--vault" in captured.err
