"""Static checks for brain Quartz sidebar layout overrides."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_sidebar_padding_overrides_stock_quartz_spacing() -> None:
    """The brain UI keeps sidebar padding tighter than stock Quartz."""
    text = (
        REPO_ROOT / "quartz_overrides" / "quartz" / "styles" / "brain" / "_sidebar.scss"
    ).read_text(encoding="utf-8")
    assert ".page > #quartz-body .sidebar" in text
    assert "padding: 2rem;" in text
