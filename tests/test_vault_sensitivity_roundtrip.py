"""The F6 publish boundary: sensitivity round-trips DB <-> vault frontmatter.

This is the half of F6 that actually stops a confidential note leaving the
machine. The other egress point — the hosted-embedder veto — is covered by
``tests/test_sensitivity_egress.py``. Between them they carry F6's entire
security value, because plan decision Q7 deliberately declines to filter
``hybrid_search`` *on the grounds that the local CLI is inside the trust
boundary*. That reasoning only holds if the boundaries that ARE outside it are
enforced.

Three properties are pinned here:

1. **Export emits ``sensitivity:`` only when non-normal**, exactly as ``draft``
   is emitted only when true. That is what makes migration 026 zero-churn for
   the existing mirror files — all of them are ``normal``, so none is rewritten.
2. **The round-trip works in both directions** — DB to file, and file back to
   DB — with an invalid hand-typed value coerced rather than raised.
3. **The two frontmatter registries agree.** A key in export's strip set but not
   in sync's ``reserved`` set is silently deleted from the user's file on the
   next export. That is not hypothetical: it is the documented ``summary``
   regression at ``vault/export.py:44-57``.

All notes and documents are synthetic.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.sensitivity import CONFIDENTIAL, DEFAULT_SENSITIVITY
from brain.vault.export import (
    _EXPORT_OWNED_FRONTMATTER_KEYS,
    regenerate_vault_file,
)
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.vault.sync import sync_one_file
from tests.conftest import FakeEmbedder

_BODY = (
    "Quarterly planning notes. The release workflow and the documentation "
    "backlog were reviewed, and owners were assigned for the follow-ups.\n"
)


def _seed_ingested(
    conn: psycopg.Connection[Any], *, title: str, level: str, vault: Path
) -> str:
    """Ingest one synthetic ingested-tier doc and mirror it into ``vault``."""
    result = ingest_document(
        conn,
        embedder=FakeEmbedder(),
        doc=ExtractedDoc(
            title=title,
            content=f"{title}\n\n{_BODY}",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=title,
        sensitivity=level,
        vault_root=vault,
    )
    assert result.document_id is not None
    return result.document_id


def _mirror_path(conn: psycopg.Connection[Any], doc_id: str, vault: Path) -> Path:
    row = conn.execute(
        "SELECT vault_path FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None and row[0], "the doc must have been mirrored"
    return vault / str(row[0])


# --------------------------------------------------------------------------
# Registry parity — the standing invariant
# --------------------------------------------------------------------------


def _sync_reserved_keys() -> set[str]:
    """Extract sync's ``reserved`` literal set from ``_build_metadata``'s source.

    Read from source rather than imported because ``reserved`` is a local
    inside the function. Coupling the test to the literal is deliberate: the
    whole point is to fail when someone edits one registry and not the other,
    which a dynamically-derived value could not detect.
    """
    import ast
    import inspect

    from brain.vault import sync as sync_mod

    tree = ast.parse(inspect.getsource(sync_mod._build_metadata).lstrip())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "reserved" for t in node.targets
            )
            and isinstance(node.value, ast.Set)
        ):
            return {
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
    raise AssertionError(
        "could not find the `reserved = {...}` set literal in "
        "vault.sync._build_metadata — the parity guard cannot run"
    )


def test_export_owned_and_sync_reserved_are_the_same_set() -> None:
    """THE INVARIANT: the two frontmatter registries are the SAME set.

    Generalized guard against the ``summary`` regression class documented at
    ``vault/export.py:44-57``. The dangerous direction is export-owned-but-not-
    sync-reserved: export strips any key in its own set from the freeform
    passthrough, assuming the value is canonical in a typed column. If sync does
    not also reserve it, sync writes the user's value into
    ``documents.metadata`` instead of a column, export strips it on the next
    pass, and the line disappears from the user's file with no error.

    Asserted as **equality**, not a subset, so BOTH directions are caught. The
    reverse (sync-reserved but not export-owned) is less destructive but still
    wrong: sync drops the key from metadata and export never re-emits it, so
    the value is silently lost on the round trip. The failure message names
    which side is missing what, because a bare "sets differ" tells whoever trips
    it nothing about which registry to edit.

    This test is the durable artifact of the whole feature: it converts
    "remember to update both registries" from a discipline into a caught error.
    """
    export_owned = set(_EXPORT_OWNED_FRONTMATTER_KEYS)
    sync_reserved = _sync_reserved_keys()

    only_export = sorted(export_owned - sync_reserved)
    only_sync = sorted(sync_reserved - export_owned)

    assert export_owned == sync_reserved, (
        "the two frontmatter registries have diverged.\n"
        f"  export-owned but NOT sync-reserved: {only_export or 'none'}\n"
        "    -> sync writes these into documents.metadata, export strips them "
        "from the freeform passthrough, and the line DISAPPEARS from the "
        "user's file on the next export. This is the summary regression class "
        "(vault/export.py:44-57) and it destroys user content silently.\n"
        f"  sync-reserved but NOT export-owned: {only_sync or 'none'}\n"
        "    -> sync drops these from metadata but export never re-emits them, "
        "so the value is lost on the round trip.\n"
        "Add the missing key to the other registry."
    )


def test_sensitivity_is_in_both_registries() -> None:
    """The specific pairing this wave added, asserted by name.

    The generalized test above would also catch a one-sided addition, but this
    one names the key so a failure points straight at F6 rather than at an
    abstract invariant.
    """
    assert "sensitivity" in _EXPORT_OWNED_FRONTMATTER_KEYS
    assert "sensitivity" in _sync_reserved_keys()


# --------------------------------------------------------------------------
# Export direction: DB -> file
# --------------------------------------------------------------------------


def test_normal_doc_mirror_has_no_sensitivity_line(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """ZERO CHURN: a normal document's mirror gains no ``sensitivity:`` key.

    Every one of the ~1,376 existing mirror files is ``normal``, so if this
    regressed, migration 026 would rewrite the entire vault on the next export —
    a huge diff, a churned git history, and a watcher storm, all for a field
    that carries no information in the default case.
    """
    vault = tmp_path / "vault"
    doc_id = _seed_ingested(
        test_db, title="Synthetic normal mirror", level=DEFAULT_SENSITIVITY, vault=vault
    )

    text = _mirror_path(test_db, doc_id, vault).read_text()
    fm, _ = parse_frontmatter(text)

    assert "sensitivity" not in fm, (
        "a normal document must produce a byte-identical mirror to pre-026"
    )


def test_confidential_doc_mirror_carries_the_tier(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """A confidential document's mirror states the tier for the Quartz emitter.

    This frontmatter key is precisely what ``contentIndex.ts`` reads to drop the
    document from every published index, so it is the load-bearing output of the
    export half of the boundary.
    """
    vault = tmp_path / "vault"
    doc_id = _seed_ingested(
        test_db, title="Synthetic secret mirror", level=CONFIDENTIAL, vault=vault
    )

    fm, _ = parse_frontmatter(_mirror_path(test_db, doc_id, vault).read_text())

    assert fm.get("sensitivity") == CONFIDENTIAL


def test_regenerate_picks_up_a_tier_change(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """Flipping the column and regenerating stamps the mirror.

    This is the path ``brain mark-confidential`` depends on: it issues a direct
    UPDATE and then calls ``regenerate_vault_file(..., force=True)``. If the
    mirror did not pick the change up, a document would be confidential in the
    database and still fully indexed on the published wiki.
    """
    vault = tmp_path / "vault"
    doc_id = _seed_ingested(
        test_db, title="Synthetic flip mirror", level=DEFAULT_SENSITIVITY, vault=vault
    )
    path = _mirror_path(test_db, doc_id, vault)
    assert "sensitivity" not in parse_frontmatter(path.read_text())[0]

    test_db.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s", (CONFIDENTIAL, doc_id)
    )
    regenerate_vault_file(test_db, doc_id, vault_path=vault, force=True)

    fm, _ = parse_frontmatter(path.read_text())
    assert fm.get("sensitivity") == CONFIDENTIAL


# --------------------------------------------------------------------------
# Sync direction: file -> DB
# --------------------------------------------------------------------------


def _write_vault_note(
    vault: Path, *, relative: str, doc_id: str | None, fields: dict[str, Any]
) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    header = dict(fields)
    if doc_id is not None:
        header["id"] = doc_id
    path.write_text(dump_frontmatter(header, _BODY))
    return path


def test_vault_note_sensitivity_reaches_the_column(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """A hand-authored ``sensitivity: confidential`` lands in the typed column."""
    vault = tmp_path / "vault"
    note = _write_vault_note(
        vault,
        relative="notes/secret.md",
        doc_id=None,
        fields={"title": "Synthetic authored secret", "sensitivity": CONFIDENTIAL},
    )

    report = sync_one_file(
        test_db, embedder=FakeEmbedder(), vault_path=vault, file_path=note
    )

    assert report.errors == []
    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE vault_path = %s",
        ("notes/secret.md",),
    ).fetchone()
    assert row is not None and row[0] == CONFIDENTIAL


def test_sensitivity_does_not_leak_into_metadata(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """``sensitivity`` is reserved, so it never shadows the column in JSONB.

    A copy in ``documents.metadata`` would be a second source of truth for a
    security-relevant value, and — because export strips export-owned keys from
    the freeform merge — the JSONB copy would be the one that silently rots.
    """
    vault = tmp_path / "vault"
    note = _write_vault_note(
        vault,
        relative="notes/reserved.md",
        doc_id=None,
        fields={"title": "Synthetic reserved key", "sensitivity": CONFIDENTIAL},
    )

    sync_one_file(
        test_db, embedder=FakeEmbedder(), vault_path=vault, file_path=note
    )

    row = test_db.execute(
        "SELECT metadata FROM documents WHERE vault_path = %s",
        ("notes/reserved.md",),
    ).fetchone()
    assert row is not None
    assert "sensitivity" not in dict(row[0] or {})


def test_editing_only_the_sensitivity_line_is_detected(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """A frontmatter-only tier edit still triggers an update.

    The body hash is unchanged and every other field is equal, so without
    ``sensitivity_changed`` feeding ``user_visible_change`` the whole update
    would be skipped and the tier the user just set would never reach the
    column — failing silently, with the file on disk saying one thing and the
    database another.
    """
    vault = tmp_path / "vault"
    note = _write_vault_note(
        vault,
        relative="notes/edit.md",
        doc_id=None,
        fields={"title": "Synthetic tier edit"},
    )
    sync_one_file(
        test_db, embedder=FakeEmbedder(), vault_path=vault, file_path=note
    )
    doc_id = parse_frontmatter(note.read_text())[0]["id"]

    # Act — change ONLY the sensitivity line; body and title stay identical.
    note.write_text(
        dump_frontmatter(
            {
                "id": doc_id,
                "title": "Synthetic tier edit",
                "sensitivity": CONFIDENTIAL,
            },
            _BODY,
        )
    )
    report = sync_one_file(
        test_db, embedder=FakeEmbedder(), vault_path=vault, file_path=note
    )

    assert report.updated == 1, report
    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None and row[0] == CONFIDENTIAL


def test_removing_the_line_returns_a_vault_note_to_normal(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """For a VAULT-tier note the file is authoritative in both directions.

    Deleting the line is an explicit edit of the user's own note, so it is a
    sanctioned downgrade. Contrast the ingested-tier behaviour below, where the
    file is generated and a stale mirror must never downgrade anything.
    """
    vault = tmp_path / "vault"
    note = _write_vault_note(
        vault,
        relative="notes/revert.md",
        doc_id=None,
        fields={"title": "Synthetic revert", "sensitivity": CONFIDENTIAL},
    )
    sync_one_file(
        test_db, embedder=FakeEmbedder(), vault_path=vault, file_path=note
    )
    doc_id = parse_frontmatter(note.read_text())[0]["id"]

    note.write_text(
        dump_frontmatter({"id": doc_id, "title": "Synthetic revert"}, _BODY)
    )
    sync_one_file(
        test_db, embedder=FakeEmbedder(), vault_path=vault, file_path=note
    )

    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None and row[0] == DEFAULT_SENSITIVITY


def test_invalid_value_is_coerced_and_warned_not_raised(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    caplog: Any,
) -> None:
    """A typo'd tier coerces to ``normal``, logs WARNING, and does NOT abort.

    One hand-typed mistake must not kill a ``vault sync --watch`` pass over the
    whole corpus. Coercion is one-way by construction — it can only ever land on
    ``normal`` — so a typo costs protection the user intended but can never
    fabricate protection they did not.
    """
    vault = tmp_path / "vault"
    note = _write_vault_note(
        vault,
        relative="notes/typo.md",
        doc_id=None,
        fields={"title": "Synthetic typo tier", "sensitivity": "seCRET"},
    )

    with caplog.at_level(logging.WARNING, logger="brain.vault.sync"):
        report = sync_one_file(
            test_db, embedder=FakeEmbedder(), vault_path=vault, file_path=note
        )

    assert report.errors == [], "an invalid tier must not become a sync error"
    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE vault_path = %s",
        ("notes/typo.md",),
    ).fetchone()
    assert row is not None and row[0] == DEFAULT_SENSITIVITY
    # ``getMessage()`` interpolates the lazy %-args the logger was called with;
    # ``record.message`` is only populated once a formatter has run, and
    # hand-rolling ``r.message % r.args`` mis-parses under ``if/else``.
    rendered = [r.getMessage() for r in caplog.records]
    assert any(
        "invalid sensitivity" in msg and "seCRET" in msg for msg in rendered
    ), f"expected a WARNING naming the bad value; got {rendered}"


def test_stale_ingested_mirror_cannot_downgrade_the_column(
    test_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """REGRESSION: an ingested-tier mirror is NOT authoritative for the tier.

    ``_ingested/`` files are generated, and a mirror can legitimately be stale —
    written before the tier changed, restored from a backup, or hand-copied.
    If sync honoured it, a routine ``brain vault sync --watch`` pass would
    silently downgrade a confidential document, and the next ingest under a
    hosted embedder would ship the body off-machine. Same egress hole the
    escalate-only rule closes on the re-ingest path, reached by a different
    route.
    """
    vault = tmp_path / "vault"
    doc_id = _seed_ingested(
        test_db, title="Synthetic stale mirror", level=CONFIDENTIAL, vault=vault
    )
    path = _mirror_path(test_db, doc_id, vault)

    # Simulate a stale mirror: strip the sensitivity line off the generated file.
    fm, body = parse_frontmatter(path.read_text())
    assert fm.pop("sensitivity", None) == CONFIDENTIAL
    path.write_text(dump_frontmatter(fm, body))

    sync_one_file(
        test_db, embedder=FakeEmbedder(), vault_path=vault, file_path=path
    )

    row = test_db.execute(
        "SELECT sensitivity FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None and row[0] == CONFIDENTIAL, (
        "a stale ingested-tier mirror must never downgrade the stored tier"
    )
