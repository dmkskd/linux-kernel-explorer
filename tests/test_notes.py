"""Splitting a --notes file, and stepping through the notes, with no UI.

``kexplore/notes.py`` is text work and ``NoteDeck`` is a counter, so both run
anywhere. The popup itself is Textual and is not exercised here.
"""

from __future__ import annotations

import base64
import os
import sys

from kexplore.notes import load, parse

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


SAMPLE = """
task_struct is the kernel's record of one thread.
Follow `mm` to reach its address space.
---

The files tab.

  indented line kept
---
---
   ---
last note, no trailing separator
"""


def test_parse() -> None:
    notes = parse(SAMPLE)
    check(len(notes) == 3, "three notes; empty ones between separators are dropped")
    check(
        notes[0] == "task_struct is the kernel's record of one thread.\n"
                    "Follow `mm` to reach its address space.",
        "a note keeps its inner newlines and loses the blank line before it",
    )
    check(
        notes[1] == "The files tab.\n\n  indented line kept",
        "inner blank lines and indentation survive",
    )
    check(notes[2] == "last note, no trailing separator", "the last note needs no separator")
    check(parse("") == [], "an empty file has no notes")
    check(parse("a --- b") == ["a --- b"], "--- inside a line is text, not a separator")


def test_load_from_environment() -> None:
    os.environ["KEXPLORE_NOTES"] = base64.b64encode("one\n---\ntwo".encode()).decode()
    try:
        check(load(None) == ["one", "two"], "KEXPLORE_NOTES is decoded when there is no path")
    finally:
        del os.environ["KEXPLORE_NOTES"]
    check(load(None) == [], "no path and no KEXPLORE_NOTES gives no notes")


def test_deck() -> None:
    # Textual is not on every host, and NoteDeck does not need it.
    try:
        from kexplore.tui.notes import NoteDeck
    except ImportError:
        print("  skip NoteDeck: textual is not installed here")
        return
    deck = NoteDeck(["a", "b"])
    check(deck.next()[:2] == (0, "a"), "the first w shows note 1")
    check(deck.next()[2].startswith("note 2/2"), "the heading counts notes")
    check(deck.next()[:2] == (1, "b"), "w past the last note stays on the last")
    check(deck.previous()[:2] == (0, "a"), "W goes back one")
    deck.store(0, "a, edited")
    check(deck.previous()[1] == "a, edited", "an edit made in the popup is kept")
    blank = NoteDeck([])
    check(blank.next()[:2] == (None, ""), "without notes, w opens a blank one")
    blank.store(None, "pasted")
    check(blank.next()[1] == "pasted", "the blank note keeps what was pasted into it")


def main() -> int:
    test_parse()
    test_load_from_environment()
    test_deck()
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
