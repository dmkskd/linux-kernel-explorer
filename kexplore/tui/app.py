"""Textual frontend.

Runs in the same process as drgn, so the model is the live ``drgn.Object``
itself and there is no serialization layer. The ``:`` binding suspends the UI
and opens a drgn REPL with the object under the cursor bound to ``obj``.

What a frame *contains* is decided in ``view.frames``; this module decides when
to build one, which one is on screen, and what the keys do to it.
"""

from __future__ import annotations

import code
import re
import textwrap
from dataclasses import dataclass, field, replace

import drgn
from drgn import Object, Program
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text


def safe_escape(text: str) -> str:
    """Escape text for Textual markup, ensuring unmatched brackets do not break markup parsing."""
    if not text:
        return ""
    escaped = escape(str(text))
    return re.sub(r"(?<!\\)\[", r"\\[", escaped)
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Footer, Header, Input, Static, Tab, Tabs, Tree

from ..catalog.procfs import served_by
from ..catalog.registry import Measurement, Subsystem, subsystems
from ..catalog.userspace import UNFILLED, runnable
from ..core import ctypes as ct
from ..core.nav import Row, follow
from ..core.source import KernelSource, StructDoc
from ..operations.algorithm import algorithms
from ..operations.tour import GuidedTour, GuidedTutorial, TourStep, TutorialStep, tours, tutorials
from ..operations.walkthrough import WALKTHROUGHS
from ..view import frames
from ..view.frames import (
    FIELD_COLUMNS,
    GROUP_DOCS,
    SOURCE_COLUMNS,
    Context,
    Frame,
    Listing,
    Plan,
)
from ..view.frames import MAX_CELL, matches_field as _matches_field
from .clipboard import copy_to_system_clipboard
from .graph import GraphScreen, graph_key
from .navigator import CursorNavigator

# Source frames are exempt. Their wide column is the last one, so a long line
# pushes nothing off screen: it makes the table scroll sideways instead, which
# is the right answer for code. The line number and the current-line marker are
# pinned so they survive that scroll.
SOURCE_FIXED_COLUMNS = 2

# How long the tree cursor must rest on an entry before it is built. Every
# entry resolves by walking kernel memory, so without this, holding an arrow
# key queues one walk per keystroke and the last one wins anyway.
PREVIEW_DELAY = 0.2


def _clip(text: str, limit: int | None = MAX_CELL) -> str:
    text = str(text)
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _highlight_source(rows: list[Row]) -> dict[str, Text]:
    """Colour a block of C, keyed by the line number each row carries.

    Lexed in one pass over the whole block rather than line by line, because a
    ``/* */`` comment running over several lines is only recognised as a comment
    on its later lines if the lexer carries its state across them. Keying by
    line number rather than by position lets the filter hide rows without
    changing what the remaining ones look like.

    ``ansi_dark`` renders through the sixteen terminal colours, so the result
    follows the scheme the terminal is already using instead of painting a
    background of its own.
    """
    code = "\n".join(row.value for row in rows)
    highlighted = Syntax(code, "c", theme="ansi_dark").highlight(code)
    return {
        row.name: line
        for row, line in zip(rows, highlighted.split("\n"), strict=True)
    }


# One colour per row kind, applied to the name cell. Links and derived rows are
# things the explorer added; fields are what the struct actually declares, so
# fields keep the default colour and the added rows are the ones that stand out.
KIND_STYLES = {
    "link": "bold blue",
    "derived": "cyan",
    "error": "bold red",
    "truncated": "dim italic",
}

# A value cell is "lead  = annotation", where the annotation is a decoder's
# reading of the number in front of it ("1  = S (sleeping)"). Split so the two
# halves can be coloured apart.
# The separating whitespace stays in the first group, so no styled span ever
# starts or ends on a space.
_ANNOTATED = re.compile(r"^(.*?\s\s+)(=\s.*)$", re.DOTALL)
# A number followed by the same number in hex, or by a name: "0 (root)".
_TRAILING_PAREN = re.compile(r"^([^(]*?\s*)(\(.*\))$", re.DOTALL)


def _paint_value(cell: Text) -> None:
    """Colour a value cell by the form its text takes.

    The forms are the ones the catalog produces: an aggregate placeholder, a
    null pointer, a hex address, a number, and any of those followed by a
    decoder's annotation. Anything else keeps the default colour.
    """
    text = cell.plain
    body = text
    match = _ANNOTATED.match(text)
    if match:
        body = match.group(1)
        cell.stylize("cyan", len(body), len(text))

    # rstrip so the annotation's separator does not stop the paren matching;
    # slicing still uses offsets from the start of the cell.
    body = body.rstrip()
    end = len(body)
    inner = _TRAILING_PAREN.match(body)
    if inner:
        end = len(inner.group(1))
        cell.stylize("dim", end, len(body))

    lead = body[:end].rstrip()
    if not lead.strip():
        return
    if lead.startswith("{"):
        style = "dim"
    elif lead == "NULL":
        style = "dim red"
    elif lead.startswith("0x"):
        style = "yellow"
    elif lead.lstrip("-").isdigit():
        style = "green"
    else:
        return
    cell.stylize(style, 0, len(lead))


def _descendants(node):
    """Every node under this one, the tree's own iteration being top level."""
    for child in node.children:
        yield child
        yield from _descendants(child)


def _type_of(row: Row) -> str:
    """The C type a field row would show outside userspace mode."""
    return row.original_type or row.type_name


class FieldsTable(DataTable):
    """The detail pane, with enter named.

    DataTable and Tree both bind enter themselves, with show=False, and the
    focused widget's binding is the one the footer prints. An App-level
    "enter follow" is therefore invisible exactly when it applies. Declaring it
    on the widget keeps it on the footer, and keeps it accurate: enter means
    follow in the table and open in the sidebar.
    """

    BINDINGS = [
        Binding("enter", "select_cursor", "follow"),
        Binding("c", "copy", "copy", show=False),
        Binding("C", "copy_row", "copy row", show=False),
        Binding("y", "copy", "copy", show=False),
        Binding("m", "toggle_mouse", "mouse", show=False),
    ]

    def action_copy(self) -> None:
        if hasattr(self.app, "action_copy"):
            self.app.action_copy()

    def action_copy_row(self) -> None:
        if hasattr(self.app, "action_copy_row"):
            self.app.action_copy_row()

    def action_toggle_mouse(self) -> None:
        if hasattr(self.app, "action_toggle_mouse"):
            self.app.action_toggle_mouse()


class NavTree(Tree):
    BINDINGS = [
        Binding("enter", "select_cursor", "open"),
        Binding("c", "copy", "copy", show=False),
        Binding("y", "copy", "copy", show=False),
        Binding("m", "toggle_mouse", "mouse", show=False),
    ]

    def action_copy(self) -> None:
        if hasattr(self.app, "action_copy"):
            self.app.action_copy()

    def action_toggle_mouse(self) -> None:
        if hasattr(self.app, "action_toggle_mouse"):
            self.app.action_toggle_mouse()


class TutorialBanner(Static):
    """Commentary and navigation banner for live guided tutorials."""

    header_variant: int = 2  # 1 = title only, 2 = title and the traversal roadmap

    def update_step(
        self,
        tutorial_label: str,
        step_num: int,
        total_steps: int,
        step: TutorialStep,
        steps: list[TutorialStep] | None = None,
        auto_play: bool = False,
        variant: int | None = None,
        seconds_left: int | None = None,
        is_travelling: bool = False,
    ) -> None:
        if variant is not None:
            self.header_variant = variant
        lines: list[str] = []

        # 1. Step title + auto-play status
        if auto_play:
            if is_travelling:
                auto_badge = "[bold white on #059669] ▶ AUTO PLAYING (navigating...) [/] [dim]('a' to pause)[/]"
            else:
                secs_str = f" (advancing in {seconds_left}s)" if seconds_left is not None else ""
                auto_badge = f"[bold white on #059669] ▶ AUTO PLAYING{secs_str} [/] [dim]('a' to pause)[/]"
        else:
            auto_badge = "[dim]('a' for auto-play)[/]"
        lines.append(
            f"[bold yellow]Step {step_num} of {total_steps}:[/] [bold white]{safe_escape(step.title)}[/]   {auto_badge}"
        )

        # 2. Compact, single-line Flow roadmap showing progression through data
        # structures. Variant 1 drops it, leaving the title alone: the step
        # narration is no longer part of this banner, so the roadmap is the only
        # thing left for the variants to differ over.
        if steps and self.header_variant >= 2:
            flow_nodes = []
            for i, s in enumerate(steps, start=1):
                name = s.get_flow_label() if hasattr(s, "get_flow_label") else getattr(s, "flow_label", "")
                if not name:
                    name = s.action.split("›")[-1].strip().split("(")[0].strip()[:14]
                if i < step_num:
                    flow_nodes.append(f"[dim green]✓ {safe_escape(name)}[/]")
                elif i == step_num:
                    flow_nodes.append(f"[bold bright_yellow]▶ {safe_escape(name)}[/]")
                else:
                    flow_nodes.append(f"[dim]{safe_escape(name)}[/]")
            lines.append("   [dim]Flow:[/] " + " [bold dim cyan]──▶[/] ".join(flow_nodes))

        # The narration itself is rendered by TutorialCallout, beside the row it
        # describes. Repeating a shortened form of it here put two accounts of
        # the same step on screen at once.

        self.update("\n".join(lines))
        self.display = True


TourBanner = TutorialBanner


# Tokens the narration is worth colouring. One alternation, so each character is
# claimed by exactly one group and nothing is painted twice. The point is to
# make the flag and permission names stand out from the prose around them: the
# difference between a private and a shared mapping is carried entirely by those
# words, and in running text they read like every other word.
_NARRATION_TOKENS = re.compile(
    r"(?P<struct>\bstruct\s+[a-z_][a-z0-9_]*)"
    r"|(?P<const>\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b)"
    r"|(?P<perm>\br[-w][-x][ps]\b)"
    r"|(?P<call>\b[a-z_][a-z0-9_]*\(\))"
    r"|(?P<member>\b[a-z][a-z0-9]*_[a-z0-9_]+\b)"
    r"|(?P<bare>\b(?:mm|brk|pid|tgid|comm|anon|shmem|NULL)\b)"
)

_NARRATION_STYLES = {
    "struct": "bold #7dd3fc",   # struct names, the things being navigated
    "const": "bold #fbbf24",    # VM_EXEC, CLONE_VM, PF_KTHREAD
    "perm": "bold #f472b6",     # r-xp, rw-p, as /proc prints them
    "call": "#86efac",          # brk(), fork()
    "member": "#7dd3fc",        # struct members
    "bare": "#7dd3fc",          # members short enough to carry no underscore
}


def paint_narration(text: str) -> str:
    """Mark up the kernel names in a line of narration."""

    def one(match: re.Match[str]) -> str:
        kind = match.lastgroup or ""
        style = _NARRATION_STYLES.get(kind)
        body = safe_escape(match.group(0))
        return f"[{style}]{body}[/]" if style else body

    out: list[str] = []
    last = 0
    for match in _NARRATION_TOKENS.finditer(text):
        out.append(safe_escape(text[last:match.start()]))
        out.append(one(match))
        last = match.end()
    out.append(safe_escape(text[last:]))
    return "".join(out)


