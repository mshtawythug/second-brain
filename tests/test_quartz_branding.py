"""Static checks for the brain Quartz branding assets."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_quartz_titles_do_not_use_emoji_logo() -> None:
    """The Quartz page title should be text-only; the logo lives in assets."""
    for rel in ("quartz.config.ts", "quartz_overrides/quartz.config.ts"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert 'pageTitle: "Second Brain"' in text
        assert "🧠 Second Brain" not in text


def test_quartz_brand_assets_are_overlayed() -> None:
    """Overlay-managed static assets should include the logo and favicon inputs."""
    static_dir = REPO_ROOT / "quartz_overrides" / "quartz" / "static"
    for name in ("brain-logo-light.png", "brain-logo-dark.png", "icon.png", "favicon.ico"):
        path = static_dir / name
        assert path.is_file(), f"missing branding asset: {path}"
        assert path.stat().st_size > 0, f"empty branding asset: {path}"


def test_page_title_renders_logo_mark() -> None:
    """The sidebar title should render the custom mark next to the text title."""
    page_title = REPO_ROOT / "quartz_overrides" / "quartz" / "components" / "PageTitle.tsx"
    text = page_title.read_text(encoding="utf-8")
    assert "/static/brain-logo-light.png" in text
    assert "/static/brain-logo-dark.png" in text
    assert "saved-theme=\"dark\"" in text
    assert 'alt=""' in text
    assert 'aria-hidden="true"' in text
    assert text.count('width="48"') == 2
    assert text.count('height="48"') == 2
    assert "width: 3rem; height: 3rem;" in text


def test_footer_does_not_use_emoji_logo() -> None:
    """The overlayed footer should keep branding text-only."""
    footer = REPO_ROOT / "quartz_overrides" / "quartz" / "components" / "Footer.tsx"
    text = footer.read_text(encoding="utf-8")
    assert "Second Brain" in text
    assert "🧠" not in text
    assert "<ul>" not in text
    assert "Object.entries" not in text
