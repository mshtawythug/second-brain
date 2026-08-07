"""Regression tests: a wiki build that cannot load Config must NOT publish.

Pins the fix for the 2026-07-26 -> 2026-08-07 outage. ``Config.load`` was
failing (``DATABASE_URL is not set``) on every scheduled build for twelve
days. ``_refresh_pre_build_adornments`` caught the ``ConfigError``, logged a
WARNING, returned, and let the build run and swap anyway. Quartz happily
rendered a complete site from the stale vault mirror, so the wiki looked
completely alive while serving twelve-day-old content with no DB refresh
behind it. A stale surface that looks healthy is worse than one that is
visibly down — the visibly-down one gets fixed within the hour.

Every test here asserts **both halves**. Asserting only "it raises" would let
a publishing regression slip through unnoticed: an exception that escapes
*after* ``_atomic_swap`` would still satisfy a raises-only test while having
already put a DB-less build on the wire. So each test also proves the
``current`` symlink was not retargeted.

Uses ``unittest.mock.patch`` as a standard test double (auto-restoring), not
production monkey-patching. Patching ``brain.config.Config.load`` is the
established pattern in this package — see ``test_build_swap_refresh_skip.py``
— and is necessary here because the repo checkout ships a ``.env`` that
``Config.load`` would walk up and find, so ``delenv("DATABASE_URL")`` alone
would not reproduce the failure.
"""
from __future__ import annotations

import logging
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brain.config import Config, ConfigError
from brain.wiki.build_swap import (
    BUILD_TIMEOUT_ENV_VAR,
    DEFAULT_BUILD_TIMEOUT_S,
    EXIT_BUILD_ERROR,
    EXIT_CONFIG_ERROR,
    _refresh_pre_build_adornments,
    build_and_swap,
    main,
    resolve_build_timeout_s,
)
from brain.wiki.errors import BrainWikiBuildError, BrainWikiConfigError, BrainWikiError

# Synthetic DSN — never connected to. Every DB-touching helper on these paths
# is patched out, so this only has to satisfy Config's field types.
_FAKE_DSN = "postgresql://brain:brain@localhost:1/nonexistent_test_db"


def _make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal vault + ``.quartz`` workspace that passes _check_workspace."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "hello.md").write_text("# hello\n", encoding="utf-8")
    quartz = vault / ".quartz"
    quartz.mkdir()
    (quartz / "quartz.config.ts").write_text("export default {}\n", encoding="utf-8")
    (quartz / "quartz").mkdir()
    (quartz / "quartz" / "bootstrap-cli.mjs").write_text("// stub\n", encoding="utf-8")
    return vault, quartz


def _fake_run_build(**kwargs: object) -> None:
    """Stand-in for ``_run_build`` that emits a minimal site tree."""
    build_dir = kwargs["build_dir"]
    assert isinstance(build_dir, Path)
    build_dir.mkdir(parents=True)
    (build_dir / "index.html").write_text("<html></html>", encoding="utf-8")


def _good_config(vault: Path) -> Config:
    return Config(database_url=_FAKE_DSN, vault_path=vault.expanduser().resolve())


def _config_error() -> ConfigError:
    return ConfigError("DATABASE_URL is not set (see .env.example)")


def test_build_aborts_and_publishes_nothing_when_config_unloadable(
    tmp_path: Path,
) -> None:
    """Config.load fails -> build_and_swap raises AND nothing is published.

    Both halves matter. The raise proves the failure is no longer swallowed;
    the absence of ``current`` and of any build directory proves the abort
    happened *before* the Quartz subprocess and the symlink swap, which is
    what keeps a DB-less build off the wire.
    """
    vault, quartz = _make_workspace(tmp_path)

    with (
        patch("brain.config.Config.load", side_effect=_config_error()),
        patch("brain.wiki.build_swap._run_build", side_effect=_fake_run_build) as spy_build,
        pytest.raises(BrainWikiConfigError) as excinfo,
    ):
        build_and_swap(vault, quartz_dir=quartz, node_path="node")

    # Half 1 — it raises, and the message names the repair.
    assert "could not load config" in str(excinfo.value)
    assert "DATABASE_URL" in str(excinfo.value)
    # It is a BrainWikiError subclass, so `main` and bin/brain-up handle it.
    assert isinstance(excinfo.value, BrainWikiError)

    # Half 2 — nothing was built and nothing was published.
    spy_build.assert_not_called()
    assert not (quartz / "current").exists()
    assert not (quartz / "current").is_symlink()
    assert not (quartz / "builds").exists()


