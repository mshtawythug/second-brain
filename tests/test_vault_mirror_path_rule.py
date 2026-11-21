"""The ``_ingested/`` mirror path rule as a pure function (F15).

``ingest-code`` / ``ingest-folder`` store an absolute file path as the external id,
and ``ingest-github`` stores ``owner/repo#N``. The old rule put the first eight
characters of that id into the filename unchanged, so the "filename" became stray
nested directories (``_ingested/code/2026-11-14-/srv/rep-…``). These tests pin the
new rule: a path-hostile segment is replaced by a hash, a safe one is kept byte for
byte, and an old-rule file already on disk is reused rather than duplicated.

Every external id and path here is synthetic; every vault root is ``tmp_path``.
"""
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from brain.vault.export import _DocumentForExport, _resolve_relative_path
from brain.vault.mirror_path import external_segment, ingested_relative_path

pytestmark = pytest.mark.nodb

_ABS_ID = "/srv/synthetic/repo/src/a.py"
_DOC_ID = "11111111-2222-3333-4444-555555555555"


@dataclass(frozen=True)
class _Doc:
    id: str = _DOC_ID
    title: str = "sample-repo/src/a.py"
    metadata: Mapping[str, Any] = field(default_factory=lambda: {"date": "2026-01-15"})
    ingested_at: datetime | None = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
    source_kind: str | None = "code"
    source_external_id: str | None = _ABS_ID


def _never_taken(_candidate: str) -> bool:
    return False


def _sha8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _path(doc: _Doc, vault: Path, taken: set[str] | None = None) -> str:
    is_taken = (taken or set()).__contains__
    return ingested_relative_path(doc, is_taken, vault_root=vault)


# --- the bug ---------------------------------------------------------------------------


def test_an_absolute_path_external_id_makes_no_nested_directory(tmp_path: Path) -> None:
    path = _path(_Doc(), tmp_path)

    assert path == f"_ingested/code/2026-01-15-{_sha8(_ABS_ID)}-sample-repo-src-a-py.md"
    assert PurePosixPath(path).parent == PurePosixPath("_ingested/code")


@pytest.mark.parametrize("external_id", ["acme/widgets#7", "acme/widgets:README"])
def test_a_github_style_external_id_makes_no_nested_directory(
    tmp_path: Path, external_id: str
) -> None:
    doc = _Doc(source_kind="github", source_external_id=external_id, title="Widget PR")

    path = _path(doc, tmp_path)

    assert path == f"_ingested/github/2026-01-15-{_sha8(external_id)}-widget-pr.md"


def test_the_gmail_fallback_branch_uses_the_same_segment(tmp_path: Path) -> None:
    doc = _Doc(source_kind="gmail", source_external_id="thread/abc", title="Hello")

    assert _path(doc, tmp_path) == f"_ingested/gmail/2026-01-15-{_sha8('thread/abc')}-hello.md"


# --- zero churn for ids that were already safe -------------------------------------------


@pytest.mark.parametrize(
    ("external_id", "segment"),
    [
        ("aaaabbbb-cccc-dddd-eeee-ffff00001111", "aaaabbbb"),
        ("1234567.890", "1234567."),
        ("x-1234567890", "x1234567"),
        ("abc_def_ghi", "abc_def_"),
        ("evt123@g.example", "evt123@g"),
        ("0123456789abcdef" * 4, "01234567"),
        ("short", "short"),
    ],
)
def test_a_segment_that_was_already_safe_is_kept_byte_for_byte(
    external_id: str, segment: str
) -> None:
    assert external_segment(external_id) == segment


def test_a_doc_with_no_external_id_keeps_the_local_placeholder(tmp_path: Path) -> None:
    doc = _Doc(source_kind="krisp", source_external_id=None, title="Sync")

    assert _path(doc, tmp_path) == "_ingested/krisp/2026-01-15-local-11111111-sync.md"


def test_manual_documents_keep_the_plain_slug(tmp_path: Path) -> None:
    doc = _Doc(source_kind="manual", title="Plain Note")

    assert _path(doc, tmp_path) == "_ingested/manual/plain-note.md"


# --- collisions --------------------------------------------------------------------------


def test_two_paths_that_slugify_alike_get_two_paths_and_no_suffix(tmp_path: Path) -> None:
    first = _Doc(source_external_id="/srv/x/pkg/a-b.py", title="pkg/a-b.py")
    second = _Doc(
        id="99999999-2222-3333-4444-555555555555",
        source_external_id="/srv/x/pkg/a_b.py",
        title="pkg/a_b.py",
    )
    taken: set[str] = set()

    one = _path(first, tmp_path, taken)
    taken.add(one)
    two = _path(second, tmp_path, taken)

    assert one != two
    assert one == f"_ingested/code/2026-01-15-{_sha8('/srv/x/pkg/a-b.py')}-pkg-a-b-py.md"
    assert two == f"_ingested/code/2026-01-15-{_sha8('/srv/x/pkg/a_b.py')}-pkg-a-b-py.md"


def test_the_collision_ladder_still_suffixes_by_document_id(tmp_path: Path) -> None:
    base = f"_ingested/code/2026-01-15-{_sha8(_ABS_ID)}-sample-repo-src-a-py.md"
    short = base.replace(".md", "-11111111.md")

    assert _path(_Doc(), tmp_path, {base}) == short
    assert _path(_Doc(), tmp_path, {base, short}) == base.replace(".md", f"-{_DOC_ID}.md")


