"""Run the userspace command under the cursor and show what it prints.

Tracing (t) runs the same command to record the kernel functions it reaches
and discards its output. This shows the output, so the kernel's value and the
one userspace reports can be read side by side.

The command runs through ``sh -c`` as the user kexplore runs as (root), with no
stdin and a time limit, so an interactive tool such as slabtop ends instead of
waiting for keys.
"""

from __future__ import annotations

import subprocess

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

TIMEOUT = 10.0
# Lines kept from each stream. lsof on a busy box prints tens of thousands.
MAX_LINES = 2000


def run(command: str) -> tuple[str, str]:
    """(heading status, output) for one run of ``command``."""
    try:
        done = subprocess.run(
            ["sh", "-c", command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired as expired:
        out = expired.stdout or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return f"stopped after {TIMEOUT:g}s", out
    except OSError as error:
        return "could not start", str(error)
    text = done.stdout
    if done.stderr:
        text = f"{text}\n{done.stderr}" if text else done.stderr
    return f"exit {done.returncode}", text


def _clip(text: str) -> str:
    lines = text.rstrip("\n").splitlines()
    if len(lines) <= MAX_LINES:
        return "\n".join(lines)
    kept = "\n".join(lines[:MAX_LINES])
    return f"{kept}\n... {len(lines) - MAX_LINES} more lines not shown"


class CommandScreen(ModalScreen[None]):
    """The command, then its output once it finishes. Esc closes."""

    DEFAULT_CSS = """
    CommandScreen {
        align: center middle;
    }
    CommandScreen > Vertical {
        width: 90%;
        height: auto;
        max-height: 90%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    CommandScreen #command-heading {
        color: $text-muted;
    }
    CommandScreen VerticalScroll {
        height: auto;
        max-height: 40;
    }
    """

    BINDINGS = [Binding("escape", "dismiss", "close", priority=True)]

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"$ {self.command}  (running)", id="command-heading", markup=False)
            with VerticalScroll():
                yield Static("", id="command-output", markup=False)

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()
        self._run()

    @work(thread=True)
    def _run(self) -> None:
        status, output = run(self.command)
        # Expanded here because Textual 4.0.0's Static drops the text before a
        # tab: "Cpus_allowed_list:\t0-3" rendered as "        0-3".
        text = _clip(output).expandtabs() or "(no output)"
        self.app.call_from_thread(self._show, status, text)

    def _show(self, status: str, output: str) -> None:
        self.query_one("#command-heading", Static).update(
            f"$ {self.command}  ({status}; esc closes)"
        )
        self.query_one("#command-output", Static).update(output)