def test_previously_live_build_stays_live_after_aborted_run(tmp_path: Path) -> None:
    """An aborted run must leave the last known-good build serving, untouched.

    This is the property that actually protects readers: the failure mode we
    are fixing is not "the build crashes", it is "a bad build reaches the
    wire". Half-publishing is worse than not publishing.
    """
    vault, quartz = _make_workspace(tmp_path)

    # A first, successful build establishes the live target.
    with (
        patch("brain.config.Config.load", return_value=_good_config(vault)),
        patch("brain.wiki.build_homepage.refresh_homepage", MagicMock()),
        patch("brain.wiki.build_related.refresh_related", MagicMock()),
        patch("brain.wiki.build_swap._run_build", side_effect=_fake_run_build),
    ):
        good = build_and_swap(vault, quartz_dir=quartz, node_path="node")

    live_before = (quartz / "current").resolve()
    assert live_before == good.build_dir.resolve()
    build_ids_before = sorted(p.name for p in (quartz / "builds").iterdir())

    # Now the config breaks — as it did on 2026-07-26.
    with (
        patch("brain.config.Config.load", side_effect=_config_error()),
        patch("brain.wiki.build_swap._run_build", side_effect=_fake_run_build),
        pytest.raises(BrainWikiConfigError),
    ):
        build_and_swap(vault, quartz_dir=quartz, node_path="node")

    # The symlink still resolves to the SAME build, and no new build dir was
    # created (so the aborted run also cannot have GC'd the live one).
    assert (quartz / "current").resolve() == live_before
    assert (quartz / "current" / ".build-id").read_text(encoding="utf-8").strip() == (
        good.build_id
    )
    assert sorted(p.name for p in (quartz / "builds").iterdir()) == build_ids_before


def test_refresh_related_path_aborts_when_config_unloadable(tmp_path: Path) -> None:
    """The refresh_related leg fails loud too, and never runs half-configured.

    ``bin/brain-rebuild`` takes this path (``refresh_related_inline=True``).
    Before the fix it logged "skipping refresh" and returned normally.
    """
    with (
        patch("brain.config.Config.load", side_effect=_config_error()),
        patch("brain.wiki.build_homepage.refresh_homepage") as spy_homepage,
        patch("brain.wiki.build_related.refresh_related") as spy_related,
        pytest.raises(BrainWikiConfigError),
    ):
        _refresh_pre_build_adornments(tmp_path, refresh_related_inline=True)

    # Neither adornment may run against a config we could not load.
    spy_homepage.assert_not_called()
    spy_related.assert_not_called()


