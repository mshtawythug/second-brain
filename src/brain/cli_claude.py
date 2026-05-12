"""brain claude install-skill — install the Claude Code skill into ~/.claude/skills/brain/."""
import sys
from importlib.resources import files as resource_files
from pathlib import Path

import typer

from .errors import BrainError


class SkillInstallError(BrainError):
    """Raised when the Claude skill install can't proceed."""


_DEFAULT_TARGET_ROOT = Path.home() / ".claude" / "skills"
_SKILL_DIR_NAME = "brain"
_SKILL_FILENAME = "SKILL.md"


def install_skill(
    target_root: Path | None = None,
    force: bool = False,
    uninstall: bool = False,
) -> None:
    """Install (or uninstall) the brain Claude Code skill."""
    root = target_root if target_root is not None else _DEFAULT_TARGET_ROOT
    skill_dir = root / _SKILL_DIR_NAME
    target = skill_dir / _SKILL_FILENAME

    if uninstall:
        _uninstall(skill_dir, target)
        return

    src_bytes = _read_skill_template()
    _install(target, skill_dir, src_bytes, force=force)


def _read_skill_template() -> bytes:
    """Read SKILL.md from package data."""
    res = resource_files("brain.templates.skill") / _SKILL_FILENAME
    return res.read_bytes()


def _install(target: Path, skill_dir: Path, src_bytes: bytes, *, force: bool) -> None:
    """Write src_bytes to target, creating skill_dir as needed."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if target.read_bytes() == src_bytes:
            print(f"skill up to date: {target}")
            return
        if not force:
            typer.confirm(
                f"{target} differs from the packaged SKILL.md. Overwrite?",
                abort=True,
            )
    target.write_bytes(src_bytes)
    print(f"skill installed: {target}")


def _uninstall(skill_dir: Path, target: Path) -> None:
    """Remove SKILL.md and rmdir the brain/ dir if empty."""
    target_removed = False
    if target.is_file():
        target.unlink()
        print(f"removed {target}")
        target_removed = True

    if skill_dir.is_dir():
        remaining = list(skill_dir.iterdir())
        if remaining:
            print(
                f"warning: {skill_dir} not empty — leaving it in place "
                f"({len(remaining)} other entries)",
                file=sys.stderr,
            )
        else:
            skill_dir.rmdir()
            print(f"removed {skill_dir}")

    if not target_removed and not skill_dir.is_dir():
        print("nothing to uninstall")
