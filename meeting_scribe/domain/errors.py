"""Errors a chat user can understand and act on.

The Discord UI answers these with a plain sentence of its own (never the exception text); any other
exception gets a generic "could not be completed" reply and goes to the log. They subclass the
built-in types the service always raised, so existing callers keep working.
"""
from __future__ import annotations


class SinkUnavailable(KeyError):
    """Kanban/Linear is off or not connected."""

    def __init__(self, sink: str) -> None:
        super().__init__(f"sink {sink!r} is not available")
        self.sink = sink


class ItemDismissed(ValueError):
    """The task was dismissed; it cannot be sent anywhere any more."""


class NotesNotReady(ValueError):
    """The meeting has no notes yet (still being processed)."""


class EmptyRecording(Exception):
    """The recording holds no voice at all (no track, or only silence the transcriber drops).

    Not a failure: the pipeline ends the meeting as ``empty`` without retrying, spending LLM or
    publishing anything."""


class NothingToReprocess(ValueError):
    """The meeting was discarded because no audio was captured; there is nothing to redo."""


class ChannelUnavailable(LookupError):
    """The Discord channel picked for a task is gone or the bot cannot post there."""


class DirectMessageUnavailable(LookupError):
    """The assignee cannot receive a direct message from the bot (DMs closed, unknown user)."""


class NotPrivate(ValueError):
    """A share button of a private meeting was pressed on a meeting that is not private (stale message)."""


class ForumTagRequired(ChannelUnavailable):
    """The picked channel is a forum that requires a tag on every post and none fits the task."""
