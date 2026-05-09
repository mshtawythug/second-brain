"""Unit tests for ``brain.wiki.build_swap``.

Exercises the build + swap + GC pipeline against a stub ``node`` script.
The stub is a real executable Python file written into ``tmp_path`` and
invoked through :func:`subprocess.run` — no monkey-patching of
production code, just an alternate ``node_path`` argument.

The stub reads one env var to decide what to do:

- ``STUB_FAIL`` (default ``0``): if ``1``, exit non-zero after partial
  output (lets us assert the cleanup-on-failure path).

Tests set these via :func:`pytest.MonkeyPatch.setenv` so the build
subprocess sees the right flags.  Each test gets its own ``tmp_path``
to keep behavior independent across tests.
"""
from __future__ import annotations

import os
import re
import stat
import threading
import time
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from brain.wiki.build_swap import (
    BuildResult,
    _garbage_collect,
    _run_build,
    build_and_swap,
    main,
)
from brain.wiki.errors import BrainWikiBuildError, BrainWikiError

# ---------------------------------------------------------------------------
# Stub node — written into tmp_path so each test runs against a fresh exe.
# ---------------------------------------------------------------------------


_NODE_STUB_SOURCE = '''\
#!/usr/bin/env python3
"""Stub ``node`` for build_swap tests.

Mimics the calling convention of
``node <workspace>/quartz/bootstrap-cli.mjs build --directory <vault> --output <out>``.
Argv shape when invoked:

  argv[0] = this stub (acts as the node binary)
  argv[1] = path to bootstrap-cli.mjs (ignored — just here for shape)
  argv[2] = "build"
  argv[3] = "--directory"
  argv[4] = <vault>
  argv[5] = "--output"
  argv[6] = <build_dir>

Reads ``STUB_FAIL`` env var: if ``1``, emit partial output and exit 1.
"""
from __future__ import annotations

import os
import pathlib
import sys


def _emit_site(target: pathlib.Path, *, partial: bool) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "index.html").write_text("<html></html>", encoding="utf-8")
    (target / "404.html").write_text("not found", encoding="utf-8")
    if partial:
        # Half-written file that should get cleaned up on failure.
        (target / "broken.html").write_text("<half", encoding="utf-8")


def main() -> int:
    # argv[1] = bootstrap-cli.mjs path (ignored), argv[2] = "build"
    argv = sys.argv[1:]  # drop the stub (node) path

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


def _stub_node(tmp_path: Path) -> Path:
    """Write an executable stub-node script into ``tmp_path``.

    Returns the path the test should pass to ``build_and_swap`` as
    ``node_path``.  Behaviour is parameterized via env vars (``STUB_FAIL``)
    so a single source covers every test scenario — the test sets them via
    :func:`pytest.MonkeyPatch.setenv` before calling.
    """
    stub = tmp_path / "node-stub"
    stub.write_text(_NODE_STUB_SOURCE, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create a vault + ``.quartz`` workspace under ``tmp_path``.

    Returns ``(vault, quartz_dir)``. The workspace gets a stub
    ``quartz.config.ts`` so :func:`build_swap._check_workspace` lets us
    through, and a stub ``quartz/bootstrap-cli.mjs`` so the node-direct
    bootstrap check passes.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hello.md").write_text("# hello\n")
    quartz = vault / ".quartz"
    quartz.mkdir()
    (quartz / "quartz.config.ts").write_text("export default {}\n")
    # bootstrap-cli.mjs must exist for _check_workspace to pass.
    (quartz / "quartz").mkdir()
    (quartz / "quartz" / "bootstrap-cli.mjs").write_text("// stub\n")
    return vault, quartz


# ---------------------------------------------------------------------------
# Tests — happy paths.
# ---------------------------------------------------------------------------


def test_happy_path_output_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--output`` is supported → output-flag method runs end-to-end."""
    vault, quartz = _make_workspace(tmp_path)
    node = _stub_node(tmp_path)

    result = build_and_swap(vault, quartz_dir=quartz, node_path=str(node))

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


def test_first_build_no_existing_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold start: ``current`` symlink doesn't exist; build creates it."""
    vault, quartz = _make_workspace(tmp_path)
    node = _stub_node(tmp_path)

    assert not (quartz / "current").exists()
    result = build_and_swap(vault, quartz_dir=quartz, node_path=str(node))
    assert (quartz / "current").is_symlink()
    assert (quartz / "current").resolve() == result.build_dir.resolve()


# ---------------------------------------------------------------------------
# Tests — invariants on the build_id + build_dir layout.
# ---------------------------------------------------------------------------


def test_build_id_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build id = ``YYYYMMDD-HHMMSS-<6 hex>`` exactly."""
    vault, quartz = _make_workspace(tmp_path)
    node = _stub_node(tmp_path)
    result = build_and_swap(vault, quartz_dir=quartz, node_path=str(node))
    assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{6}$", result.build_id), result.build_id


def test_build_dir_under_builds_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build dir is exactly ``<quartz_dir>/builds/<build_id>``."""
    vault, quartz = _make_workspace(tmp_path)
    node = _stub_node(tmp_path)
    result = build_and_swap(vault, quartz_dir=quartz, node_path=str(node))
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
    vault, quartz = _make_workspace(tmp_path)
    node = _stub_node(tmp_path)

    # Seed an initial build so readers always have something to read.
    first = build_and_swap(vault, quartz_dir=quartz, node_path=str(node))

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
    second = build_and_swap(vault, quartz_dir=quartz, node_path=str(node))

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
    vault, quartz = _make_workspace(tmp_path)
    node = _stub_node(tmp_path)

    for i in range(5):
        build_and_swap(
            vault, quartz_dir=quartz, keep=2, node_path=str(node)
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
    vault, quartz = _make_workspace(tmp_path)
    node = _stub_node(tmp_path)

    # Seed a known-good build so we can observe that ``current``
    # didn't move after the failing build.
    good = build_and_swap(vault, quartz_dir=quartz, node_path=str(node))
    assert (quartz / "current").is_symlink()
    pre_target = os.readlink(quartz / "current")
    pre_count = len(list((quartz / "builds").iterdir()))

    monkeypatch.setenv("STUB_FAIL", "1")
    with pytest.raises(BrainWikiBuildError):
        build_and_swap(vault, quartz_dir=quartz, node_path=str(node))

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

    # Stub node that sleeps forever — written inline so we don't need to
    # plumb extra env vars into the shared stub.
    stub = tmp_path / "node-sleep"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    with pytest.raises(BrainWikiBuildError):
        build_and_swap(
            vault,
            quartz_dir=quartz,
            node_path=str(stub),
            timeout_seconds=0.5,
        )
    # No build dir lingering after timeout.
    builds_dir = quartz / "builds"
    if builds_dir.exists():
        assert not any(builds_dir.iterdir())


def test_node_path_bogus_surfaces_build_error(tmp_path: Path) -> None:
    """Pointing ``node_path`` at a non-existent binary raises :class:`BrainWikiBuildError`.

    A missing binary causes a FileNotFoundError (OSError subclass) in the
    build subprocess, which we wrap in :class:`BrainWikiBuildError` so
    callers don't need to know about subprocess internals.
    """
    vault, quartz = _make_workspace(tmp_path)
    bogus = tmp_path / "does-not-exist"

    with pytest.raises((BrainWikiBuildError, BrainWikiError)):
        build_and_swap(vault, quartz_dir=quartz, node_path=str(bogus))
    builds_dir = quartz / "builds"
    if builds_dir.exists():
        assert not any(builds_dir.iterdir())


# ---------------------------------------------------------------------------
# Regression tests — hard-fail paths.
# ---------------------------------------------------------------------------


def test_build_and_swap_hard_fails_on_missing_bootstrap(tmp_path: Path) -> None:
    """``build_and_swap`` raises :class:`BrainWikiBuildError` when bootstrap-cli.mjs is absent.

    Regression guard: the node-direct build path must fail loud with a
    repair-path message rather than silently falling back to npx or
    producing a confusing subprocess error about a missing script.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hello.md").write_text("# hello\n")
    quartz = vault / ".quartz"
    quartz.mkdir()
    (quartz / "quartz.config.ts").write_text("export default {}\n")
    # Deliberately do NOT create quartz/bootstrap-cli.mjs.

    with pytest.raises(BrainWikiBuildError, match="repair"):
        build_and_swap(vault, quartz_dir=quartz, node_path="/usr/local/bin/node")


def test_build_and_swap_hard_fails_when_node_missing(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """``build_and_swap`` raises :class:`BrainWikiBuildError` when node is not on PATH.

    Regression guard: when ``node_path`` is ``None`` and ``shutil.which``
    cannot locate the node binary, we must fail immediately with a clear
    install hint rather than attempting any subprocess or npx fallback.
    """
    vault, quartz = _make_workspace(tmp_path)

    mocker.patch("brain.wiki.build_swap.shutil.which", return_value=None)

    with pytest.raises(BrainWikiBuildError, match="node binary not found"):
        build_and_swap(vault, quartz_dir=quartz)


def test_build_and_swap_warns_when_npx_path_passed(tmp_path: Path) -> None:
    """``build_and_swap`` emits :class:`DeprecationWarning` when ``npx_path`` is non-None.

    Regression guard: Task 5 made the build node-direct and deprecated
    ``npx_path``.  The parameter is kept on the public signature for API
    compatibility but must surface a runtime warning so any caller still
    passing it knows the value is ignored.  Pairing the warning with a
    forced failure (missing bootstrap) keeps the test fast and avoids
    standing up a real subprocess stub.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    quartz = vault / ".quartz"
    quartz.mkdir()
    (quartz / "quartz.config.ts").write_text("export default {}\n")
    # Bootstrap intentionally absent → BrainWikiBuildError fires after warning.

    with (
        pytest.warns(DeprecationWarning, match="npx_path is deprecated"),
        pytest.raises(BrainWikiBuildError),
    ):
        build_and_swap(
            vault,
            quartz_dir=quartz,
            node_path="/usr/local/bin/node",
            npx_path="npx",
        )


# ---------------------------------------------------------------------------
# Regression test — output-flag + node-direct pinning.
# ---------------------------------------------------------------------------


def test_run_build_uses_output_flag(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    """``_run_build`` invokes ``node bootstrap-cli.mjs`` and always passes ``--output``.

    Regression guard covering two invariants:
    1. The first arg to the subprocess must be the node binary (not npx).
    2. The second arg must end with ``bootstrap-cli.mjs``.
    3. ``--output <build_dir>`` must appear in the args.

    Originally introduced in Task 4 to pin ``--output`` (eliminating the
    lru_cache-poisoned probe).  Extended in Task 5 to also pin the
    node-direct calling convention.
    """
    _vault, quartz = _make_workspace(tmp_path)
    build_dir = quartz / "builds" / "20260101-000000-abcdef"
    build_dir.mkdir(parents=True)

    node = "/usr/local/bin/node"
    spy = mocker.patch("subprocess.run")

    _run_build(
        node_path=node,
        workspace=quartz,
        vault=tmp_path / "vault",
        build_dir=build_dir,
        timeout_seconds=60.0,
        env=None,
    )

    spy.assert_called_once()
    call_args: list[str] = spy.call_args.args[0]

    # First arg must be the node binary.
    assert call_args[0] == node, (
        f"expected node binary as first arg, got: {call_args}"
    )
    # Second arg must be the bootstrap-cli.mjs path.
    assert call_args[1].endswith("bootstrap-cli.mjs"), (
        f"expected bootstrap-cli.mjs as second arg, got: {call_args}"
    )
    # --output flag must be present with build_dir as its value.
    assert "--output" in call_args, (
        f"--output not found in subprocess args: {call_args}"
    )
    output_idx = call_args.index("--output")
    assert call_args[output_idx + 1] == str(build_dir)


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
    vault, quartz = _make_workspace(tmp_path)
    node = _stub_node(tmp_path)

    # Run the real build through main() but with the stub node + isolated
    # workspace — exercises the full code path including parsing, build,
    # swap, GC, and stdout formatting.
    monkeypatch.setattr(
        "brain.wiki.build_swap.build_and_swap",
        lambda v, *, quartz_dir=None, keep=3: build_and_swap(
            v, quartz_dir=quartz_dir, keep=keep, node_path=str(node)
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
    # Stdout has one line: ``<build_id> (<elapsed>s, method=output-flag)``.
    # method is always output-flag — Quartz 4.5.x is pinned.
    line = captured.out.strip()
    assert re.match(
        r"^\d{8}-\d{6}-[0-9a-f]{6} \(\d+\.\d{2}s, method=output-flag\)$",
        line,
    ), line


def test_main_returns_nonzero_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A wrapped :class:`BrainWikiError` from the build → exit 1 with stderr msg."""
    vault, quartz = _make_workspace(tmp_path)
    monkeypatch.setenv("STUB_FAIL", "1")
    node = _stub_node(tmp_path)

    monkeypatch.setattr(
        "brain.wiki.build_swap.build_and_swap",
        lambda v, *, quartz_dir=None, keep=3: build_and_swap(
            v, quartz_dir=quartz_dir, keep=keep, node_path=str(node)
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