# --- determinism -------------------------------------------------------------------------


def test_the_rule_is_a_pure_function_of_its_inputs(tmp_path: Path) -> None:
    first = [_path(_Doc(), tmp_path) for _ in range(5)]
    later = _path(_Doc(), tmp_path)

    assert len(set(first)) == 1
    assert later == first[0]
    assert external_segment(_ABS_ID) == external_segment(_ABS_ID) == _sha8(_ABS_ID)


# --- the vacancy guard: an old-rule file on disk is reused, never duplicated ---------------

_LEGACY = "_ingested/code/2026-01-15-/srv/syn-sample-repo-src-a-py.md"


def test_an_old_rule_file_on_disk_is_reused_exactly(tmp_path: Path) -> None:
    legacy = tmp_path / _LEGACY
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\nid: x\ntitle: x\n---\nbody\n", encoding="utf-8")

    assert _path(_Doc(), tmp_path) == _LEGACY


def test_an_old_rule_symlink_on_disk_is_reused_exactly(tmp_path: Path) -> None:
    legacy = tmp_path / _LEGACY
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(tmp_path / "dangling-target.md")

    assert _path(_Doc(), tmp_path) == _LEGACY


def test_a_vacant_old_rule_location_gets_the_new_path(tmp_path: Path) -> None:
    assert _path(_Doc(), tmp_path) != _LEGACY
    assert not (tmp_path / "_ingested").exists()


def test_a_stored_vault_path_is_returned_without_consulting_the_rule(tmp_path: Path) -> None:
    doc = _DocumentForExport(
        id=_DOC_ID,
        title="sample-repo/src/a.py",
        content="x",
        content_hash="h",
        content_type="code",
        tags=[],
        metadata={"date": "2026-01-15"},
        ingested_at=datetime(2026, 1, 15, tzinfo=UTC),
        kind="ingested",
        vault_path=_LEGACY,
        source_kind="code",
        source_external_id=_ABS_ID,
        draft=False,
        summary=None,
    )

    def boom(_candidate: str) -> bool:
        raise AssertionError("the collision oracle must not be consulted")

    assert _resolve_relative_path(doc, boom, vault_root=tmp_path / "missing") == _LEGACY


# --- hostile external ids stay inside the vault ------------------------------------------

_HOSTILE_IDS = [
    "../../etc/x",
    "/abs/path",
    "..",
    ".",
    "...",
    ".hidden",
    "a\x00b",
    "x" * 5000,
    "../" * 2000,
    "unicodé/名前",
    "名前名前名前名前名前",
    "back\\slash\\x",
    "with space",
    "tab\there",
    "colon:id",
    "",
    "--------",
]


@pytest.mark.parametrize("external_id", _HOSTILE_IDS)
def test_a_hostile_external_id_stays_inside_the_vault(tmp_path: Path, external_id: str) -> None:
    doc = _Doc(source_external_id=external_id or None)

    path = _path(doc, tmp_path)

    pure = PurePosixPath(path)
    assert not pure.is_absolute()
    assert ".." not in pure.parts
    assert pure.parent == PurePosixPath("_ingested/code")
    assert not pure.name.startswith(".")
    assert "\x00" not in path
    resolved = (tmp_path / path).resolve()
    assert resolved.is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("external_id", [i for i in _HOSTILE_IDS if i])
def test_no_segment_is_a_dot_name_or_holds_a_separator(external_id: str) -> None:
    segment = external_segment(external_id)

    assert segment[0] != "."
    assert segment not in {".", ".."}
    assert "/" not in segment and "\\" not in segment and "\x00" not in segment


@pytest.mark.parametrize("external_id", ["/srv/x", "..", ".", ".hidden", "a\x00b", "é"])
def test_an_unsafe_segment_is_the_sha256_prefix(external_id: str) -> None:
    assert external_segment(external_id) == hashlib.sha256(
        external_id.encode("utf-8")
    ).hexdigest()[:8]


@pytest.mark.parametrize("date_value", ["../../../x", "2026/01/15", ".hidden-date", "a\x00b"])
def test_a_hostile_metadata_date_falls_back_to_the_ingest_date(
    tmp_path: Path, date_value: str
) -> None:
    doc = replace(_Doc(), metadata={"date": date_value})

    path = _path(doc, tmp_path)

    assert path == f"_ingested/code/2026-01-15-{_sha8(_ABS_ID)}-sample-repo-src-a-py.md"


def test_an_escaping_old_rule_path_is_never_reused(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    # The source directory must exist, or the kernel cannot walk the ``..`` hops and
    # the probe would miss the outside file for a reason unrelated to the guard.
    (vault / "_ingested" / "code").mkdir(parents=True)
    # The old rule would have produced ``_ingested/code/../../../x-<seg>-<slug>.md``,
    # which resolves outside the vault. Put a file there: it must not be adopted.
    doc = replace(_Doc(), metadata={"date": "../../../x"})
    outside = tmp_path / "x-/srv/syn-sample-repo-src-a-py.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("not ours\n", encoding="utf-8")

    escaping = "_ingested/code/../../../x-/srv/syn-sample-repo-src-a-py.md"
    assert (vault / escaping).is_file()  # the probe really can see the outside file

    path = _path(doc, vault)

    assert path != escaping
    assert (vault / path).resolve().is_relative_to(vault.resolve())