def test_config_failure_and_build_failure_get_distinct_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """"Cannot load config" (3) must not be collapsed into "build broke" (1).

    A cron/launchd wrapper — and ``brain doctor``'s "last exited 0" check —
    needs to tell "this box is misconfigured, a human must act" apart from
    "the Quartz build broke, a retry might fix it".
    """
    vault, quartz = _make_workspace(tmp_path)
    argv = ["--vault", str(vault), "--quartz-dir", str(quartz)]

    with patch("brain.config.Config.load", side_effect=_config_error()):
        config_code = main(argv)
    config_err = capsys.readouterr().err

    with (
        patch("brain.config.Config.load", return_value=_good_config(vault)),
        patch("brain.wiki.build_homepage.refresh_homepage", MagicMock()),
        patch("brain.wiki.build_related.refresh_related", MagicMock()),
        patch(
            "brain.wiki.build_swap._run_build",
            side_effect=BrainWikiBuildError("simulated quartz failure"),
        ),
    ):
        build_code = main(argv)
    build_err = capsys.readouterr().err

    assert config_code == EXIT_CONFIG_ERROR == 3
    assert build_code == EXIT_BUILD_ERROR == 1
    assert config_code != build_code

    # Bounded, legible output — no traceback. This branch fires on every
    # scheduled build until a human intervenes, and the incident it comes from
    # accumulated 496 MB of repeated tracebacks. The bound is a handful of
    # lines, not exactly one: `ConfigError` itself renders the .env chain it
    # searched over several lines, which is a repair hint worth keeping. What
    # must never appear is an unbounded stack dump.
    assert "Traceback" not in config_err
    assert len(config_err.strip().splitlines()) <= 12
    assert "wiki build aborted" in config_err
    assert "still live and unchanged" in config_err

    # The build-failure arm stays bounded too, and stays distinguishable in
    # prose as well as in exit code.
    assert "Traceback" not in build_err
    assert len(build_err.strip().splitlines()) == 1
    assert "build failed" in build_err
    assert "build aborted" not in build_err


def _sleep_forever_node(tmp_path: Path) -> Path:
    """An executable stub ``node`` that hangs, to force a subprocess timeout."""
    stub = tmp_path / "node-sleep"
    stub.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", encoding="utf-8"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def test_timed_out_build_is_not_promoted_and_previous_stays_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build killed mid-flight must not reach the wire.

    Same promotion rule as the config abort, different cause. This is the
    live failure mode on the real vault right now: builds are exceeding their
    wall clock, and if a half-written tree were ever promoted the reader would
    get a broken site instead of an old-but-correct one. Verifies the
    ``TimeoutExpired`` branch, not just the ``ConfigError`` branch.
    """
    vault, quartz = _make_workspace(tmp_path)

    with (
        patch("brain.config.Config.load", return_value=_good_config(vault)),
        patch("brain.wiki.build_homepage.refresh_homepage", MagicMock()),
        patch("brain.wiki.build_related.refresh_related", MagicMock()),
        patch("brain.wiki.build_swap._run_build", side_effect=_fake_run_build),
    ):
        good = build_and_swap(vault, quartz_dir=quartz, node_path="node")

    live_before = (quartz / "current").resolve()
    builds_before = sorted(p.name for p in (quartz / "builds").iterdir())

    # The ceiling comes from `resolve_build_timeout_s()` via the env var — NOT
    # from an explicit `timeout_seconds=` argument. That distinction is the
    # whole point: passing the timeout in bypassed the resolver entirely, so
    # this test used to pass identically against the pre-C10 code, which had a
    # `timeout_seconds: float = 600.0` signature default that accepted the same
    # value. Driving it through the env var exercises the unified-ceiling path.
    monkeypatch.setenv(BUILD_TIMEOUT_ENV_VAR, "0.5")
    assert resolve_build_timeout_s() == 0.5, "env seam did not reach the resolver"

    with (
        patch("brain.config.Config.load", return_value=_good_config(vault)),
        patch("brain.wiki.build_homepage.refresh_homepage", MagicMock()),
        patch("brain.wiki.build_related.refresh_related", MagicMock()),
        pytest.raises(BrainWikiError) as excinfo,
    ):
        build_and_swap(
            vault,
            quartz_dir=quartz,
            node_path=str(_sleep_forever_node(tmp_path)),
        )

    # A timeout is a BUILD error, never a CONFIG error — different cause,
    # different remedy. Collapsing them would repeat the mistake being fixed.
    #
    # Asserted against the COMMON base (BrainWikiError) rather than inside
    # `pytest.raises(BrainWikiBuildError)`: the two classes are siblings, so
    # under the narrower raises() the isinstance check was a tautology that
    # could never be False. Catching the base means the type assertion below
    # is the thing actually discriminating.
    assert isinstance(excinfo.value, BrainWikiBuildError)
    assert not isinstance(excinfo.value, BrainWikiConfigError)
    assert "exceeded" in str(excinfo.value)

    # The previously-good build is still the live one, and the timed-out
    # build's partial directory was cleaned up rather than left to be GC'd
    # or served.
    assert (quartz / "current").resolve() == live_before
    assert (quartz / "current" / ".build-id").read_text(
        encoding="utf-8"
    ).strip() == good.build_id
    assert sorted(p.name for p in (quartz / "builds").iterdir()) == builds_before


def test_timeout_and_config_failure_get_different_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main` must not report a slow build as a misconfigured box."""
    vault, quartz = _make_workspace(tmp_path)
    argv = ["--vault", str(vault), "--quartz-dir", str(quartz)]

    with (
        patch("brain.config.Config.load", return_value=_good_config(vault)),
        patch("brain.wiki.build_homepage.refresh_homepage", MagicMock()),
        patch("brain.wiki.build_related.refresh_related", MagicMock()),
        patch(
            "brain.wiki.build_swap._run_build",
            side_effect=BrainWikiBuildError("quartz build exceeded 900s for vault x"),
        ),
    ):
        timeout_code = main(argv)
    err = capsys.readouterr().err

    assert timeout_code == EXIT_BUILD_ERROR == 1
    assert timeout_code != EXIT_CONFIG_ERROR
    assert "exceeded" in err
    assert "Traceback" not in err


