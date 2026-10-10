"""Notes for a recorded session, read from a file written beforehand.

The file is plain text. A line holding only ``---`` separates one note from
the next; blank lines at either end of a note are dropped, and so is a note
that is left empty.
"""

from __future__ import annotations

import base64
import os

SEPARATOR = "---"


def parse(text: str) -> list[str]:
    """Split the text of a notes file into notes, in file order."""
    notes: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == SEPARATOR:
            notes.append("\n".join(current).strip("\n"))
            current = []
        else:
            current.append(line.rstrip())
    notes.append("\n".join(current).strip("\n"))
    return [note for note in notes if note.strip()]


def load(path: str | None) -> list[str]:
    """The notes from ``path``, else from KEXPLORE_NOTES, else none.

    run.sh reads the file on the host and passes it base64-encoded in
    KEXPLORE_NOTES, because the VM or container does not see every host path.
    """
    if path:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            return parse(handle.read())
    encoded = os.environ.get("KEXPLORE_NOTES", "")
    if not encoded:
        return []
    return parse(base64.b64decode(encoded).decode("utf-8"))
