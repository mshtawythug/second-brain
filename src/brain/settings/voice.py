"""The voice-memo knobs (roadmap ticket RM-38).

``BRAIN_WHISPER_MODEL`` is the path of a whisper.cpp model file (``ggml-*.bin``). Unset
means voice memos are off: there is no default model and nothing is ever downloaded. It
must be absolute or start with ``~``; whether the file exists is checked where it is used
(:func:`brain.ingest.whisper.resolve_whisper`) and reported by ``brain doctor``.

``BRAIN_WHISPER_MAX_MINUTES`` (default 60) is the longest memo transcribed. A longer one
is skipped, and whisper's timeout is ``2 × duration + 60 s``, so the cap also bounds how
long one memo can hold a run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from brain.config import ConfigError
from brain.settings._env import env_int, env_str

DEFAULT_MAX_MINUTES = 60
#: A bound on the cap itself: ten hours of audio is a recording, not a memo.
MAX_MAX_MINUTES = 600


@dataclass(frozen=True)
class VoiceSettings:
    """Voice memos' configuration. ``model is None`` means voice memos are off."""

    model: Path | None
    max_minutes: int

    @property
    def enabled(self) -> bool:
        return self.model is not None

    @property
    def max_seconds(self) -> float:
        return self.max_minutes * 60.0


def load_voice_settings(env: Mapping[str, str]) -> VoiceSettings:
    """Read the voice knobs from ``env`` (``os.environ`` after ``Config.load()``)."""
    raw = env_str(env, "BRAIN_WHISPER_MODEL", None)
    model = Path(raw).expanduser() if raw is not None else None
    if model is not None and not model.is_absolute():
        raise ConfigError("BRAIN_WHISPER_MODEL must be an absolute path (or start with ~)")
    return VoiceSettings(
        model=model,
        max_minutes=env_int(
            env,
            "BRAIN_WHISPER_MAX_MINUTES",
            DEFAULT_MAX_MINUTES,
            minimum=1,
            maximum=MAX_MAX_MINUTES,
        ),
    )