def test_build_timeout_is_single_sourced_and_env_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ceiling for a Quartz build, wherever it is launched from.

    `cli.py` used to carry its own 300 s constant against build_swap's 600 s,
    so how long a build was allowed to run depended on which entrypoint you
    came through. Pins that `cli.py` no longer defines a competing constant
    and that both paths read the same resolver.
    """
    import brain.cli as cli_module

    assert not hasattr(cli_module, "_QUARTZ_BUILD_TIMEOUT_S"), (
        "cli.py must not redefine a competing Quartz build timeout"
    )

    monkeypatch.delenv(BUILD_TIMEOUT_ENV_VAR, raising=False)
    assert resolve_build_timeout_s() == DEFAULT_BUILD_TIMEOUT_S == 900.0

    monkeypatch.setenv(BUILD_TIMEOUT_ENV_VAR, "1200")
    assert resolve_build_timeout_s() == 1200.0


def test_malformed_build_timeout_aborts_rather_than_silently_defaulting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd ceiling must not be silently swapped for the default.

    Silently falling back is the same degrade-to-silent-success shape this
    module exists to prevent: the operator believes they set a ceiling, and
    a different one is in force with nothing said.
    """
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv(BUILD_TIMEOUT_ENV_VAR, bad)
        with pytest.raises(BrainWikiConfigError) as excinfo:
            resolve_build_timeout_s()
        assert BUILD_TIMEOUT_ENV_VAR in str(excinfo.value)
        assert bad in str(excinfo.value)


def test_refresh_related_db_failure_logs_at_error_not_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A swallowed DB failure must still be loud in the log.

    ``refresh_related``'s summary is discarded by its callers (the build
    proceeds either way), so this log line is the only signal that the
    sidebar stopped updating. WARNING is what let the original staleness run
    unnoticed for twelve days.
    """
    from brain.wiki.build_related import refresh_related

    cfg = Config(database_url=_FAKE_DSN, vault_path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="brain.wiki.build_related"):
        summary = refresh_related(cfg)

    assert summary.errors  # still best-effort: returns, does not raise
    records = [r for r in caplog.records if r.name == "brain.wiki.build_related"]
    assert records, "expected the DB failure to be logged"
    assert any(r.levelno == logging.ERROR for r in records), (
        "DB failure must log at ERROR — a WARNING is invisible in practice"
    )
