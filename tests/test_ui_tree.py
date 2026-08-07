"""The pure vault-tree fold."""
from __future__ import annotations

from datetime import UTC, datetime

from brain.ui.tree import build_tree


def _row(doc_id: str, title: str, path: str, *, kind: str = "vault", draft: bool = False):
    return (doc_id, title, path, kind, draft, datetime(2026, 7, 26, tzinfo=UTC))


def test_flat_paths_nest_into_folders() -> None:
    root = build_tree(
        [
            _row("1", "Q3 Planning Sync", "projects/q3.md"),
            _row("2", "Ingest Rewrite Design", "projects/design/ingest.md"),
        ]
    )
    payload = root.to_payload()
    assert [c["name"] for c in payload["children"]] == ["projects"]
    projects = payload["children"][0]
    assert [c["name"] for c in projects["children"]] == ["design"]
    assert [n["title"] for n in projects["notes"]] == ["Q3 Planning Sync"]


def test_vault_root_files_are_not_dropped() -> None:
    root = build_tree([_row("1", "Scratch", "scratch.md")])
    assert [n["title"] for n in root.to_payload()["notes"]] == ["Scratch"]


def test_empty_input_yields_an_empty_root() -> None:
    payload = build_tree([]).to_payload()
    assert payload["children"] == []
    assert payload["notes"] == []


def test_rows_without_a_vault_path_are_skipped() -> None:
    """One unexported row must never blank the entire rail."""
    root = build_tree([_row("1", "Ok", "a.md"), _row("2", "Bad", "")])
    assert len(root.to_payload()["notes"]) == 1


def test_identically_named_folders_at_different_depths_do_not_collide() -> None:
    root = build_tree(
        [
            _row("1", "A", "projects/design/a.md"),
            _row("2", "B", "archive/design/b.md"),
        ]
    )
    payload = root.to_payload()
    names = {c["name"] for c in payload["children"]}
    assert names == {"projects", "archive"}
    for child in payload["children"]:
        assert [g["name"] for g in child["children"]] == ["design"]
        assert len(child["children"][0]["notes"]) == 1


def test_ingested_renders_as_a_normal_branch() -> None:
    root = build_tree([_row("1", "Mail", "_ingested/gmail/x.md", kind="ingested")])
    branch = root.to_payload()["children"][0]
    assert branch["name"] == "_ingested"
    assert branch["children"][0]["notes"][0]["tier"] == "ingested"


def test_draft_flag_surfaces_on_the_leaf() -> None:
    root = build_tree([_row("1", "WIP", "a.md", draft=True)])
    assert root.to_payload()["notes"][0]["draft"] is True


def test_ordering_is_stable_and_case_insensitive() -> None:
    """Ordering must not depend on SQL row order."""
    root = build_tree(
        [
            _row("1", "zebra", "b/z.md"),
            _row("2", "Apple", "b/a.md"),
            _row("3", "x", "Beta/x.md"),
        ]
    )
    payload = root.to_payload()
    assert [c["name"] for c in payload["children"]] == ["b", "Beta"]
    assert [n["title"] for n in payload["children"][0]["notes"]] == ["Apple", "zebra"]


def test_folder_paths_are_cumulative() -> None:
    root = build_tree([_row("1", "A", "one/two/three/a.md")])
    node = root.to_payload()["children"][0]
    assert node["path"] == "one"
    assert node["children"][0]["path"] == "one/two"
    assert node["children"][0]["children"][0]["path"] == "one/two/three"


def test_dates_serialize_as_iso8601() -> None:
    root = build_tree([_row("1", "A", "a.md")])
    assert root.to_payload()["notes"][0]["date"].startswith("2026-07-26T")


def test_null_date_is_none_not_a_crash() -> None:
    root = build_tree([("1", "A", "a.md", "vault", False, None)])
    assert root.to_payload()["notes"][0]["date"] is None
