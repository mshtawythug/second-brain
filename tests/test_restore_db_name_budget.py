"""`brain restore` must refuse an over-long database name, in production.

Postgres truncates identifiers at ``NAMEDATALEN - 1`` = 63 BYTES and does it
SILENTLY. ``_restore_database`` derives ``{live}_replaced_<stamp>`` from the
live database name, which costs 25 bytes, so any brain database name over 38
bytes produced a `database "..." does not exist` that named a database the user
could see in their own DSN — with the length appearing nowhere in the error.

That budget was previously written down ONLY in ``tests/backup_fakes.py``,
which is the wrong place for a constraint production imposes: the test harness
kept its own names inside a limit the shipped code never enforced.

TWO CALL SITES, BOTH LOAD-BEARING — this is not belt-and-braces, and each was
mutation-proved separately:

* ``restore_backup`` checks BEFORE the pre-restore backup. Removing only that
  call leaves the suite failing on ``assert taken == []`` below: the later
  guard still refuses, but only after a full ``pg_dump`` the user waited for
  and can never use.
* ``_restore_database`` checks where the names are actually derived, which is
  the enforcement point for a direct caller. The same mutation showed it still
  raising a matching ``BackupError``, so it is reachable on its own.

WHY ``prepared=`` APPEARS IN THE ORDERING TEST. Without it, ``restore_backup``
unpacks the archive first and dies on "no such archive" before the pre-restore
backup, so the ordering assertion passed for the wrong reason and stayed green
under mutation. Skipping that phase is what makes the claim provable.

THE LITERALS 63 / 25 / 38 ARE PINNED AS LITERALS ON PURPOSE. Asserting
``MAX_RESTORABLE_DB_NAME_BYTES == PG_IDENTIFIER_MAX_BYTES - ...`` would restate
the production expression and pass for any pair of wrong numbers — the exact
self-consistency trap that let a sibling harness constant be "corrected" from
63 to 64 with its whole file still green. 63 is Postgres' limit, not this
repo's opinion.

NOTE ON THE CONVENTION. The guard reports the exact byte budget and does NOT
suggest shortening anything. Sibling test databases here are deliberately
``second_brain_test_<worktree-slug>`` — one per worktree, reused across
sessions; per-task names are the anti-pattern that once left dozens of stale
databases behind. Telling someone how many bytes they have keeps that
convention available; telling them to "use a shorter name" would not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.backup.create import TIMESTAMP_FORMAT
from brain.backup.restore import (
    MAX_RESTORABLE_DB_NAME_BYTES,
    PG_IDENTIFIER_MAX_BYTES,
    RESTORE_DERIVED_SUFFIX_BYTES,
    PreparedRestore,
    _validated_restorable_db_name,
    restore_backup,
)
from brain.config import Config
from brain.errors import BackupError
from tests.test_backup_manifest import _manifest


def _name_of(size: int) -> str:
    """A database name of exactly ``size`` bytes that ``_DB_NAME_RE`` accepts."""
    return "b" * size


def test_the_limit_and_budget_are_pinned_to_literals() -> None:
    """Pinned to Postgres' reality, not to the production expression itself."""
    assert PG_IDENTIFIER_MAX_BYTES == 63
    assert RESTORE_DERIVED_SUFFIX_BYTES == 25
    assert MAX_RESTORABLE_DB_NAME_BYTES == 38


def test_the_suffix_budget_matches_what_restore_actually_appends() -> None:
    """The 25 comes from the real ``TIMESTAMP_FORMAT``, not from a guess."""
    stamp = datetime(2026, 8, 20, 14, 30, 5, tzinfo=UTC).strftime(TIMESTAMP_FORMAT)
    assert stamp == "20260820-143005"
    assert len(f"_replaced_{stamp.replace('-', '_')}") == 25
    assert len(f"_restore_{stamp.replace('-', '_')}") == 24


def test_a_name_at_the_budget_is_accepted() -> None:
    """The boundary is inclusive, and a name at it derives exactly 63 bytes."""
    name = _name_of(MAX_RESTORABLE_DB_NAME_BYTES)
    assert _validated_restorable_db_name(name) == name
    assert len(f"{name}_replaced_20260820_143005") == 63


def test_one_byte_past_the_budget_is_refused() -> None:
    """Prove the guard can fail, and that its message is actionable."""
    name = _name_of(MAX_RESTORABLE_DB_NAME_BYTES + 1)
    with pytest.raises(BackupError) as excinfo:
        _validated_restorable_db_name(name)
    message = str(excinfo.value)
    assert name in message
    assert "38 bytes" in message
    assert "39 bytes" in message


def test_restore_refuses_before_taking_the_pre_restore_backup(tmp_path: Path) -> None:
    """The early call site, isolated: no pg_dump is taken for a doomed restore."""
    over = _name_of(MAX_RESTORABLE_DB_NAME_BYTES + 1)
    cfg = Config(
        database_url=f"postgresql://brain:brain@localhost:5434/{over}",
        embedder="fake",
        vault_path=tmp_path / "vault",
        backup_dir=tmp_path / "backups",
        brain_home=tmp_path / "brain-home",
    )
    taken: list[Path] = []

    def _pre_backup() -> Path:
        taken.append(tmp_path / "never.tar.zst")
        return taken[-1]

    # `prepared` skips the archive-unpacking phase, which would otherwise raise
    # "no such archive" BEFORE the pre-restore backup and make the assertion
    # below unprovable — verified by mutation, see this module's docstring.
    prepared = PreparedRestore(
        staging_dir=tmp_path / "staging",
        manifest=_manifest(),
        sidecar_ok=None,
    )

    with pytest.raises(BackupError, match="brain restore"):
        restore_backup(
            tmp_path / "absent.tar.zst",
            cfg,
            pre_backup=_pre_backup,
            prepared=prepared,
        )
    assert taken == []


def test_the_vault_only_leg_is_not_blocked_by_the_db_name(tmp_path: Path) -> None:
    """A vault-only restore derives no database name, so the budget must not bind."""
    over = _name_of(MAX_RESTORABLE_DB_NAME_BYTES + 1)
    cfg = Config(
        database_url=f"postgresql://brain:brain@localhost:5434/{over}",
        embedder="fake",
        vault_path=tmp_path / "vault",
        backup_dir=tmp_path / "backups",
        brain_home=tmp_path / "brain-home",
    )
    with pytest.raises(Exception) as excinfo:
        restore_backup(
            tmp_path / "absent.tar.zst",
            cfg,
            db_leg=False,
            pre_backup=lambda: tmp_path / "pre.tar.zst",
        )
    assert "brain restore" not in str(excinfo.value)
