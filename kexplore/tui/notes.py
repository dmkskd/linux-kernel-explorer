"""A popup holding one note, for commentary in a recorded session.

The popup is drawn into the terminal like the rest of the UI, so asciinema
records it with no extra step. Notes come from ``--notes``; without that file
the popup is an empty scratch area that keeps its text between openings, for
typing or pasting during the session.

A note is written in Markdown and shown rendered: ``**bold**``, ``*italic*``,
```code```, lists and headings. Unlike standard Markdown, every newline is a
line break, so a note written as plain lines keeps them. ``e`` opens the source for editing; an empty
note opens straight into the editor.
"""

from __future__ import annotations

from markdown_it import MarkdownIt
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TextArea


def _hard_breaks(state) -> None:
    for token in state.tokens:
        for child in token.children or ():
            if child.type == "softbreak":
                child.type = "hardbreak"


def _parser() -> MarkdownIt:
    # Textual's default parser, except that a single newline ends the line.
    # The "breaks" option would not do it: it changes markdown-it's HTML
    # output, and Textual reads the tokens, where the newline stays a softbreak.
    parser = MarkdownIt("gfm-like")
    parser.core.ruler.push("hard_breaks", _hard_breaks)
    return parser


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
    NoteScreen Markdown {
        max-height: 24;
        padding: 0;
        background: $surface;
    }
    NoteScreen Markdown:focus {
        background-tint: $foreground 0%;
    }
    NoteScreen TextArea, NoteScreen TextArea:focus {
        height: auto;
        max-height: 24;
        border: none;
        padding: 0;
        background: $surface;
    }
    """

    # Focus is placed in on_mount; left to Textual, the hidden editor takes it
    # and swallows the e that should open it.
    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("escape", "close", "close note", priority=True),
        # Not priority: while the editor has focus, e is a letter to type.
        Binding("e", "edit", "edit note"),
    ]

    def __init__(self, text: str, heading: str) -> None:
        super().__init__()
        self.text = text
        self.heading = heading

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.heading, id="note-heading", markup=False)
            yield Markdown(self.text, parser_factory=_parser)
            yield TextArea(self.text, soft_wrap=True, show_line_numbers=False)

    def on_mount(self) -> None:
        if self.text.strip():
            self.query_one(TextArea).display = False
        else:
            self.action_edit()

    def action_edit(self) -> None:
        self.query_one(Markdown).display = False
        area = self.query_one(TextArea)
        area.display = True
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
            return None, self.scratch, "note  (e edits, esc closes)"
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
        heading = f"note {index + 1}/{len(self.notes)}  (e edits, esc closes)"
        return index, self.notes[index], heading
