"""Parse and load the curated examples behind ``brain examples`` (RM-53).

The resource's grammar is strict so that no ``brain ...`` line can sit where the rot test
(``tests/test_examples_rot.py``) does not look: a stale line this parser did not recognise as an
invocation would never be checked. Outside a fence: blank lines, the one title, and
``## <slug>: <title>``. Inside a ```` ```bash ```` fence: blank lines, ``# comment`` lines, and
single ``brain ...`` invocations with no shell syntax beyond one trailing ``< word``, because the
safety check reads an example as one command and a shell must read it the same way. Anything else
raises.

Printable ASCII only. The lines are meant to be pasted into a shell, so nothing may be invisible
to the reader. Split on ``\\n`` alone, because ``str.splitlines`` would quietly treat ``\\x0b`` or
``\\r`` as a line break instead of refusing them.
"""
from __future__ import annotations

import importlib.resources
import re
from dataclasses import dataclass

from .errors import BrainError

_RESOURCE_PACKAGE = "brain.resources"
_RESOURCE_NAME = "examples.md"
_DOCUMENT_TITLE = "# brain examples"
_FENCE_OPEN = "```bash"
_FENCE_CLOSE = "```"
_INVOCATION_PREFIX = "brain "
_COMMENT_PREFIX = "# "
_HEADING_RE = re.compile(r"## ([a-z][a-z-]*): (\S.*)\Z")
_PRINTABLE_ASCII_RE = re.compile(r"[\x20-\x7e]*\Z")
_SHELL_METACHARACTERS = frozenset(";&|>$`()")


class ExamplesResourceError(BrainError):
    """The packaged examples are missing, unreadable, or not in the expected format."""


@dataclass(frozen=True)
class ExampleTopic:
    """One topic: its slug, its title, and its comment, invocation and blank lines in order."""

    slug: str
    title: str
    lines: tuple[str, ...]

    @property
    def invocations(self) -> tuple[str, ...]:
        """The lines that are ``brain ...`` commands."""
        return tuple(line for line in self.lines if line.startswith(_INVOCATION_PREFIX))


def is_valid_fence_line(line: str) -> bool:
    """Whether ``line`` may sit inside a fence: blank, a comment, or one ``brain`` invocation.

    An invocation may redirect its stdin only, as a trailing ``< word``. A shell would pass
    anything after that word to ``brain``, and it would read ``<word`` or ``<placeholder>`` as a
    redirect, so every other ``<`` is refused.
    """
    if not line or line.startswith(_COMMENT_PREFIX):
        return True
    if not line.startswith(_INVOCATION_PREFIX) or line.endswith("\\"):
        return False
    if _SHELL_METACHARACTERS.intersection(line):
        return False
    words = line.split()
    return all(
        "<" not in word or (word == "<" and index == len(words) - 2)
        for index, word in enumerate(words)
    )


def _finished(topic: ExampleTopic, seen: tuple[ExampleTopic, ...]) -> tuple[ExampleTopic, ...]:
    if not topic.invocations:
        raise ExamplesResourceError(f"topic {topic.slug} has no invocation")
    if any(earlier.slug == topic.slug for earlier in seen):
        raise ExamplesResourceError(f"topic {topic.slug} is repeated")
    return (*seen, topic)


def parse_examples(text: str) -> tuple[ExampleTopic, ...]:
    """The topics in ``text``, in file order. Raises on anything outside the grammar."""
    topics: tuple[ExampleTopic, ...] = ()
    current: ExampleTopic | None = None
    in_fence = False
    for number, line in enumerate(text.split("\n"), 1):
        if not _PRINTABLE_ASCII_RE.match(line):
            raise ExamplesResourceError(f"line {number}: not printable ASCII")
        heading = _HEADING_RE.match(line)
        if in_fence:
            if line == _FENCE_CLOSE:
                in_fence = False
            elif current is not None and is_valid_fence_line(line):
                current = ExampleTopic(current.slug, current.title, (*current.lines, line))
            else:
                raise ExamplesResourceError(f"line {number}: not a comment or an invocation")
        elif line == _FENCE_OPEN and current is not None:
            in_fence = True
        elif heading is not None:
            topics = _finished(current, topics) if current is not None else topics
            current = ExampleTopic(heading.group(1), heading.group(2), ())
        elif line and not (line == _DOCUMENT_TITLE and number == 1):
            raise ExamplesResourceError(f"line {number}: not allowed outside a fence")
    if in_fence or current is None:
        raise ExamplesResourceError("a fence is left open, or there is no topic")
    return _finished(current, topics)


def load_examples() -> tuple[ExampleTopic, ...]:
    """The packaged topics. Raises :class:`ExamplesResourceError` on a broken install."""
    try:
        resource = importlib.resources.files(_RESOURCE_PACKAGE).joinpath(_RESOURCE_NAME)
        text = resource.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError, UnicodeDecodeError) as exc:
        raise ExamplesResourceError("the examples resource could not be read") from exc
    return parse_examples(text)
