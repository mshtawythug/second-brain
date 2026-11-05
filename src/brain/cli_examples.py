"""`brain examples` — find the right command by example (RM-53).

Plain ``typer.echo``, not Rich: Rich would wrap a long invocation at the terminal's width and read
``[...]`` as markup, and both break copy and paste. No ``Config.load()`` and no database, so this
works before ``brain init`` and with a bad ``BRAIN_*`` variable. An unknown topic is not echoed, so
an argument cannot put escape sequences on the terminal.
"""
from __future__ import annotations

import typer

from .examples import ExamplesResourceError, ExampleTopic, load_examples

_UNREADABLE = "The packaged examples could not be read; reinstall brain."


def _render(topic: ExampleTopic) -> str:
    return "\n".join((f"{topic.slug}: {topic.title}", "", *topic.lines))


def examples(
    topic: str | None = typer.Argument(
        None, help="One topic to show, such as search. Omit it to show every topic."
    ),
) -> None:
    """Show curated example commands, grouped by task."""
    try:
        topics = load_examples()
    except ExamplesResourceError:
        typer.echo(_UNREADABLE, err=True)
        raise typer.Exit(code=1) from None
    if topic is None:
        typer.echo("\n\n".join(_render(each) for each in topics))
        return
    chosen = [each for each in topics if each.slug == topic]
    if not chosen:
        valid = ", ".join(each.slug for each in topics)
        typer.echo(f"Unknown topic. Valid topics: {valid}", err=True)
        raise typer.Exit(code=2)
    typer.echo(_render(chosen[0]))


def register(app: typer.Typer) -> None:
    """Attach ``brain examples`` to ``app``."""
    app.command()(examples)