class TutorialCallout(Static):
    """The step narration, floated over the table beneath the row it explains.

    It is not a table row. The field column is clipped to ``MAX_CELL`` so that
    one wide cell cannot push the type and value columns off screen, and prose
    wrapped to that width is unreadable. This sits on its own layer instead, so
    it takes the width of the pane and stays next to what it is describing.
    """

    def prepare(self, text: str, width: int) -> int:
        """Lay the narration out for ``width`` and report the height it needs.

        The caller decides where to put it, and cannot do that without knowing
        how tall it is: near the bottom of the pane there is no room below the
        row and the block has to go above it instead.
        """
        body = " ".join(str(text).split())
        if not body:
            return 0
        # The border and padding take six columns, and the block is indented
        # four from the left edge of the pane.
        wrap = max(28, min(92, width - 14))
        lines = textwrap.wrap(body, width=wrap) or [body]
        self.update("\n".join(paint_narration(line) for line in lines))
        # Two more rows than lines of text: the border draws one above and one
        # below.
        return len(lines) + 2

    def place(self, y: int) -> None:
        self.styles.offset = (4, y)
        self.display = True

    def hide(self) -> None:
        self.display = False


class TutorialLanding(VerticalScroll):
    """Full-screen scrollable landing page and itinerary for a guided tutorial."""

    can_focus = True

    BINDINGS = [
        Binding("enter", "start_step_1", "begin step 1", priority=True),
        Binding("space", "start_step_1", "begin step 1", priority=True, show=False),
        Binding("n", "start_step_1", "begin step 1", priority=True, show=False),
        Binding("a", "start_auto", "auto-play walkthrough", priority=True),
        Binding("p", "prev", "back", priority=True, show=False),
        Binding("c", "copy", "copy link / overview", priority=True),
        Binding("y", "copy", "copy link / overview", priority=True, show=False),
        Binding("m", "toggle_mouse", "mouse selection", priority=True),
        Binding("escape", "exit", "exit", priority=True, show=False),
        Binding("down", "scroll_down", "scroll ↓", show=True),
        Binding("up", "scroll_up", "scroll ↑", show=True),
        Binding("j", "scroll_down", "scroll down", show=False),
        Binding("k", "scroll_up", "scroll up", show=False),
        Binding("page_down,pagedown", "page_down", "pgdn", show=True),
        Binding("page_up,pageup", "page_up", "pgup", show=True),
        Binding("home", "scroll_home", "top", show=False),
        Binding("end", "scroll_end", "bottom", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="tutorial-landing-content")

    def update(self, content: str | Text) -> None:
        try:
            self.query_one("#tutorial-landing-content", Static).update(content)
        except Exception:
            pass

    def render(self):
        try:
            return self.query_one("#tutorial-landing-content", Static).render()
        except Exception:
            return ""

    def action_start_step_1(self) -> None:
        app = getattr(self, "app", None)
        if app is not None and hasattr(app, "action_tutorial_next"):
            app.action_tutorial_next()

    def action_start_auto(self) -> None:
        app = getattr(self, "app", None)
        if app is not None and hasattr(app, "action_toggle_auto"):
            app.action_toggle_auto()

    def action_prev(self) -> None:
        app = getattr(self, "app", None)
        if app is not None and hasattr(app, "action_tutorial_prev"):
            app.action_tutorial_prev()

    def action_copy(self) -> None:
        app = getattr(self, "app", None)
        if app is not None and hasattr(app, "action_copy"):
            app.action_copy()

    def action_toggle_mouse(self) -> None:
        app = getattr(self, "app", None)
        if app is not None and hasattr(app, "action_toggle_mouse"):
            app.action_toggle_mouse()

    def action_exit(self) -> None:
        app = getattr(self, "app", None)
        if app is not None and hasattr(app, "exit_tutorial"):
            app.exit_tutorial()

    def on_click(self) -> None:
        self.focus()

    def update_tutorial(
        self,
        tutorial: GuidedTutorial,
        steps: list[TutorialStep],
        is_active: bool = True,
    ) -> None:
        lines: list[str] = []

        # Status badge and header
        status_badge = (
            "[bold white on #15803d] TUTORIAL SELECTED [/]"
            if is_active
            else "[bold white on #374151] TUTORIAL PREVIEW [/]"
        )
        lines.append(
            f"{status_badge}  [bold white]{safe_escape(tutorial.label)}[/]   "
            f"[dim cyan]({safe_escape(tutorial.category)})[/]   "
            f"[bold white on #1e3a8a] {len(steps)} Live Steps [/]"
        )
        lines.append("")

        # Prominent launch action bar right at top of screen
        if is_active:
            lines.append(
                "  [bold white on #1e3a8a]  ▶ Press \\[Enter], \\[Space], or \\[n] to Begin Step 1  [/]    "
                "[bold white on #059669]  \\[a] Auto-Play Walkthrough  [/]    "
                "[dim]•  \\[c] Copy Link  •  \\[Esc] Exit[/dim]"
            )
        else:
            lines.append(
                "  [bold white on #0284c7]  👉 Press \\[Enter] to Select & Launch Tutorial  [/]    "
                "[bold white on #059669]  \\[a] Auto-Play  [/]    "
                "[dim]•  \\[c] Copy Link[/dim]"
            )
        lines.append("")

        # Overview / Scope
        lines.append("[bold green]What You'll Explore:[/]")
        lines.append(f"  {safe_escape(tutorial.doc)}")
        lines.append("")

        # Video companion (if available)
        video_url = getattr(tutorial, "video_url", "")
        video_title = getattr(tutorial, "video_title", "")
        if video_url:
            title_text = f"[bold white]{safe_escape(video_title)}[/]  " if video_title else ""
            lines.append(f"  [bold cyan]Video companion:[/] {title_text}[bold underline bright_blue]{safe_escape(video_url)}[/]  [dim]('c' to copy link)[/dim]")
            lines.append("")

        # Architectural Traversal Flow
        flow_parts = []
        for i, s in enumerate(steps, start=1):
            target = s.get_flow_label() if hasattr(s, "get_flow_label") else getattr(s, "flow_label", "")
            if not target:
                target = s.action.split("›")[-1].strip() or s.title.split("(")[0].strip()
            flow_parts.append(f"[bold bright_yellow]{safe_escape(target)}[/]")
        lines.append("[bold green]Architectural Traversal Flow:[/]")
        lines.append("  " + " [bold dim cyan]──▶[/] ".join(flow_parts))
        lines.append("")

        # Tutorial steps
        lines.append(f"[bold cyan]Tutorial steps & itinerary ({len(steps)} live steps):[/]  [bold bright_yellow]▼ Scroll down (↓ / j / PgDn / mouse wheel) to view all steps[/]")
        lines.append("  [dim]" + "─" * 72 + "[/dim]")
        for idx, step in enumerate(steps, start=1):
            action_field = step.get_action_field() if hasattr(step, "get_action_field") else (step.action_field or step.highlight_field)
            action_badge = f"[bold bright_cyan]👉 \\[ENTER] {safe_escape(action_field)}[/]" if action_field else "[dim](inspect)[/]"
            val_fields = step.get_value_fields() if hasattr(step, "get_value_fields") else step.value_fields
            val_names = ", ".join(val_fields)
            val_badge = f"   [dim]Value:[/] [bold bright_yellow]💡 {safe_escape(val_names)}[/]" if val_names else ""
            lines.append(f"  [bold yellow]{idx}. {safe_escape(step.title)}[/]   [dim]›[/]  [cyan]{safe_escape(step.action)}[/]")
            lines.append(f"     [dim]Action:[/] {action_badge}{val_badge}")
            if step.userspace:
                lines.append(f"     [dim]Userspace:[/] [green]{safe_escape(step.userspace)}[/]")
            insight = step.get_insight() if hasattr(step, "get_insight") else (step.commentary.split(". ")[0].strip() + ".")
            if insight:
                lines.append(f"     [dim]Takeaway:[/] [italic white]{safe_escape(insight)}[/]")
            lines.append("")
            if idx == 5 and len(steps) > 5:
                lines.append(f"  [bold bright_yellow]─── ▼ Scroll down (↓ / PgDn) for remaining steps 6 to {len(steps)} ▼ ───[/]")
                lines.append("")

        lines.append("  [bold bright_yellow]▲ End of itinerary · Scroll up (↑ / k / PgUp) to return to top[/]")
        lines.append("  [dim]" + "─" * 72 + "[/dim]")
        lines.append(
            "  [bold white on #1e3a8a]  Press \\[Enter] or \\[n] to Begin Step 1  [/]    "
            "[bold white on #059669]  \\[a] Auto-Play  [/]    "
            "[bold white on #374151]  \\[c] Copy Link  [/]"
        )
        lines.append(
            "  [dim]Shortcuts: [bold bright_yellow]\\[m][/] Mouse Select   •   "
            "[bold bright_yellow]\\[p][/] Overview   •   "
            "[bold bright_yellow]\\[Esc][/] Exit[/dim]"
        )
        lines.append("")
        lines.append("")

        self.update("\n".join(lines))
        self.display = True
        try:
            self.scroll_home(animate=False)
        except Exception:
            pass

    update_tour = update_tutorial


TourLanding = TutorialLanding


@dataclass
class TutorialSession:
    tutorial: GuidedTutorial
    steps: list[TutorialStep]
    current_idx: int = 0
    step_frames: dict[int, Frame] = field(default_factory=dict)

    @property
    def tour(self) -> GuidedTutorial:
        return self.tutorial

    @tour.setter
    def tour(self, val: GuidedTutorial) -> None:
        self.tutorial = val


TourSession = TutorialSession




def get_action_cell_text(
    clean_name: str,
    variant: int = 1,
    blink_phase: bool = True,
) -> Text:
    """Return the styled text for the action row name cell, honoring the blink phase."""
    if variant == 1:
        # Software blink alternating between navy-backed and bright-yellow-backed styles
        style = "bold bright_yellow on #1e3a8a" if blink_phase else "bold black on bright_yellow"
        return Text(f"👉 [ENTER] {clean_name}", style=style)
    else:
        # Variant 2: Subtle arrow pulse
        style = "bold bright_yellow" if blink_phase else "bold bright_cyan on #374151"
        return Text(f"▶ {clean_name} ↵", style=style)


def style_action_row(
    clean_name: str,
    values: list[str],
    cells: list[Text],
    next_target: str,
    is_final: bool,
    variant: int = 1,
    blink_phase: bool = True,
) -> None:
    """Style the action row that user navigates to advance the tutorial."""
    if variant == 1:
        # Variant 1: Interactive coach-mark badge with alternating high-contrast blink
        if not is_final:
            cells[0] = get_action_cell_text(clean_name, variant=1, blink_phase=blink_phase)
            if len(cells) > 1:
                cells[1].stylize("bold cyan")
            if len(cells) > 2:
                cells[2] = Text(f"──▶ follow into {next_target}", style="bold bright_green")
        else:
            cells[0] = Text(f"✓ {clean_name}", style="bold bright_green")
            if len(cells) > 2:
                cells[2] = Text(f"{values[2]}  ──▶ traversal complete!", style="bold bright_green")
    else:
        # Variant 2: Subtle arrow marker and direct action target
        if not is_final:
            cells[0] = get_action_cell_text(clean_name, variant=2, blink_phase=blink_phase)
            if len(cells) > 1:
                cells[1].stylize("cyan")
            if len(cells) > 2:
                cells[2] = Text(f"{values[2]}  [follow ──▶ {next_target}]", style="bold bright_green")
        else:
            cells[0] = Text(f"✓ {clean_name}", style="bold bright_green")
            if len(cells) > 2:
                cells[2] = Text(f"{values[2]}  [done]", style="bold bright_green")


def style_value_row(
    clean_name: str,
    values: list[str],
    cells: list[Text],
    variant: int = 1,
) -> None:
    """Style the value row displaying key data payoff for current step."""
    clean_val = values[2].strip() if len(values) > 2 else ""
    if variant == 1:
        # Variant 1: Insight bulb icon + golden star payoff
        cells[0] = Text(f"💡 {clean_name}", style="bold bright_cyan")
        if len(cells) > 2 and clean_val:
            cells[2] = Text(f"★ {clean_val}", style="bold bright_yellow")
    else:
        # Variant 2: Tag pill + highlighted value block
        cells[0] = Text(f"• {clean_name}", style="bold white on #0369a1")
        if len(cells) > 2 and clean_val:
            cells[2] = Text(f" {clean_val} ", style="bold bright_yellow on #374151")


class Explorer(App):
    CSS_PATH = "app.tcss"
    TITLE = "kexplore"

    BINDINGS = [
        # enter belongs to whichever widget has focus, and each names it there.
        # The footer prints the rest in this order, grouped by what they do to
        # the screen: move within it, change it, leave it for another view.
        Binding("space", "expand", "expand"),
        Binding("backspace", "back", "back"),
        Binding("slash", "search", "search"),
        Binding("o", "sort", "sort"),
        # Shift reverses whatever o settled on, which is worth having but not
        # worth a second slot on the footer.
        Binding("O", "sort_reverse", "reverse", show=False),
        Binding("r", "refresh", "refresh"),
        Binding("u", "userspace", "userspace"),
        Binding("v", "cycle_view", "view"),
        Binding("a", "toggle_auto", "autoplay"),
        Binding("H", "toggle_header_variant", "header variant", show=False),
        Binding("Y", "toggle_highlight_style", "highlight style", show=False),
        Binding("c", "copy", "copy"),
        Binding("C", "copy_row", "copy row", show=False),
        Binding("y", "copy", "copy", show=False),
        Binding("N", "copy_narration", "copy narration", show=False),
        Binding("m", "toggle_mouse", "mouse"),
        Binding("n", "tutorial_next", "next step"),
        Binding("p", "tutorial_prev", "prev step"),
        Binding("s", "source", "source"),
        Binding("t", "trace_command", "trace command"),
        Binding("g", "graph", "graph"),
        Binding("colon", "repl", "drgn repl"),
        # Escape undoes whatever is most local: the filter box, then the same
        # step backspace takes. Hidden from the footer, which already shows one.
        Binding("escape", "escape", "back", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(
        self,
        prog: Program,
        source: KernelSource | None = None,
        source_available: bool = True,
        live: bool = True,
        initial_tutorial: str | None = None,
        initial_tour: str | None = None,
    ) -> None:
        super().__init__()
        self.prog = prog
        self.initial_tutorial = initial_tutorial or initial_tour
        self.active_tutorial: TutorialSession | None = None
        self._tutorial_action_idx: int | None = None
        self._tutorial_action_clean_name: str = ""
        self._tutorial_action_target: str = ""
        self._tutorial_action_is_final: bool = False
        self.auto_play: bool = False
        self._auto_timer = None
        self._blink_timer = None
        self._blink_phase: bool = True
        self.blink_interval: float = 0.85
        self.auto_dwell_travel_settled: float = 3.0
        self.auto_dwell_already_in_place: float = 2.0
        self.auto_dwell_time: float = 3.0
        self._auto_seconds_remaining: float = 2.0
        self.highlight_variant: int = 1
        self.mouse_tracking: bool = True
        self._navigator = CursorNavigator(
            self,
            on_step=self._on_nav_step,
            on_settled=self._on_nav_settled,
        )
        self.animate_cursor: bool | None = None
        self.context = Context(
            prog,
            source or KernelSource(),
            live=live,
            source_available=source_available,
        )
        # False means startup probed and found no kernel source (or a vmcore,
        # where a lookup would describe the host kernel): the 's' key is
        # hidden by check_action and refuses in action_source. The default
        # keeps tests and direct construction ungated.
        self._source_available = source_available
        # Set by action_source when a fetch is in flight, so the view opens as
        # soon as the worker returns instead of requiring a second keypress.
        self._pending_source: tuple[str, str] | None = None
        self.stack: list[Frame] = []
        self.filter = ""
        self._docs: dict[str, StructDoc] = {}
        # Bumped by every navigation. A frame being built in a worker carries
        # the token it started with, so a result that arrives after the user
        # moved on is dropped instead of overwriting whatever is on screen now.
        self._token = 0
        # The last graph's layout, so returning to it from a detail view does
        # not throw away the branches the user opened.
        self.graph_state: dict | None = None
        # Pending sidebar preview, cancelled by the next cursor move and by any
        # navigation that would otherwise be overwritten when it fires.
        self._preview_timer = None
        # A node the sidebar cursor was moved onto by the pane rather than by
        # the user, whose highlight must not be answered with a preview.
        self._synced_node = None

    @property
    def active_tour(self) -> TutorialSession | None:
        return self.active_tutorial

    @active_tour.setter
    def active_tour(self, value: TutorialSession | None) -> None:
        self.active_tutorial = value

    @property
    def initial_tour(self) -> str | None:
        return self.initial_tutorial

    @initial_tour.setter
    def initial_tour(self, value: str | None) -> None:
        self.initial_tutorial = value

    def query_one(self, *args: Any, **kwargs: Any) -> Any:
        if args and isinstance(args[0], str):
            selector = args[0]
            if selector == "#tour-banner":
                args = ("#tutorial-banner", *args[1:])
            elif selector == "#tour-landing":
                args = ("#tutorial-landing", *args[1:])
            elif selector == "view-tours":
                args = ("view-tutorials", *args[1:])
        return super().query_one(*args, **kwargs)

    @property
    def source(self) -> KernelSource:
        return self.context.source

    @property
    def userspace(self) -> bool:
        return self.context.userspace

    def _landing_displayed(self) -> bool:
        """Whether a tutorial landing / overview page is currently displayed."""
        if self.active_tutorial is not None and self.active_tutorial.current_idx == 0:
            return True
        try:
            landing = self.query_one(TutorialLanding)
            return bool(landing.display)
        except Exception:
            return False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("copy", "copy_row", "toggle_mouse"):
            return True

        landing_shown = self._landing_displayed()
        showing_src = self._showing_source()

        # Step navigation: strictly context-dependent
        if action in ("tutorial_next", "tour_next"):
            if self.active_tutorial is not None:
                # In an active tutorial session: available until the final step
                if self.active_tutorial.current_idx < len(self.active_tutorial.steps):
                    return True
                return None
            if landing_shown:
                return True
            try:
                tree = self.query_one("#nav", Tree)
                node = getattr(tree, "cursor_node", None)
                if node and isinstance(getattr(node, "data", None), (GuidedTutorial, GuidedTour)):
                    return True
            except Exception:
                pass
            return None

        if action == "copy_narration":
            return (
                True
                if self.active_tutorial is not None
                and self.active_tutorial.current_idx > 0
                else None
            )

        if action in ("tutorial_prev", "tour_prev"):
            # Previous step is only valid inside an active tutorial session past step 0
            if self.active_tutorial is not None and self.active_tutorial.current_idx > 0:
                return True
            return None

        if action == "toggle_auto":
            if self.active_tutorial is not None:
                return True
            if landing_shown:
                return True
            try:
                tree = self.query_one("#nav", Tree)
                node = getattr(tree, "cursor_node", None)
                if node and isinstance(getattr(node, "data", None), (GuidedTutorial, GuidedTour)):
                    return True
            except Exception:
                pass
            return None

        # When the tutorial landing page is displayed, hide all actions that operate on
        # data tables, structs, or commands (none of which exist on the landing page)
        if landing_shown:
            if action in (
                "search",
                "sort",
                "sort_reverse",
                "refresh",
                "userspace",
                "source",
                "trace_command",
                "graph",
                "expand",
            ):
                return None

        # Source code view context: suppress struct/table manipulations
        if showing_src:
            if action in (
                "source",
                "sort",
                "sort_reverse",
                "refresh",
                "userspace",
                "trace_command",
                "graph",
                "expand",
            ):
                return None

        # Expand: only available when stack is active
        if action == "expand":
            if not self.stack:
                return None
            return True


        # Source inspection: requires source available and a loaded stack frame
        if action == "source":
            if not self._source_available or not self.stack:
                return None
            return True

        # Tracing commands: requires live kernel and an active stack frame
        if action == "trace_command":
            if not self.context.live or not self.stack:
                return None
            return True

        # Userspace equivalents: requires live kernel and an active stack frame
        if action == "userspace":
            if not self.context.live or not self.stack:
                return None
            return True

        # Sorting: requires an active stack frame
        if action in ("sort", "sort_reverse"):
            if not self.stack:
                return None
            return True

        # Refresh: requires live kernel and an active stack frame
        if action == "refresh":
            if not self.context.live or not self.stack:
                return None
            return True

        # Search / filter: requires an active stack frame
        if action == "search":
            if not self.stack:
                return None
            return True

        # Graph: requires an active stack frame
        if action == "graph":
            if not self.stack:
                return None
            return True

        return True

    def _showing_source(self) -> bool:
        return bool(self.stack) and self.stack[-1].columns == frames.SOURCE_COLUMNS

    # ------------------------------------------------------------ struct docs

    def struct_doc(self, obj: Object | None) -> StructDoc | None:
        """Kernel source comments for ``obj``'s type, fetched once per tag.

        Never blocks: recovering these runs pahole over a ~700MB vmlinux and
        then pulls the source file through debuginfod, so the first request for
        a tag starts a worker and returns ``None``. The doc and hint lines are
        rewritten when the worker returns.
        """
        if obj is None or not self._source_available:
            return None
        # obj may be a pointer here (a NULL link target is pushed as-is), and
        # pointer types have no tag.
        aggregate = ct.struct_type(obj.type_)
        tag = aggregate.tag if aggregate is not None else None
        if not tag:
            return None
        if tag in self._docs:
            return self._docs[tag]  # None while the worker is still running
        self._docs[tag] = None
        self._load_struct_doc(tag, ct.member_names(aggregate))
        return None

    def _load_struct_doc(self, tag: str, members: frozenset[str]) -> None:
        self.set_activity(f"reading kernel source for struct {tag}…")

        def work() -> None:
            def step(text: str) -> None:
                self.call_from_thread(self.set_activity, text)

            try:
                doc = self.source.document(tag, members, progress=step)
            except Exception as exc:  # noqa: BLE001 - a bad tag shouldn't kill the UI
                doc = StructDoc(tag, error=f"{type(exc).__name__}: {exc}")
            self.call_from_thread(self._struct_doc_done, tag, doc)

        self.run_worker(work, thread=True, group=f"doc:{tag}")

    def _still_fetching(self, tag: str) -> None:
        """Report a slow source fetch, unless it has already been answered."""
        if self._pending_source is not None and self._pending_source[0] == tag:
            self.notify(f"fetching the source for struct {tag}")

    def _struct_doc_done(self, tag: str, doc: StructDoc | None) -> None:
        # A failed lookup still gets recorded, so it is attempted once per tag
        # rather than on every repaint.
        self._docs[tag] = doc if doc is not None else StructDoc(tag, error="unavailable")
        self.set_activity("")
        self.update_doc()
        self.update_hint()
        pending = self._pending_source
        if pending is not None and pending[0] == tag:
            self._pending_source = None
            doc = self._docs[tag]
            if not doc.decl_file:
                self.notify("no source available here", severity="warning")
                return
            line = doc.decl_line
            title = f"struct {doc.tag}"
            if pending[1] in doc.member_lines:
                line = doc.member_lines[pending[1]]
                title = f"struct {doc.tag}.{pending[1]}"
            self.open_source(doc.decl_file, line, title)

    def update_doc(self) -> None:
        """Prefer the kernel's own words for this struct over the map's blurb."""
        if not self.stack:
            return
        frame = self.stack[-1]
        doc = self.struct_doc(frame.obj)
        if doc and (doc.summary or doc.location):
            where = f"[{doc.location}]" if doc.location else ""
            self.query_one("#doc", Static).update(f"{doc.summary} {where}".strip())
        else:
            self.query_one("#doc", Static).update(frame.doc)

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("select a subsystem", id="path", markup=False)
        with Horizontal():
            with Vertical(id="sidebar"):
                initial_tab = "view-tutorials" if self.initial_tutorial else "view-structures"
                yield Tabs(
                    Tab("structures", id="view-structures"),
                    Tab("operations", id="view-operations"),
                    Tab("tutorials", id="view-tutorials"),
                    id="views",
                    active=initial_tab,
                )
                yield NavTree("subsystems", id="nav")
            with Vertical(id="detail"):
                yield Static("", id="doc", markup=False)
                yield Static("", id="activity", markup=False)
                yield TutorialBanner(id="tutorial-banner")
                yield TutorialLanding(id="tutorial-landing")
                yield FieldsTable(id="fields", cursor_type="row", zebra_stripes=True)
                yield TutorialCallout(id="tutorial-callout")
                yield Static("", id="hint")
        yield Input(placeholder="filter fields…", id="search")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_bindings()
        self.query_one("#search", Input).display = False
        self.query_one("#activity", Static).display = False
        self.query_one(TutorialBanner).display = False
        self.query_one(TutorialLanding).display = False
        if self.initial_tutorial:
            tabs = self.query_one("#views", Tabs)
            tabs.active = "view-tutorials"
            self.build_tree("tutorials")
            matched = None
            query = self.initial_tutorial.lower().replace("-", "_").replace(" ", "_")
            for t in tutorials():
                if query in t.key.lower() or query in t.label.lower().replace("-", "_").replace(" ", "_"):
                    matched = t
                    break
            if matched is not None:
                self.sync_tree(matched)
                self.start_tutorial(matched)
            else:
                self.open_plan(frames.landing_plan(self.context))
        else:
            self.build_tree("structures")
            self.open_plan(frames.landing_plan(self.context))

    # ------------------------------------------------------------- background

    def set_activity(self, text: str) -> None:
        """One line saying what is happening off the UI thread, if anything.

        Everything that goes through debuginfod can block for as long as a
        download, so silence here reads as a hang.
        """
        widget = self.query_one("#activity", Static)
        widget.update(text)
        widget.display = bool(text)

    def open_plan(self, plan: Plan | None) -> None:
        """Show what a plan describes, building it off the UI thread if slow.

        The one path into every view. A deferred build reaches for pahole,
        addr2line, debuginfod or bpftrace, any of which can take seconds on a
        warm cache and minutes on a cold one, so a placeholder goes up first.
        """
        if plan is None:
            return
        if not plan.deferred:
            self.push(plan.build())
            return

        self.push(
            Frame(plan.label, plan.waiting_rows, doc=plan.doc, columns=plan.columns)
        )
        self.set_activity(plan.activity)
        token = self._token

        def work() -> None:
            try:
                if plan.build_with_progress is not None:
                    frame = plan.build_with_progress(
                        lambda text: self.call_from_thread(self.set_activity, text)
                    )
                else:
                    frame = plan.build()
                frame.load()
            except Exception as exc:  # noqa: BLE001 - report, don't kill the UI
                frame = Frame(
                    plan.label,
                    lambda exc=exc: [
                        Row(f"{type(exc).__name__}: {exc}", None, "", "", False,
                            kind="error")
                    ],
                    doc=plan.doc,
                    columns=plan.columns,
                )
                frame.load()
            self.call_from_thread(self._frame_ready, token, frame)

        self.run_worker(work, thread=True)

    def _frame_ready(self, token: int, frame: Frame) -> None:
        # The user may have navigated away while this was in flight, in which
        # case the frame that asked for it is no longer the one on top.
        if token != self._token or not self.stack:
            return
        self.set_activity("")
        self.stack[-1] = frame
        self.render_frame()

    # ---------------------------------------------------------------- sidebar

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """Switch which view the sidebar lists. The views are independent."""
        view = (event.tab.id or "").removeprefix("view-")
        if not view:
            return
        if view == "tours":
            view = "tutorials"
        if view != "tutorials":
            if self.active_tutorial is not None:
                self.exit_tutorial()
            self.query_one(TutorialLanding).display = False
            self.query_one("#fields", DataTable).display = True
            self.query_one("#doc", Static).display = True
        tree: Tree = self.query_one("#nav", Tree)
        current_data = getattr(getattr(tree, "root", None), "data", None)
        current_label = getattr(current_data, "label", "")
        if (view == "structures" and current_label == "subsystems") or (view == current_label):
            return
        self.build_tree(view)

    def build_tree(self, view: str) -> None:
        tree: Tree = self.query_one("#nav", Tree)
        tree.clear()
        tree.root.expand()
        if view == "operations":
            tree.root.set_label("operations")
            self._build_operation_tree(tree)
        elif view in ("tutorials", "tours"):
            tree.root.set_label("tutorials")
            self._build_tutorial_tree(tree)
        else:
            tree.root.set_label("subsystems")
            self._build_structure_tree(tree)

    def _build_tutorial_tree(self, tree: Tree) -> None:
        """Live guided exploration tutorials grouped by category."""
        from ..operations.tutorial import tutorials

        items = tutorials()
        tree.root.data = Listing(
            "tutorials",
            "Live guided tutorials driving through running kernel structures with commentary.",
            tuple(items),
        )
        members: dict[str, list] = {}
        for item in items:
            members.setdefault(item.category, []).append(item)

        for category, group_items in members.items():
            branch = tree.root.add(
                category,
                expand=True,
                data=Listing(
                    category,
                    f"Live guided tutorials in the {category} category.",
                    tuple(group_items),
                ),
            )
            for item in group_items:
                branch.add_leaf(item.label, data=item)

    _build_tour_tree = _build_tutorial_tree

    def _build_operation_tree(self, tree: Tree) -> None:
        """Both kinds of entry, grouped by the subsystem they belong to.

        A step sequence and a single-moment analysis render differently but are
        both about one operation, so they belong in one list rather than two
        tabs.
        """
        items = list(WALKTHROUGHS) + [
            item for item in algorithms() if self.context.live or not item.background
        ]
        tree.root.data = Listing(
            "operations",
            "Kernel execution paths, scheduling analyses, and controlled experiments.",
            tuple(items),
        )
        # Grouped in one pass. The branch for a subsystem has to name every
        # member when it is created, so collect them before building any of it
        # rather than rescanning the list once per item.
        members: dict[str, list] = {}
        for item in items:
            members.setdefault(item.subsystem, []).append(item)

        for subsystem_key, group_items in members.items():
            branch = tree.root.add(
                subsystem_key,
                expand=True,
                data=Listing(
                    subsystem_key,
                    f"Operations associated with the {subsystem_key} subsystem.",
                    tuple(group_items),
                ),
            )
            for item in group_items:
                branch.add_leaf(item.label, data=item)

    def _build_structure_tree(self, tree: Tree) -> None:
        all_subsystems = subsystems()
        visible_subsystems = []
        for subsystem in all_subsystems:
            entries = [
                entry
                for entry in subsystem.entries
                if self.context.live or not isinstance(entry, Measurement)
            ]
            if entries:
                visible_subsystems.append(replace(subsystem, entries=entries))
        # A subsystem with a parent goes under its parent's branch, after the
        # parent's own entries. One whose parent is not shown stays at the top.
        shown = {subsystem.key for subsystem in visible_subsystems}
        top = [s for s in visible_subsystems if s.parent not in shown]
        tree.root.data = Listing(
            "subsystems",
            "Kernel subsystems with registered structure entry points.",
            tuple(top),
        )
        branches: dict[str, object] = {}
        for subsystem in visible_subsystems:
            entries = subsystem.entries
            under = branches.get(subsystem.parent, tree.root)
            branch = under.add(subsystem.label, data=subsystem, expand=True)
            branches[subsystem.key] = branch
            groups: dict[str, object] = {}
            for entry in entries:
                parent = branch
                group = getattr(entry, "group", "")
                if group:
                    if group not in groups:
                        members = tuple(
                            e for e in entries
                            if getattr(e, "group", "") == group
                        )
                        groups[group] = branch.add(
                            group,
                            expand=False,
                            data=Listing(
                                f"{subsystem.label} > {group}",
                                GROUP_DOCS.get(group, f"The {group} entries."),
                                members,
                            ),
                        )
                    parent = groups[group]
                parent.add_leaf(entry.label, data=entry)

    @staticmethod
    def _subsystem_of(node) -> str:
        """Walk up the tree to the subsystem this entry sits under."""
        current = node
        while current is not None:
            if isinstance(getattr(current, "data", None), Subsystem):
                return current.data.key
            current = current.parent
        return ""

    # ------------------------------------------------------------ guided tutorials

    def start_tutorial(self, tutorial: GuidedTutorial) -> None:
        """Start a guided tutorial in driver mode on the tutorial landing page."""
        steps = tutorial.steps(self.context.prog)
        if not steps:
            self.notify("No steps available for this tutorial on this kernel", severity="warning")
            return
        self.active_tutorial = TutorialSession(tutorial=tutorial, steps=steps, current_idx=0)
        self.refresh_bindings()
        self._show_tutorial_step(0)

    start_tour = start_tutorial

    def _show_tutorial_step(self, step_idx: int) -> None:
        """Render the landing page (step 0) or real Frame for the tutorial step (steps 1..N)."""
        if self.active_tutorial is None:
            return

        banner = self.query_one(TutorialBanner)
        landing = self.query_one(TutorialLanding)
        table = self.query_one("#fields", DataTable)
        doc = self.query_one("#doc", Static)
        hint = self.query_one("#hint", Static)

        if step_idx == 0:
            if hasattr(self, "_navigator"):
                self._navigator.cancel()
            self._stop_blink_timer()
            self.active_tutorial.current_idx = 0
            self._tutorial_action_idx = None
            self._tutorial_action_clean_name = ""
            self._tutorial_action_target = ""
            self._tutorial_action_is_final = False
            banner.display = False
            table.display = False
            doc.display = False
            hint.update("")
            landing.update_tutorial(self.active_tutorial.tutorial, self.active_tutorial.steps, is_active=True)
            self.query_one("#path", Static).display = True
            self.query_one("#path", Static).update(
                f"tutorial › {self.active_tutorial.tutorial.category} › {self.active_tutorial.tutorial.label} (Selected · Press Enter to Begin)"
            )
            nav = self.query_one("#nav", Tree)
            if not nav.has_focus:
                landing.focus()
            self.refresh_bindings()
            return

        self.query_one("#path", Static).display = False

        real_step_idx = step_idx - 1
        if not (0 <= real_step_idx < len(self.active_tutorial.steps)):
            return

        self.active_tutorial.current_idx = step_idx
        step = self.active_tutorial.steps[real_step_idx]

        # Progressive stack: load/cache frame for each step in order to show connected flow
        if step_idx not in self.active_tutorial.step_frames:
            frame = frames.tutorial_step_frame(self.context, step)
            frame.load()
            self.active_tutorial.step_frames[step_idx] = frame

        self.stack = [self.active_tutorial.step_frames[i] for i in range(1, step_idx + 1) if i in self.active_tutorial.step_frames]
        self.filter = ""
        self.set_activity("")

        # Position cursor on action field if present, or on first value field
        action_needle = (
            step.get_action_field()
            if hasattr(step, "get_action_field")
            else (step.action_field or step.highlight_field)
        )
        val_needles = (
            step.get_value_fields()
            if hasattr(step, "get_value_fields")
            else step.value_fields
        )
        target_idx = None
        action_idx = None
        current_frame = self.stack[-1]

        if action_needle:
            for idx, row in enumerate(current_frame.rows):
                if row.name.startswith("→ ") and _matches_field(action_needle, row.name, row.display_name):
                    action_idx = idx
                    target_idx = idx
                    break
            if action_idx is None:
                for idx, row in enumerate(current_frame.rows):
                    if _matches_field(action_needle, row.name, row.display_name):
                        action_idx = idx
                        target_idx = idx
                        break

        if target_idx is None and val_needles:
            for idx, row in enumerate(current_frame.rows):
                if any(_matches_field(v, row.name, row.display_name) for v in val_needles):
                    target_idx = idx
                    break

        self._tutorial_action_idx = action_idx
        self.render_frame()

        landing.display = False
        table.display = True
        doc.display = False

        animating = False
        if target_idx is not None:
            if self._should_animate_cursor() and target_idx > 0:
                animating = True
                self._auto_seconds_remaining = self.auto_dwell_travel_settled
                self._navigator.navigate(table, start_row=0, target_row=target_idx, total_duration=1.1)
            else:
                self._auto_seconds_remaining = self.auto_dwell_already_in_place
                table.move_cursor(row=target_idx, animate=False)
                self.update_hint()
                self._ensure_blink_timer()
        else:
            self._auto_seconds_remaining = self.auto_dwell_already_in_place

        banner.update_step(
            self.active_tutorial.tutorial.label,
            step_idx,
            len(self.active_tutorial.steps),
            step,
            self.active_tutorial.steps,
            auto_play=self.auto_play,
            seconds_left=int(round(self._auto_seconds_remaining)) if self.auto_play else None,
            is_travelling=animating,
        )
        if not animating:
            self._ensure_blink_timer()
        table.focus()
        self.refresh_bindings()

    def _on_nav_step(self, row_idx: int) -> None:
        self.update_hint()

    def _on_nav_settled(self, row_idx: int) -> None:
        self.update_hint()
        # When the cursor arrives at its place, wait a full 3 seconds
        self._auto_seconds_remaining = self.auto_dwell_travel_settled
        self._ensure_blink_timer()
        if self.auto_play and self.active_tutorial is not None and self.active_tutorial.current_idx > 0:
            step_idx = self.active_tutorial.current_idx - 1
            if 0 <= step_idx < len(self.active_tutorial.steps):
                try:
                    banner = self.query_one(TutorialBanner)
                    banner.update_step(
                        self.active_tutorial.tutorial.label,
                        self.active_tutorial.current_idx,
                        len(self.active_tutorial.steps),
                        self.active_tutorial.steps[step_idx],
                        self.active_tutorial.steps,
                        auto_play=True,
                        seconds_left=int(round(self._auto_seconds_remaining)),
                        is_travelling=False,
                    )
                except Exception:
                    pass

    def _should_animate_cursor(self) -> bool:
        if self.animate_cursor is not None:
            return self.animate_cursor
        return not getattr(self, "is_headless", False)

    _show_tour_step = _show_tutorial_step

    def action_tutorial_next(self, from_auto: bool = False) -> None:
        """Advance to the next step of the active guided tutorial."""
        if hasattr(self, "_navigator") and self._navigator.is_active:
            self._navigator.cancel(snap_to_target=True)
        if not from_auto and self.auto_play:
            self._pause_auto()
        if self.active_tutorial is None:
            tree: Tree = self.query_one("#nav", Tree)
            node = getattr(tree, "cursor_node", None)
            data = getattr(node, "data", None)
            if node is not None and isinstance(data, (GuidedTutorial, GuidedTour)):
                self.start_tutorial(data)
                self._show_tutorial_step(1)
                return
            return
        if self.active_tutorial.current_idx < len(self.active_tutorial.steps):
            self._show_tutorial_step(self.active_tutorial.current_idx + 1)
        else:
            self.notify("Completed tutorial! Press Esc to exit or 'p' to go back.", severity="information")

    action_tour_next = action_tutorial_next

    def action_tutorial_prev(self) -> None:
        """Return to the previous step of the active guided tutorial (or landing page)."""
        if hasattr(self, "_navigator") and self._navigator.is_active:
            self._navigator.cancel(snap_to_target=True)
        if self.auto_play:
            self._pause_auto()
        if self.active_tutorial is None:
            return
        if self.active_tutorial.current_idx > 0:
            self._show_tutorial_step(self.active_tutorial.current_idx - 1)

    action_tour_prev = action_tutorial_prev

    def exit_tutorial(self) -> None:
        """Exit the active guided tutorial and hide the commentary banner and landing page."""
        if hasattr(self, "_navigator"):
            self._navigator.cancel()
        if self.auto_play:
            self._pause_auto(silent=True)
        self._stop_blink_timer()
        if self.active_tutorial is None:
            return
        self.active_tutorial = None
        self._tutorial_action_idx = None
        self._tutorial_action_clean_name = ""
        self._tutorial_action_target = ""
        try:
            self.query_one("#tutorial-callout", TutorialCallout).hide()
        except Exception:  # noqa: BLE001
            pass
        self._tutorial_action_is_final = False
        self.query_one(TutorialBanner).display = False
        self.query_one(TutorialLanding).display = False
        self.query_one("#fields", DataTable).display = True
        self.query_one("#doc", Static).display = True
        self.query_one("#path", Static).display = True
        self.refresh_bindings()
        self.notify("Exited tutorial")

    exit_tour = exit_tutorial

    def action_toggle_auto(self) -> None:
        """Toggle auto-play demo walkthrough mode."""
        if self.auto_play:
            self._pause_auto()
            return

        if self.active_tutorial is None:
            tree: Tree = self.query_one("#nav", Tree)
            node = getattr(tree, "cursor_node", None)
            data = getattr(node, "data", None)
            if node is not None and isinstance(data, (GuidedTutorial, GuidedTour)):
                self.start_tutorial(data)
            else:
                self.notify("Select a tutorial to auto-play", severity="warning")
                return

        if self.active_tutorial is not None:
            if self.active_tutorial.current_idx == 0:
                self._show_tutorial_step(1)
            self._start_auto()

    def _start_auto(self) -> None:
        self.auto_play = True
        is_active = hasattr(self, "_navigator") and self._navigator.is_active
        self._auto_seconds_remaining = (
            self.auto_dwell_travel_settled if is_active else self.auto_dwell_already_in_place
        )
        self._ensure_blink_timer()
        self.notify("▶ Auto-play started ('a' or navigation keys to pause)", severity="information")
        if self.active_tutorial is not None and self.active_tutorial.current_idx > 0:
            step_idx = self.active_tutorial.current_idx - 1
            if 0 <= step_idx < len(self.active_tutorial.steps):
                banner = self.query_one(TutorialBanner)
                banner.update_step(
                    self.active_tutorial.tutorial.label,
                    self.active_tutorial.current_idx,
                    len(self.active_tutorial.steps),
                    self.active_tutorial.steps[step_idx],
                    self.active_tutorial.steps,
                    auto_play=True,
                    seconds_left=int(round(self._auto_seconds_remaining)),
                    is_travelling=is_active,
                )
        self.refresh_bindings()

    def _pause_auto(self, silent: bool = False) -> None:
        if self.auto_play:
            self.auto_play = False
            if not silent:
                self.notify("⏸ Auto-play paused")
            if self.active_tutorial is not None and self.active_tutorial.current_idx > 0:
                step_idx = self.active_tutorial.current_idx - 1
                if 0 <= step_idx < len(self.active_tutorial.steps):
                    try:
                        banner = self.query_one(TutorialBanner)
                        banner.update_step(
                            self.active_tutorial.tutorial.label,
                            self.active_tutorial.current_idx,
                            len(self.active_tutorial.steps),
                            self.active_tutorial.steps[step_idx],
                            self.active_tutorial.steps,
                            auto_play=False,
                        )
                    except Exception:
                        pass
            self.refresh_bindings()

    def _ensure_blink_timer(self) -> None:
        if self._blink_timer is None:
            self._blink_timer = self.set_interval(self.blink_interval, self._tick_blink)

    def _stop_blink_timer(self) -> None:
        if self._blink_timer is not None:
            self._blink_timer.stop()
            self._blink_timer = None

    def _tick_blink(self) -> None:
        """Pulse the action row and update auto-play countdown if active."""
        if self.active_tutorial is None or self.active_tutorial.current_idx == 0:
            self._stop_blink_timer()
            return

        self._blink_phase = not self._blink_phase

        # 1. Pulse action row cell in DataTable
        if (
            self._tutorial_action_idx is not None
            and self._tutorial_action_clean_name
            and not getattr(self, "_tutorial_action_is_final", False)
        ):
            try:
                table = self.query_one("#fields", DataTable)
                cell_text = get_action_cell_text(
                    self._tutorial_action_clean_name,
                    variant=self.highlight_variant,
                    blink_phase=self._blink_phase,
                )
                table.update_cell_at(Coordinate(self._tutorial_action_idx, 0), cell_text)
            except Exception:
                pass

        # 2. If auto-play is active, only countdown AFTER the cursor has arrived at its place
        if self.auto_play:
            if hasattr(self, "_navigator") and self._navigator.is_active:
                return  # do not count down while cursor is still travelling to its destination
            self._auto_seconds_remaining = max(0.0, self._auto_seconds_remaining - self.blink_interval)
            if self._auto_seconds_remaining <= 0.0:
                self._auto_seconds_remaining = self.auto_dwell_time
                self._run_auto_step()
            else:
                try:
                    banner = self.query_one(TutorialBanner)
                    step_idx = self.active_tutorial.current_idx - 1
                    if 0 <= step_idx < len(self.active_tutorial.steps):
                        banner.update_step(
                            self.active_tutorial.tutorial.label,
                            self.active_tutorial.current_idx,
                            len(self.active_tutorial.steps),
                            self.active_tutorial.steps[step_idx],
                            self.active_tutorial.steps,
                            auto_play=True,
                            seconds_left=int(round(self._auto_seconds_remaining)),
                        )
                except Exception:
                    pass

    def _run_auto_step(self) -> None:
        if self.active_tutorial is None:
            self._pause_auto(silent=True)
            return
        if self.active_tutorial.current_idx < len(self.active_tutorial.steps):
            self.action_tutorial_next(from_auto=True)
            if self.active_tutorial.current_idx >= len(self.active_tutorial.steps):
                self._pause_auto(silent=True)
                self.notify("✓ Tutorial walkthrough completed!", severity="information")
        else:
            self._pause_auto(silent=True)

    def action_toggle_header_variant(self) -> None:
        """Toggle between 2-line minimal HUD and 3-line pipeline+insight header."""
        banner = self.query_one(TutorialBanner)
        banner.header_variant = 1 if banner.header_variant == 2 else 2
        mode = "Variant A (2 lines - Minimal HUD)" if banner.header_variant == 1 else "Variant B (3 lines - Pipeline + Insight)"
        self.notify(f"Header: {mode}")
        if self.active_tutorial and self.active_tutorial.current_idx > 0:
            step_idx = self.active_tutorial.current_idx - 1
            if 0 <= step_idx < len(self.active_tutorial.steps):
                banner.update_step(
                    self.active_tutorial.tutorial.label,
                    self.active_tutorial.current_idx,
                    len(self.active_tutorial.steps),
                    self.active_tutorial.steps[step_idx],
                    self.active_tutorial.steps,
                    auto_play=self.auto_play,
                )

    def action_toggle_highlight_style(self) -> None:
        """Toggle between Style 1 (Coach mark + Bulb) and Style 2 (Chevron + Pill)."""
        self.highlight_variant = 2 if self.highlight_variant == 1 else 1
        mode = "Style 1 (Coach mark + Bulb)" if self.highlight_variant == 1 else "Style 2 (Chevron + Pill)"
        self.notify(f"Highlight: {mode}")
        self.render_frame()

    exit_tour = exit_tutorial

    def action_expand_or_tutorial_next(self) -> None:
        """In a tutorial landing screen, advance to step 1; otherwise expand the row."""
        if self.active_tutorial is not None and self.active_tutorial.current_idx == 0:
            self.action_tutorial_next()
        else:
            self.action_expand()

    action_expand_or_tour_next = action_expand_or_tutorial_next

    # ------------------------------------------------------------ navigation

    def push(self, frame: Frame) -> None:
        self.cancel_preview()
        frame.load()
        self.stack.append(frame)
        self.filter = ""
        # Navigating invalidates any build still in flight, and with it the
        # line describing that build.
        self._token += 1
        self.set_activity("")
        self.render_frame()

    def _type_suffix(self, frame: Frame) -> str:
        """The C type this screen is made of, for the path line.

        A struct view takes it from the object it opened. A listing has no
        object of its own, so it takes it from the rows, which is what stops a
        table of pid/state/command reading as ps output: every row of it is a
        task_struct. Reported only when the rows agree, since a list of mixed
        types has no single answer.
        """
        if frame.obj is not None:
            return f"   {frame.obj.type_.type_name()}"
        types = {row.type_name for row in frame.rows if row.obj is not None}
        return f"   {types.pop()}" if len(types) == 1 else ""

    def _headers(self, columns: tuple[str, ...]) -> list[str]:
        """The column names, with an arrow on the one the rows are ordered by."""
        frame = self.stack[-1] if self.stack else None
        if frame is None or frame.sort_column is None:
            return list(columns)
        arrow = " ▼" if frame.sort_reverse else " ▲"
        return [
            f"{name}{arrow}" if index == frame.sort_column else name
            for index, name in enumerate(columns)
        ]

    def render_frame(self) -> None:
        table: DataTable = self.query_one("#fields", DataTable)
        columns = self.stack[-1].columns if self.stack else FIELD_COLUMNS
        # Rebuild the columns every time. DataTable.clear() keeps each column's
        # cached auto-width, so a wide value from the previous frame would still
        # be reserving space in this one.
        table.clear(columns=True)
        table.add_columns(*self._headers(columns))
        source_view = columns == SOURCE_COLUMNS
        table.fixed_columns = SOURCE_FIXED_COLUMNS if source_view else 0

        if not self.stack:
            self.query_one("#path", Static).update("select a subsystem")
            return

        frame = self.stack[-1]
        if self.active_tutorial is not None and self.active_tutorial.current_idx > 0:
            self.query_one("#path", Static).display = False
        else:
            self.query_one("#path", Static).display = True
            breadcrumb = " › ".join(f.label for f in self.stack)
            self.query_one("#path", Static).update(f"{breadcrumb}{self._type_suffix(frame)}")

        self.update_doc()

        # Lexed from every row, not the visible ones, so filtering a source
        # frame does not splice unrelated lines together for the lexer.
        source = _highlight_source(frame.rows) if source_view else {}
        limit = None if source_view else MAX_CELL

        width = len(columns)
        for index, row in enumerate(self.visible_rows()):
            if row.cells is not None:
                values = list(row.cells)[:width]
                if row.marked and values:
                    values[0] = f"{'  ' * row.depth}{row.marker} {values[0]}"
            else:
                values = [
                    f"{'  ' * row.depth}{row.marker} {row.display_name}",
                    row.type_name,
                    row.value,
                    row.placement,
                ][:width]
            values += [""] * (width - len(values))
            # Text, not str: a str cell is parsed as console markup, which eats
            # anything in square brackets ("[leader]", a "grep '\['" command).
            cells = [Text(_clip(v, limit)) for v in values]
            if row.name in source:
                cells[2] = source[row.name].copy()
            elif row.cells is None:
                # Only the field/type/value/placement layout is painted. A view
                # that supplies its own cells is a matrix of unrelated columns,
                # where colouring by position would mean nothing.
                style = KIND_STYLES.get(row.kind)
                if style:
                    cells[0].stylize(style)
                # The C type and the placement are reference detail. Dimming
                # them lets the name and the value carry the line.
                if row.kind == "field" and len(cells) > 1:
                    cells[1].stylize("dim")
                if len(cells) > 2:
                    _paint_value(cells[2])
                if len(cells) > 3:
                    cells[3].stylize("dim")
            # Colour the userspace command so it is obviously not a kernel path.
            if (
                self.userspace
                and len(cells) > 1
                and row.kind in ("link", "field")
                and row.type_name != _type_of(row)
            ):
                # Only colour cells that actually became a command: a field or
                # link with no equivalent keeps its type or its origin, and
                # should look normal.
                cells[1].stylize("cyan")
            # In an active tutorial step, apply dual highlighting: Action ⚡ vs Value 💡
            if self.active_tutorial is not None and self.active_tutorial.current_idx > 0:
                step_idx = self.active_tutorial.current_idx - 1
                if 0 <= step_idx < len(self.active_tutorial.steps):
                    step = self.active_tutorial.steps[step_idx]
                    action_needle = (
                        step.get_action_field()
                        if hasattr(step, "get_action_field")
                        else (step.action_field or step.highlight_field)
                    )
                    val_needles = (
                        step.get_value_fields()
                        if hasattr(step, "get_value_fields")
                        else step.value_fields
                    )
                    if self._tutorial_action_idx is not None:
                        is_action = (index == self._tutorial_action_idx)
                    else:
                        is_action = bool(action_needle and _matches_field(action_needle, row.name, row.display_name))
                    is_value = (not is_action) and any(_matches_field(v, row.name, row.display_name) for v in val_needles)
                    clean_name = values[0].strip()

                    if is_action:
                        is_final = (step_idx + 1 >= len(self.active_tutorial.steps))
                        next_target = ""
                        if not is_final:
                            next_step = self.active_tutorial.steps[step_idx + 1]
                            next_target = next_step.get_flow_label()
                        style_action_row(
                            clean_name,
                            values,
                            cells,
                            next_target,
                            is_final,
                            variant=self.highlight_variant,
                            blink_phase=self._blink_phase,
                        )
                        self._tutorial_action_clean_name = clean_name
                        self._tutorial_action_target = next_target
                        self._tutorial_action_is_final = is_final
                    elif is_value:
                        style_value_row(clean_name, values, cells, variant=self.highlight_variant)

            table.add_row(*cells, key=str(index))
        self.update_hint()
        self._place_callout()
        self.refresh_bindings()
        if source_view:
            target_idx = next(
                (i for i, row in enumerate(self.visible_rows()) if row.kind == "derived" or row.type_name == "▸"),
                None,
            )
            if target_idx is not None and table.row_count > target_idx:
                table.move_cursor(row=target_idx, animate=False)

    def _place_callout(self) -> None:
        """Put the step narration just under the row it is about.

        The anchor is the action row's position on screen, so the block follows
        the cursor when the table scrolls. Any failure hides it rather than
        leaving it stranded over unrelated rows.
        """
        try:
            callout = self.query_one("#tutorial-callout", TutorialCallout)
        except Exception:
            return
        step = None
        if self.active_tutorial is not None and self.active_tutorial.current_idx > 0:
            idx = self.active_tutorial.current_idx - 1
            if 0 <= idx < len(self.active_tutorial.steps):
                step = self.active_tutorial.steps[idx]
        if step is None or not step.commentary:
            callout.hide()
            return
        try:
            table = self.query_one("#fields", DataTable)
            detail = self.query_one("#detail")
            row_idx = self._tutorial_action_idx
            if row_idx is None:
                row_idx = table.cursor_row
            # Table rows start one line below its top once the header is drawn.
            header = 1 if table.show_header else 0
            row_y = (
                table.region.y
                - detail.region.y
                + header
                + (row_idx - table.scroll_offset.y)
            )
            height = callout.prepare(step.commentary, detail.region.width)
            if height == 0:
                callout.hide()
                return
            available = detail.region.height
            below = row_y + 1
            if below + height <= available:
                y = below
            else:
                # No room under the row: sit above it instead, which keeps the
                # block against the row it describes rather than off screen.
                y = row_y - height
            if y < 0 or y >= available:
                callout.hide()
                return
            callout.place(y)
        except Exception:  # noqa: BLE001
            callout.hide()

    def update_hint(self) -> None:
        """Show documentation for the row under the cursor."""
        row = self.current_row()
        if row is None:
            self.query_one("#hint", Static).update("")
            return
        doc = self.struct_doc(self.stack[-1].obj if self.stack else None)
        source = doc.members.get(row.name, "") if doc else ""

        hint_text = Text()
        if source:
            hint_text.append(source)
        if row.doc:
            if len(hint_text):
                hint_text.append("  ·  ")
            hint_text.append(row.doc)

        if self.active_tutorial is not None and self.active_tutorial.current_idx > 0:
            step_idx = self.active_tutorial.current_idx - 1
            if 0 <= step_idx < len(self.active_tutorial.steps):
                step = self.active_tutorial.steps[step_idx]
                action_field = (
                    step.get_action_field()
                    if hasattr(step, "get_action_field")
                    else (step.action_field or step.highlight_field)
                )
                val_fields = (
                    step.get_value_fields()
                    if hasattr(step, "get_value_fields")
                    else step.value_fields
                )

                if action_field and _matches_field(action_field, row.name, row.display_name):
                    next_target = ""
                    if step_idx + 1 < len(self.active_tutorial.steps):
                        next_step = self.active_tutorial.steps[step_idx + 1]
                        next_target = next_step.get_flow_label()
                    if len(hint_text):
                        hint_text.append("  ·  ")
                    if next_target:
                        hint_text.append(f"👉 Press [Enter] to follow flow into {next_target}", style="bold bright_yellow")
                    else:
                        hint_text.append("✓ Traversal complete on this structure", style="bold bright_green")
                elif any(_matches_field(v, row.name, row.display_name) for v in val_fields):
                    if len(hint_text):
                        hint_text.append("  ·  ")
                    hint_text.append("💡 Key data payoff value for this step", style="bold bright_cyan")

        self.query_one("#hint", Static).update(hint_text)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.update_hint()
        self._place_callout()
        self.refresh_bindings()

    def visible_rows(self) -> list[Row]:
        if not self.stack:
            return []
        rows = self.stack[-1].rows
        if not self.filter:
            return rows
        needle = self.filter.lower()
        return [
            r for r in rows if needle in r.name.lower() or needle in r.value.lower()
        ]

    def current_row(self) -> Row | None:
        table: DataTable = self.query_one("#fields", DataTable)
        rows = self.visible_rows()
        if not rows or table.cursor_row < 0 or table.cursor_row >= len(rows):
            return None
        return rows[table.cursor_row]

    # --------------------------------------------------------------- actions

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        self.open_node(event.node)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Fill the pane for the entry under the cursor, without enter.

        Scrolling the tree is how the catalog gets read, so the values belong
        on screen while it happens. Deferred by ``PREVIEW_DELAY`` so passing
        over an entry costs nothing; only the one the cursor stops on is built.
        """
        self.cancel_preview()
        node = event.node
        # This highlight is the sidebar catching up with the pane, not a
        # request to open anything.
        if node is self._synced_node:
            self._synced_node = None
            return
        self.refresh_bindings()
        if node.data is None:
            return
        self._preview_timer = self.set_timer(
            PREVIEW_DELAY, lambda: self.preview_node(node)
        )

    def sync_tree(self, item) -> None:
        """Move the sidebar cursor onto the item the pane just opened.

        Following an index row is the same navigation as picking that item in
        the tree, so the two should not disagree about where the user is. Any
        collapsed branch above the item is opened, since a node inside one
        occupies no line for the cursor to reach.
        """
        tree: Tree = self.query_one("#nav", Tree)
        node = next(
            (
                n for n in _descendants(tree.root)
                if n.data is item
                or (getattr(n.data, "key", None) and getattr(n.data, "key", None) == getattr(item, "key", None))
            ),
            None,
        )
        if node is None:
            return
        parent = node.parent
        while parent is not None:
            parent.expand()
            parent = parent.parent
        if node.line < 0:
            return
        self._synced_node = node
        self.cancel_preview()
        tree.move_cursor(node, animate=False)

    def cancel_preview(self) -> None:
        if self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None

    def preview_node(self, node) -> None:
        # During the delay the tree may have lost focus, or the graph screen may
        # have gone up and taken the sidebar with it. A frame arriving under
        # someone reading the pane is worse than no preview at all.
        nav = self.screen.query("#nav")
        if len(nav) == 1 and nav.first().has_focus:
            self.open_node(node, preview=True)

    def open_node(self, node, preview: bool = False) -> None:
        data = node.data
        if data is None:
            return
        if isinstance(data, (GuidedTutorial, GuidedTour)):
            if preview:
                steps = data.steps(self.context.prog)
                self.query_one(TutorialBanner).display = False
                self.query_one("#fields", DataTable).display = False
                self.query_one("#doc", Static).display = False
                self.query_one("#hint", Static).update("")
                landing = self.query_one(TutorialLanding)
                landing.update_tutorial(data, steps, is_active=False)
                self.query_one("#path", Static).display = True
                self.query_one("#path", Static).update(
                    f"tutorial › {data.category} › {data.label} (Press Enter to Launch)"
                )
                self.refresh_bindings()
            else:
                if (
                    self.active_tutorial is not None
                    and self.active_tutorial.tutorial.key == data.key
                    and self.active_tutorial.current_idx == 0
                ):
                    self._show_tutorial_step(1)
                else:
                    self.start_tutorial(data)
            return
        self.query_one(TutorialLanding).display = False
        self.query_one("#fields", DataTable).display = True
        self.query_one("#doc", Static).display = True
        if self.active_tutorial is not None:
            self.exit_tutorial()
        plan = frames.plan_for(
            data, self.context, self._subsystem_of(node), preview=preview
        )
        if plan is None:
            return
        self.stack.clear()
        self.open_plan(plan)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_follow()

    def action_follow(self) -> None:
        if hasattr(self, "_navigator") and self._navigator.is_active:
            self._navigator.cancel(snap_to_target=True)
        if self.active_tutorial is not None:
            if self.auto_play:
                self._pause_auto()
            if self.active_tutorial.current_idx == 0:
                self.action_tutorial_next()
                return
            real_idx = self.active_tutorial.current_idx - 1
            if 0 <= real_idx < len(self.active_tutorial.steps):
                step = self.active_tutorial.steps[real_idx]
                row = self.current_row()
                action_field = (
                    step.get_action_field()
                    if hasattr(step, "get_action_field")
                    else (step.action_field or step.highlight_field)
                )
                if row is not None:
                    table = self.query_one("#fields", DataTable)
                    current_cursor_row = table.cursor_row
                    is_last_step = (self.active_tutorial.current_idx == len(self.active_tutorial.steps))
                    if (
                        (self._tutorial_action_idx is not None and current_cursor_row == self._tutorial_action_idx)
                        or (action_field and _matches_field(action_field, row.name, row.display_name))
                    ):
                        if not is_last_step:
                            self.action_tutorial_next()
                            return
                        else:
                            self.action_expand()
                            return
        row = self.current_row()
        if row is None or not row.followable:
            if row is not None and (row.expand is not None or (row.obj is not None and hasattr(row, "children"))):
                self.action_expand()
                return
            self.bell()
            return

        # An index row stands for a catalog item rather than a value, and
        # opens it exactly as selecting it in the sidebar would.
        if row.item is not None:
            self.open_plan(frames.plan_for(row.item, self.context))
            self.sync_tree(row.item)
            return

        # A curated link may fan out to a list, so it brings its own expansion.
        if row.expand is not None:
            rows = row.expand()
            if len(rows) == 1 and rows[0].obj is not None:
                # Never use `or` on a drgn Object: a struct has no truth value
                # ("cannot convert 'struct foo' to bool").
                target = follow(rows[0].obj)
                if target is None:
                    # Legitimately empty -- e.g. a kernel thread's mm is NULL.
                    self.notify(f"{row.name} is NULL here", severity="warning")
                    return
                self.push(frames.object_frame(row.name, target, self.context, row.doc))
            else:
                self.push(
                    Frame(
                        row.name,
                        row.expand,
                        doc=row.doc,
                        columns=row.expand_columns or FIELD_COLUMNS,
                    )
                )
            return

        if row.obj is None:
            self.bell()
            return
        target = follow(row.obj)
        if target is None:
            self.notify("nothing to follow (NULL or unreadable)", severity="warning")
            return
        self.push(frames.object_frame(row.name, target, self.context))

    def action_expand(self) -> None:
        """Open the row under the cursor in place, keeping its neighbours visible.

        ``enter`` replaces the screen with what it followed, which is right for
        going somewhere and wrong for a one-field refcount. This splices the
        children in below the row instead, indented, and takes them out again
        on a second press. The expansion lives in the frame's row list, so a
        refresh or a re-entry rebuilds the frame closed.
        """
        if self.active_tutorial is not None and self.active_tutorial.current_idx == 0:
            if self.auto_play:
                self._pause_auto()
            self.action_tutorial_next()
            return

        landing = self.query_one(TutorialLanding)
        if landing.display:
            tree: Tree = self.query_one("#nav", Tree)
            node = getattr(tree, "cursor_node", None)
            data = getattr(node, "data", None)
            if node is not None and isinstance(data, (GuidedTutorial, GuidedTour)):
                self.start_tutorial(data)
                self._show_tutorial_step(1)
                return

        row = self.current_row()
        if row is None or not self.stack:
            self.bell()
            return

        rows = self.stack[-1].rows
        index = next((i for i, candidate in enumerate(rows) if candidate is row), None)
        if index is None:
            self.bell()
            return

        table: DataTable = self.query_one("#fields", DataTable)
        cursor = table.cursor_row

        # A Row is frozen, so opening one means replacing it with a copy that
        # says so, rather than flipping a flag on the row already in the list.
        if row.expanded:
            # Everything deeper than this row belongs to it, including whatever
            # its children have opened themselves.
            end = index + 1
            while end < len(rows) and rows[end].depth > row.depth:
                end += 1
            del rows[index + 1 : end]
            rows[index] = replace(row, expanded=False)
        else:
            children = row.children()
            if not children:
                self.notify(f"nothing to expand under {row.name}", severity="warning")
                return
            if row.name == "rss_stat":
                rss_names = {
                    0: "MM_FILEPAGES (file cache)",
                    1: "MM_ANONPAGES (heap/stack)",
                    2: "MM_SWAPENTS (swap entries)",
                    3: "MM_SHMEMPAGES (shared mem)",
                }
                children = [
                    replace(child, note=rss_names.get(i, child.note))
                    for i, child in enumerate(children)
                ]
            rows[index] = replace(row, expanded=True)
            rows[index + 1 : index + 1] = [
                replace(child, depth=row.depth + 1) for child in children
            ]

        self.render_frame()
        table.move_cursor(row=cursor)

    def action_sort(self) -> None:
        """Order the rows by the next column, and eventually by none of them.

        Cycling rather than pointing at a column, because the table's cursor
        selects a row: there is nothing on screen that says which column the
        user means. The header carries the arrow, so the state is visible even
        though the key that set it is not.
        """
        frame = self.stack[-1] if self.stack else None
        if frame is None or not any(
            row.marked and row.cells is not None for row in frame.rows
        ):
            self.notify("nothing to sort in this view", severity="warning")
            return

        current = frame.sort_column
        if current is None:
            frame.sort_column = 0
        elif current + 1 < len(frame.columns):
            frame.sort_column = current + 1
        else:
            frame.sort_column = None
        frame.sort_reverse = False
        self._resort(frame)

    def action_sort_reverse(self) -> None:
        """Flip the direction of the column already sorted on."""
        frame = self.stack[-1] if self.stack else None
        if frame is None or frame.sort_column is None:
            self.notify("press o to sort by a column first", severity="warning")
            return
        frame.sort_reverse = not frame.sort_reverse
        self._resort(frame)

    def _resort(self, frame: Frame) -> None:
        frame.load()
        self.filter = ""
        self.render_frame()
        if frame.sort_column is None:
            self.notify("unsorted: back to the order the walk produced")
        else:
            direction = "descending" if frame.sort_reverse else "ascending"
            self.notify(f"sorted by {frame.columns[frame.sort_column]}, {direction}")

    def action_back(self) -> None:
        if self._resume_graph():
            return
        if len(self.stack) > 1:
            self.stack.pop()
            self.filter = ""
            self._token += 1
            self.render_frame()
            return
        if self.active_tutorial is not None:
            if self.active_tutorial.current_idx > 0:
                self.action_tutorial_prev()
            else:
                self.exit_tutorial()
            return

    def action_userspace(self) -> None:
        """Swap the origin column for how to get the same thing from userspace."""
        if not self.context.live:
            self.notify("userspace commands are unavailable for a vmcore", severity="warning")
            return
        self.context.userspace = not self.context.userspace
        if self.stack:
            self.stack[-1].load()
            self.render_frame()
        self.notify(
            "showing userspace equivalents" if self.userspace else "showing kernel origins"
        )

    def action_cycle_view(self) -> None:
        """Cycle between structures, operations, and tutorials views."""
        tabs = self.query_one("#views", Tabs)
        order = ["view-structures", "view-operations", "view-tutorials"]
        current = tabs.active or "view-structures"
        if current == "view-tours":
            current = "view-tutorials"
        next_idx = (order.index(current) + 1) % len(order) if current in order else 0
        next_tab_id = order[next_idx]
        tabs.active = next_tab_id
        view = next_tab_id.removeprefix("view-")
        self.build_tree(view)
        self.refresh_bindings()

    def action_refresh(self) -> None:
        """Re-read the current frame from live memory."""
        if not self.stack:
            return
        self.stack[-1].load()
        self.render_frame()
        self.notify("re-read from live kernel")

    def action_copy(self) -> None:
        """Copy the current value, command, link, or item under cursor to clipboard."""
        if self._landing_displayed():
            tutorial = None
            if self.active_tutorial is not None:
                tutorial = self.active_tutorial.tutorial
            else:
                try:
                    tree = self.query_one("#nav", Tree)
                    node = getattr(tree, "cursor_node", None)
                    if node and isinstance(getattr(node, "data", None), (GuidedTutorial, GuidedTour)):
                        tutorial = node.data
                except Exception:
                    pass
            if tutorial is not None:
                video_url = getattr(tutorial, "video_url", "")
                if video_url:
                    copy_to_system_clipboard(video_url, self)
                    self.notify(f"Copied video URL: {video_url}", title="Clipboard", timeout=3.0, markup=False)
                    return
                summary = f"{tutorial.label}\n{tutorial.doc}"
                copy_to_system_clipboard(summary, self)
                self.notify(f"Copied tutorial overview: {tutorial.label}", title="Clipboard", timeout=3.0, markup=False)
                return

        # If sidebar tree is focused, copy the tree item label
        try:
            tree = self.query_one("#nav", Tree)
            if tree.has_focus:
                node = tree.cursor_node
                if node is not None:
                    text = str(node.label)
                    copy_to_system_clipboard(text, self)
                    self.notify(f"Copied item: {text}", title="Clipboard", timeout=2.5, markup=False)
                    return
        except Exception:
            pass

        # In userspace mode, prefer copying the userspace command under cursor
        if self.userspace:
            cmd = self.command_under_cursor()
            if cmd:
                copy_to_system_clipboard(cmd, self)
                self.notify(f"Copied command: {cmd}", title="Clipboard", timeout=2.5, markup=False)
                return

        row = self.current_row()
        if row is None:
            self.notify("No item selected to copy", title="Clipboard", severity="warning", timeout=2.0)
            return

        # Choose the most relevant text from the row
        if row.value:
            text = row.value
        elif row.name:
            text = row.name
        elif row.cells:
            text = "  ".join(str(c) for c in row.cells if str(c).strip())
        else:
            text = ""

        if not text:
            self.notify("Current row is empty", title="Clipboard", severity="warning", timeout=2.0)
            return

        copy_to_system_clipboard(text, self)
        preview = _clip(text.replace("\n", " "), 50)
        self.notify(f"Copied value: {preview}", title="Clipboard", timeout=2.5, markup=False)

    def action_copy_row(self) -> None:
        """Copy the entire formatted row under the cursor to clipboard."""
        row = self.current_row()
        if row is None:
            self.notify("No row selected to copy", title="Clipboard", severity="warning", timeout=2.0)
            return

        if row.cells:
            row_text = "\t".join(str(c).strip() for c in row.cells)
        else:
            parts = [row.name]
            if row.type_name:
                parts.append(row.type_name)
            if row.value:
                parts.append(row.value)
            if row.note:
                parts.append(row.note)
            row_text = "\t".join(parts)

        copy_to_system_clipboard(row_text, self)
        preview = _clip(row_text.replace("\t", "  │  "), 60)
        self.notify(f"Copied row: {preview}", title="Clipboard", timeout=2.5, markup=False)

    def action_copy_narration(self) -> None:
        """Copy the current step's narration to the system clipboard.

        ``c`` and ``C`` copy the row under the cursor. The narration is drawn by
        TutorialCallout on its own layer rather than as rows, so neither of them
        reaches it, and terminal selection is otherwise the only route to it.
        """
        step = None
        if self.active_tutorial is not None and self.active_tutorial.current_idx > 0:
            idx = self.active_tutorial.current_idx - 1
            if 0 <= idx < len(self.active_tutorial.steps):
                step = self.active_tutorial.steps[idx]
        if step is None or not step.commentary:
            self.notify("no step narration here", severity="warning")
            return
        text = " ".join(step.commentary.split())
        if copy_to_system_clipboard(text, self):
            self.notify(f"narration copied ({len(text)} chars)")
        else:
            self.notify("could not reach the clipboard", severity="warning")

    def action_toggle_mouse(self) -> None:
        """Toggle between TUI mouse capture and native terminal text selection."""
        driver = getattr(self, "_driver", None)
        self.mouse_tracking = not self.mouse_tracking
        if self.mouse_tracking:
            if driver is not None and hasattr(driver, "_enable_mouse_support"):
                try:
                    driver._enable_mouse_support()
                except Exception:
                    pass
            self.notify(
                "Mouse: TUI scrolling & clicking enabled (Tip: Hold Option/Shift to select text)",
                title="Mouse Mode",
                timeout=3.5,
                markup=False,
            )
        else:
            if driver is not None and hasattr(driver, "_disable_mouse_support"):
                try:
                    driver._disable_mouse_support()
                except Exception:
                    pass
            self.notify(
                "Mouse: Terminal text selection enabled (drag to select & copy). Press 'm' to restore TUI mouse.",
                title="Mouse Mode",
                timeout=4.0,
                markup=False,
            )

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.display = True
        search.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter = event.value
        self.render_frame()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        search = self.query_one("#search", Input)
        search.display = False
        self.query_one("#fields", DataTable).focus()

    def action_source(self) -> None:
        """Show the kernel source for whatever is under the cursor.

        Works on a struct field (its declaration) and on a walkthrough step
        (the function), since a step already carries its file:line.
        """
        if not self._source_available:
            self.notify("no kernel source for this build", severity="warning")
            return
        if self._showing_source():
            return
        row = self.current_row()

        # A walkthrough step carries "file:line" in its source column.
        if row is not None and ":" in row.type_name:
            path, _, line = row.type_name.rpartition(":")
            if line.isdigit():
                self.open_source(path, int(line), row.name)
                return

        # A listing frame (every task, every device) has no object of its own,
        # so take the row under the cursor: its type is what "s" should show.
        frame_obj = self.stack[-1].obj if self.stack else None
        if frame_obj is None and row is not None:
            frame_obj = row.obj
        doc = self.struct_doc(frame_obj)
        if doc is None:
            # The worker that reads this struct's source is still running.
            # Record what was asked for, so _struct_doc_done opens it once the
            # worker returns. Without this the notification is all that
            # happens and the user has to press s again.
            aggregate = ct.struct_type(frame_obj.type_) if frame_obj is not None else None
            tag = aggregate.tag if aggregate is not None else None
            if tag:
                self._pending_source = (tag, row.name if row is not None else "")
                # Say nothing yet. A cached lookup returns in well under a
                # second and opens the view itself, and a notification about
                # fetching would still be on screen underneath it. Only a
                # fetch that is actually slow is worth reporting.
                self.set_timer(0.4, lambda: self._still_fetching(tag))
            else:
                self.notify("no structure here to show source for",
                            severity="warning")
            return
        if not doc.decl_file:
            self.notify("no source available here", severity="warning")
            return

        line = doc.decl_line
        title = f"struct {doc.tag}"
        if row is not None and row.name in doc.member_lines:
            line = doc.member_lines[row.name]
            title = f"struct {doc.tag}.{row.name}"
        self.open_source(doc.decl_file, line, title)

    def open_source(self, path: str, line: int, title: str) -> None:
        """Open a kernel source file, fetching it through debuginfod if needed."""
        self.open_plan(frames.source_plan(self.context, path, line, title))

    def command_under_cursor(self) -> str:
        """The userspace command the cursor is on, if it is on one.

        Two places carry one: a field or link row in userspace mode, where the
        command replaced the type and the type moved to ``original_type``, and
        an entry frame, where it is in the doc line. Anything else, including a
        type column that is still a type, is not a command.
        """
        row = self.current_row()
        if row is not None and row.original_type and row.type_name:
            return row.type_name
        doc = self.stack[-1].doc if self.stack else ""
        prefix = "from userspace:"
        if doc.startswith(prefix):
            return doc[len(prefix) :].strip()
        return ""

    def action_trace_command(self) -> None:
        """Trace the command under the cursor into the kernel that serves it.

        A pushed frame rather than a dialog: the result is a table of rows, its
        stack frames carry file:line, and "s" opens the source of any of them.
        A modal would end that chain at the first screen.
        """
        if not self.context.live:
            self.notify("command tracing is unavailable for a vmcore", severity="warning")
            return
        command = self.command_under_cursor()
        if not command:
            self.notify(
                "no command on this row: press u, then select a row that "
                "shows one",
                severity="warning",
            )
            return
        # A cell may show alternatives and a note; only one of them runs.
        command = runnable(command)
        if command in UNFILLED.values():
            self.notify(f"nothing to run here: {command}", severity="warning")
            return
        # A command that names no file is not a dead end: the trace measures
        # which one it read. Only it can say, so nothing is refused here.
        row = self.current_row()
        parent = self.stack[-1].obj if self.stack else None
        tag = ct.tag_of(parent.type_) if parent is not None else None
        selected_field = f"{tag}.{row.name}" if tag and row and row.kind == "field" else ""
        self.open_plan(
            frames.command_trace_plan(
                self.context,
                command,
                served_by(command),
                origin=row.name if row is not None else "",
                selected_field=selected_field,
            )
        )

    def action_repl(self) -> None:
        """Suspend the TUI and hand the current object to a drgn REPL."""
        row = self.current_row()
        frame = self.stack[-1] if self.stack else None
        obj = row.obj if row and row.obj is not None else (frame.obj if frame else None)

        namespace: dict = {"prog": self.prog, "drgn": drgn, "obj": obj}
        exec("from drgn import *", namespace)
        exec("from drgn.helpers.linux import *", namespace)

        banner = (
            "drgn REPL -- 'prog' is the kernel, 'obj' is the row under the cursor.\n"
            "Ctrl-D returns to the explorer.\n"
            f"obj = {obj.type_.type_name() if obj is not None else 'None'}"
        )
        with self.suspend():
            code.interact(banner=banner, local=namespace, exitmsg="")

    # ------------------------------------------------------------ focus mode

    def action_graph(self) -> None:
        """Focus mode: this entity's neighbourhood as a picture.

        Prefers the frame's own object -- pressing g while looking at a task
        graphs that task -- and falls back to the row under the cursor, which
        is what you want in a list frame, where the frame itself is not one
        struct.
        """
        frame = self.stack[-1] if self.stack else None
        obj = frame.obj if frame is not None else None
        label = frame.label if frame is not None else ""
        if obj is None:
            row = self.current_row()
            if row is not None and row.obj is not None:
                obj, label = row.obj, row.name
        if obj is None or follow(obj) is None:
            self.notify("nothing here to graph", severity="warning")
            return
        target = follow(obj)

        # Reopen the graph you left, rather than a fresh one, when this is the
        # struct you stepped out of it to look at -- or its centre. Anywhere
        # else, g means "graph this", which is a new picture.
        state = self.graph_state
        if state is not None and graph_key(target) not in state["returns"]:
            state = None
        self.push_screen(GraphScreen(self, target, label, state))

    def action_graph_back(self) -> None:
        self._resume_graph()

    def action_escape(self) -> None:
        """Leave the filter if one is open, otherwise go back a step.

        Two things can be "where I am": a filter narrowing the frame, and the
        frame itself. Escape drops the innermost one, so it never navigates
        away from a frame the user was still filtering.
        """
        search = self.query_one("#search", Input)
        if search.display:
            search.value = ""
            search.display = False
            self.filter = ""
            self.query_one("#fields", DataTable).focus()
            self.render_frame()
            return
        if self.active_tutorial is not None:
            self.exit_tutorial()
            return
        self.action_back()

    def open_object(self, label: str, obj: Object, doc: str = "") -> None:
        """Push a struct's field view. Used by focus mode on its way out."""
        self.push(frames.object_frame(label, obj, self.context, doc))
        self._mark_graph_return()

    def open_rows(self, label: str, make_rows, doc: str = "") -> None:
        """Push a list view built by someone else's expander."""
        self.push(Frame(label, make_rows, doc=doc))
        self._mark_graph_return()

    def _mark_graph_return(self) -> None:
        """Note that this frame was opened from the graph.

        Backing out of a frame should undo whatever opened it. This one was
        opened by leaving the graph, so backing out of it belongs in the
        graph, not in the table frame underneath.
        """
        if self.graph_state is not None:
            self.graph_state["return_depth"] = len(self.stack)

    def _resume_graph(self) -> bool:
        """Drop the frame the graph handed off to, and reopen the graph."""
        state = self.graph_state
        if state is None or state.get("return_depth") != len(self.stack):
            return False

        self.stack.pop()
        self.filter = ""
        self._token += 1
        self.render_frame()
        state = dict(state)
        state.pop("return_depth", None)
        obj, label = state["history"][-1]
        self.push_screen(GraphScreen(self, obj, label, state))
        return True
