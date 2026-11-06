"""RM-53: the `brain examples` resource format and the packaged resource's content."""
from __future__ import annotations

from unittest import mock

import pytest

from brain.examples import (
    ExamplesResourceError,
    ExampleTopic,
    load_examples,
    parse_examples,
)

pytestmark = pytest.mark.nodb

_TOPICS = ("search", "ingest", "graph", "brief-review", "maintenance")


def _doc(*fence_lines: str) -> str:
    """A well-formed document, one topic, with ``fence_lines`` inside its fence."""
    body = "\n".join(fence_lines)
    return f"# brain examples\n\n## search: Find things\n\n```bash\n{body}\n```\n"


def test_a_well_formed_document_parses_in_file_order() -> None:
    # Arrange
    text = (
        "# brain examples\n\n"
        "## second: Second title\n\n```bash\n# Why\nbrain status\n\nbrain doctor\n```\n\n"
        "## first: First title\n\n```bash\nbrain list\n```\n"
    )

    # Act
    topics = parse_examples(text)

    # Assert
    assert topics == (
        ExampleTopic(
            slug="second",
            title="Second title",
            lines=("# Why", "brain status", "", "brain doctor"),
        ),
        ExampleTopic(slug="first", title="First title", lines=("brain list",)),
    )
    assert topics[0].invocations == ("brain status", "brain doctor")


@pytest.mark.parametrize(
    "line",
    [
        'brain search "x"',
        "# a comment",
        "",
        "brain ingest-stdin --source slack --external-id t-1 --title T < thread.txt",
    ],
)
def test_ordinary_fence_lines_are_kept(line: str) -> None:
    topics = parse_examples(_doc("brain status", line))
    assert topics[0].lines == ("brain status", line)


@pytest.mark.parametrize(
    "line",
    [
        "brian search x",
        "  brain search x",
        "echo x | brain ingest-stdin",
        "brain search x \\",
        "#no space after the hash",
        "brain",
        "brain show <id-prefix>",
        "brain search x\x1b]0;title\x07",
        "brain search x\x0b",
        "brain search x\r",
        "brain search caf\u00e9",
        "brain search \u202ex",
    ],
)
def test_a_line_the_rot_test_could_not_see_is_refused(line: str) -> None:
    with pytest.raises(ExamplesResourceError):
        parse_examples(_doc("brain status", line))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# brain examples\n",
        "# brain examples\n\nsome prose\n\n## a: A\n\n```bash\nbrain status\n```\n",
        "# brain examples\n\nbrain status\n\n## a: A\n\n```bash\nbrain status\n```\n",
        "# brain examples\n\n## a: A\n\nbrain status\n\n```bash\nbrain status\n```\n",
        "# brain examples\n\n```bash\nbrain status\n```\n",
        "# brain examples\n\n## a: A\n\n```bash\nbrain status\n",
        "# brain examples\n\n## a: A\n\n```bash\n# only a comment\n```\n",
        "# brain examples\n\n## a: A\n\n## b: B\n\n```bash\nbrain status\n```\n",
        "# brain examples\n\n## a: A\n\n```bash\nbrain status\n```\n\n"
        "## a: A again\n\n```bash\nbrain doctor\n```\n",
        "# brain examples\n\n## a\n\n```bash\nbrain status\n```\n",
        "# brain examples\n\n## A B: A\n\n```bash\nbrain status\n```\n",
        "# brain examples\n\n## a: A\n\n```sh\nbrain status\n```\n",
    ],
    ids=[
        "empty",
        "no-topic",
        "prose-outside-a-fence",
        "invocation-before-a-topic",
        "invocation-outside-a-fence",
        "fence-before-a-topic",
        "fence-left-open",
        "topic-without-an-invocation",
        "topic-without-a-fence",
        "repeated-slug",
        "heading-without-a-title",
        "heading-with-a-bad-slug",
        "other-fence-language",
    ],
)
def test_a_malformed_document_is_refused(text: str) -> None:
    with pytest.raises(ExamplesResourceError):
        parse_examples(text)


def test_the_packaged_resource_has_exactly_the_five_topics_each_with_invocations() -> None:
    """The floor that stops the rot test passing over an empty list."""
    topics = load_examples()
    assert tuple(topic.slug for topic in topics) == _TOPICS
    assert all(topic.title for topic in topics)
    assert all(len(topic.invocations) >= 3 for topic in topics)


@pytest.mark.parametrize(
    "error",
    [OSError("x"), ModuleNotFoundError("x"), UnicodeDecodeError("utf-8", b"x", 0, 1, "x")],
    ids=["unreadable", "package-missing", "not-utf-8"],
)
def test_an_unreadable_resource_is_one_error_with_no_path(error: Exception) -> None:
    with (
        mock.patch("brain.examples.importlib.resources.files", side_effect=error),
        pytest.raises(ExamplesResourceError) as caught,
    ):
        load_examples()
    assert "/" not in str(caught.value)
