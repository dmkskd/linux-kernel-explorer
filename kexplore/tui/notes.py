"""A popup holding one note, for commentary in a recorded session.

The popup is drawn into the terminal like the rest of the UI, so asciinema
records it with no extra step. Notes come from ``--notes``; without that file
the popup is an empty scratch area that keeps its text between openings, for
typing or pasting during the session.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea


class NoteScreen(ModalScreen[str]):
    """One note in a bordered box over the current screen. Esc closes it."""

    DEFAULT_CSS = """
    NoteScreen {
        align: center middle;
    }
    NoteScreen > Vertical {
        width: 70%;
        max-width: 100;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    NoteScreen #note-heading {
        color: $text-muted;
        height: 1;
    }
    NoteScreen TextArea, NoteScreen TextArea:focus {
        height: auto;
        max-height: 24;
        border: none;
        padding: 0;
        background: $surface;
    }
    """

    BINDINGS = [Binding("escape", "close", "close note", priority=True)]

    def __init__(self, text: str, heading: str) -> None:
        super().__init__()
        self.text = text
        self.heading = heading

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.heading, id="note-heading", markup=False)
            yield TextArea(self.text, soft_wrap=True, show_line_numbers=False)

    def on_mount(self) -> None:
        area = self.query_one(TextArea)
        area.focus()
        # At the end, so a paste adds to the note instead of preceding it.
        area.move_cursor(area.document.end)

    def action_close(self) -> None:
        # The text goes back to the caller, so an edit made on screen is still
        # there the next time this note is shown.
        self.dismiss(self.query_one(TextArea).text)


class NoteDeck:
    """Which note is showing, and the text of each one including edits."""

    def __init__(self, notes: list[str]) -> None:
        self.notes = list(notes)
        # Index of the note shown last; -1 before the first.
        self.position = -1
        self.scratch = ""

    def next(self) -> tuple[int | None, str, str]:
        """The note after the last one shown: (index, text, heading)."""
        if not self.notes:
            return None, self.scratch, "note  (esc closes)"
        self.position = min(self.position + 1, len(self.notes) - 1)
        return self._at(self.position)

    def previous(self) -> tuple[int | None, str, str]:
        """The note before the last one shown."""
        if not self.notes:
            return self.next()
        self.position = max(self.position - 1, 0)
        return self._at(self.position)

    def store(self, index: int | None, text: str | None) -> None:
        """Keep the text the popup closed with."""
        if text is None:
            return
        if index is None:
            self.scratch = text
        else:
            self.notes[index] = text

    def _at(self, index: int) -> tuple[int, str, str]:
        heading = f"note {index + 1}/{len(self.notes)}  (esc closes)"
        return index, self.notes[index], heading
