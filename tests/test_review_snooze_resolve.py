"""``brain review snooze`` / ``resolve`` — the missing status writers.

``elicitation_gaps`` has carried ``'snoozed'`` / ``'resolved'`` in its status
CHECK and a ``snoozed_until`` column since migration 017, and
``review/queries.list_review_queue`` has always *read* both — but until now
nothing in the review surface ever *wrote* them. These tests pin the two
writers and, more importantly, the round trip: a snoozed finding must vanish
from the open queue and come back on its own once the snooze expires.

Real test DB via the ``test_db`` fixture; no Ollama, no graph, no mocks. Rows
are seeded with plain parameterized INSERTs rather than through the scanners so
each test states its own preconditions.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from typer.testing import CliRunner

from brain import cli
from brain.review.queries import (
    list_review_queue,
    resolve_review_finding,
    snooze_review_finding,
)

runner = CliRunner()

_TENANT = "default"
_KINDS = ("contradiction", "stale")


def _seed_finding(
    conn: psycopg.Connection,
    *,
    signal_kind: str = "stale",
    target_id: str = "topic-alpha",
    score: float = 0.9,
    status: str = "surfaced",
    snoozed_until: datetime | None = None,
    tenant_id: str = _TENANT,
    finding_id: str | None = None,
) -> str:
    """Insert one ``elicitation_gaps`` row and return its full id.

    ``finding_id`` pins the UUID; the ambiguity test needs two rows that share a
    leading prefix, which random ``gen_random_uuid()`` values almost never do.
    """
    row = conn.execute(
        """
        INSERT INTO elicitation_gaps
            (id, tenant_id, signal_kind, target_type, target_id, score,
             rationale, status, snoozed_until)
        VALUES (COALESCE(%s::uuid, gen_random_uuid()),
                %s, %s, 'topic', %s, %s, 'synthetic finding', %s, %s)
        RETURNING id::text
        """,
        (
            finding_id,
            tenant_id,
            signal_kind,
            target_id,
            score,
            status,
            snoozed_until,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _status_and_until(
    conn: psycopg.Connection, finding_id: str
) -> tuple[str, datetime | None]:
    row = conn.execute(
        "SELECT status, snoozed_until FROM elicitation_gaps WHERE id = %s::uuid",
        (finding_id,),
    ).fetchone()
    assert row is not None, f"finding {finding_id} disappeared"
    return str(row[0]), row[1]


def _open_ids(conn: psycopg.Connection) -> list[str]:
    return [
        r.id
        for r in list_review_queue(
            conn, tenant_id=_TENANT, signal_kinds=_KINDS, limit=50
        )
    ]


# ---------------------------------------------------------------------------
# review/queries.py — the two writers
# ---------------------------------------------------------------------------


def test_snooze_sets_status_and_until(test_db: psycopg.Connection) -> None:
    finding_id = _seed_finding(test_db)

    returned = snooze_review_finding(
        test_db, tenant_id=_TENANT, id_prefix=finding_id[:8], days=3
    )

    assert returned == finding_id
    status, until = _status_and_until(test_db, finding_id)
    assert status == "snoozed"
    assert until is not None
    # ~3 days out; a generous window keeps the assertion clock-skew proof.
    delta = until - datetime.now(UTC)
    assert timedelta(days=2, hours=23) < delta < timedelta(days=3, hours=1)


def test_snoozed_finding_is_hidden_until_expiry(test_db: psycopg.Connection) -> None:
    finding_id = _seed_finding(test_db)
    assert _open_ids(test_db) == [finding_id]

    snooze_review_finding(test_db, tenant_id=_TENANT, id_prefix=finding_id, days=7)
    assert _open_ids(test_db) == [], "a live snooze must hide the finding"

    # Expire the snooze in place — the reader gates on `snoozed_until < now()`.
    test_db.execute(
        "UPDATE elicitation_gaps SET snoozed_until = now() - interval '1 minute' "
        "WHERE id = %s::uuid",
        (finding_id,),
    )
    assert _open_ids(test_db) == [finding_id], "an expired snooze must re-surface"


def test_resolve_sets_status_resolved(test_db: psycopg.Connection) -> None:
    finding_id = _seed_finding(test_db)

    returned = resolve_review_finding(
        test_db, tenant_id=_TENANT, id_prefix=finding_id[:8]
    )

    assert returned == finding_id
    status, _ = _status_and_until(test_db, finding_id)
    assert status == "resolved"
    assert _open_ids(test_db) == []


def test_resolve_is_idempotent(test_db: psycopg.Connection) -> None:
    """Re-resolving must not raise — the prefix still resolves once resolved."""
    finding_id = _seed_finding(test_db)

    resolve_review_finding(test_db, tenant_id=_TENANT, id_prefix=finding_id)
    resolve_review_finding(test_db, tenant_id=_TENANT, id_prefix=finding_id)

    status, _ = _status_and_until(test_db, finding_id)
    assert status == "resolved"


def test_snooze_refuses_a_resolved_finding(test_db: psycopg.Connection) -> None:
    """A resolved finding is closed; snoozing it would resurrect it."""
    finding_id = _seed_finding(test_db, status="resolved")

    with pytest.raises(ValueError, match="no review finding"):
        snooze_review_finding(test_db, tenant_id=_TENANT, id_prefix=finding_id, days=1)


def test_writers_reject_an_unknown_prefix(test_db: psycopg.Connection) -> None:
    _seed_finding(test_db)

    with pytest.raises(ValueError, match="no review finding"):
        snooze_review_finding(test_db, tenant_id=_TENANT, id_prefix="ffffffff", days=1)
    with pytest.raises(ValueError, match="no review finding"):
        resolve_review_finding(test_db, tenant_id=_TENANT, id_prefix="ffffffff")


def test_writers_reject_an_ambiguous_prefix(test_db: psycopg.Connection) -> None:
    """Two findings under one prefix must fail loudly, never pick one."""
    shared = "abcdef12"
    _seed_finding(
        test_db, target_id="topic-alpha", finding_id=f"{shared}-0000-4000-8000-000000000001"
    )
    _seed_finding(
        test_db,
        signal_kind="contradiction",
        target_id="topic-beta",
        finding_id=f"{shared}-0000-4000-8000-000000000002",
    )

    with pytest.raises(ValueError, match="ambiguous"):
        snooze_review_finding(test_db, tenant_id=_TENANT, id_prefix=shared, days=1)
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_review_finding(test_db, tenant_id=_TENANT, id_prefix=shared)


def test_writers_are_tenant_scoped(test_db: psycopg.Connection) -> None:
    finding_id = _seed_finding(test_db, tenant_id="other")

    with pytest.raises(ValueError, match="no review finding"):
        resolve_review_finding(test_db, tenant_id=_TENANT, id_prefix=finding_id)


def test_snooze_rejects_a_non_positive_day_count(test_db: psycopg.Connection) -> None:
    finding_id = _seed_finding(test_db)

    with pytest.raises(ValueError, match="days must be >= 1"):
        snooze_review_finding(test_db, tenant_id=_TENANT, id_prefix=finding_id, days=0)
    # The row must be untouched — validation happens before any UPDATE.
    assert _status_and_until(test_db, finding_id)[0] == "surfaced"


def test_elicit_owned_kinds_are_not_reachable(test_db: psycopg.Connection) -> None:
    """``brain review`` only ever writes its own two signal kinds."""
    finding_id = _seed_finding(test_db, signal_kind="orphan")

    with pytest.raises(ValueError, match="no review finding"):
        resolve_review_finding(test_db, tenant_id=_TENANT, id_prefix=finding_id)


# ---------------------------------------------------------------------------
# cli_review_extra.py — the two commands
# ---------------------------------------------------------------------------


def test_cli_snooze_confirms_and_hides(test_db: psycopg.Connection) -> None:
    finding_id = _seed_finding(test_db)

    result = runner.invoke(cli.app, ["review", "snooze", finding_id[:8], "--days", "3"])

    assert result.exit_code == 0, result.stdout
    assert finding_id[:8] in result.stdout
    assert "3 day" in result.stdout
    assert _status_and_until(test_db, finding_id)[0] == "snoozed"
    assert _open_ids(test_db) == []


def test_cli_snooze_defaults_to_seven_days(test_db: psycopg.Connection) -> None:
    finding_id = _seed_finding(test_db)

    result = runner.invoke(cli.app, ["review", "snooze", finding_id[:8]])

    assert result.exit_code == 0, result.stdout
    assert "7 day" in result.stdout
    _, until = _status_and_until(test_db, finding_id)
    assert until is not None
    delta = until - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


def test_cli_resolve_confirms(test_db: psycopg.Connection) -> None:
    finding_id = _seed_finding(test_db)

    result = runner.invoke(cli.app, ["review", "resolve", finding_id[:8]])

    assert result.exit_code == 0, result.stdout
    assert finding_id[:8] in result.stdout
    assert _status_and_until(test_db, finding_id)[0] == "resolved"


def test_cli_snooze_unknown_prefix_exits_nonzero(test_db: psycopg.Connection) -> None:
    result = runner.invoke(cli.app, ["review", "snooze", "ffffffff"])

    assert result.exit_code != 0
    assert "no review finding" in result.output


def test_cli_resolve_unknown_prefix_exits_nonzero(test_db: psycopg.Connection) -> None:
    result = runner.invoke(cli.app, ["review", "resolve", "ffffffff"])

    assert result.exit_code != 0
    assert "no review finding" in result.output


def test_cli_snooze_rejects_zero_days(test_db: psycopg.Connection) -> None:
    finding_id = _seed_finding(test_db)

    result = runner.invoke(cli.app, ["review", "snooze", finding_id, "--days", "0"])

    assert result.exit_code != 0
    assert _status_and_until(test_db, finding_id)[0] == "surfaced"


def test_review_help_lists_both_commands() -> None:
    result = runner.invoke(cli.app, ["review", "--help"])

    assert result.exit_code == 0, result.stdout
    assert "snooze" in result.stdout
    assert "resolve" in result.stdout
