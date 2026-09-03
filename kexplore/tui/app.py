"""Textual frontend.

Runs in the same process as drgn, so the model is the live ``drgn.Object``
itself and there is no serialization layer. The ``:`` binding suspends the UI
and opens a drgn REPL with the object under the cursor bound to ``obj``.

What a frame *contains* is decided in ``view.frames``; this module decides when
to build one, which one is on screen, and what the keys do to it.
"""

from __future__ import annotations

import code
from dataclasses import replace

import drgn
from drgn import Object, Program
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, Static, Tab, Tabs, Tree

from ..catalog.registry import Subsystem, subsystems
from ..core import ctypes as ct
from ..core.nav import Row, follow
from ..core.source import KernelSource, StructDoc
from ..operations.algorithm import algorithms
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
from .graph import GraphScreen, graph_key

# Textual sizes a column to its widest cell, so a single long value pushes the
# remaining columns off screen. Truncate for display; the hint line still shows
# the row's full text.
MAX_CELL = 46

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
    return {row.name: line for row, line in zip(rows, highlighted.split("\n"))}


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

    BINDINGS = [Binding("enter", "select_cursor", "follow")]


class NavTree(Tree):
    BINDINGS = [Binding("enter", "select_cursor", "open")]


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
        Binding("s", "source", "source"),
        Binding("g", "graph", "graph"),
        Binding("colon", "repl", "drgn repl"),
        # Escape undoes whatever is most local: the filter box, then the same
        # step backspace takes. Hidden from the footer, which already shows one.
        Binding("escape", "escape", "back", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, prog: Program) -> None:
        super().__init__()
        self.prog = prog
        self.context = Context(prog, KernelSource())
        self.stack: list[Frame] = []
        self.filter = ""
        self._docs: dict[str, StructDoc] = {}
        # Bumped by every navigation. A frame being built in a worker carries
        # the token it started with, so a result that arrives after the user
        # moved on is dropped instead of overwriting whatever is on screen now.
        self._token = 0
        # The last graph's shape, so returning to it from a detail view does
        # not throw away the branches the user opened.
        self.graph_state: dict | None = None
        # Pending sidebar preview, cancelled by the next cursor move and by any
        # navigation that would otherwise be overwritten when it fires.
        self._preview_timer = None
        # A node the sidebar cursor was moved onto by the pane rather than by
        # the user, whose highlight must not be answered with a preview.
        self._synced_node = None

    @property
    def source(self) -> KernelSource:
        return self.context.source

    @property
    def userspace(self) -> bool:
        return self.context.userspace

    # ------------------------------------------------------------ struct docs

    def struct_doc(self, obj: Object | None) -> StructDoc | None:
        """Kernel source comments for ``obj``'s type, fetched once per tag.

        Never blocks: recovering these runs pahole over a ~700MB vmlinux and
        then pulls the source file through debuginfod, so the first request for
        a tag starts a worker and returns ``None``. The doc and hint lines are
        rewritten when it lands.
        """
        if obj is None:
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
            try:
                doc = self.source.document(tag, members)
            except Exception as exc:  # noqa: BLE001 - a bad tag shouldn't kill the UI
                doc = StructDoc(tag, error=f"{type(exc).__name__}: {exc}")
            self.call_from_thread(self._struct_doc_done, tag, doc)

        self.run_worker(work, thread=True, group=f"doc:{tag}")

    def _struct_doc_done(self, tag: str, doc: StructDoc | None) -> None:
        # A failed lookup still gets recorded, so it is attempted once per tag
        # rather than on every repaint.
        self._docs[tag] = doc if doc is not None else StructDoc(tag, error="unavailable")
        self.set_activity("")
        self.update_doc()
        self.update_hint()

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
        yield Static("select a subsystem", id="path")
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Tabs(
                    Tab("structures", id="view-structures"),
                    Tab("operations", id="view-operations"),
                    id="views",
                )
                yield NavTree("subsystems", id="nav")
            with Vertical(id="detail"):
                yield Static("", id="doc")
                yield Static("", id="activity")
                yield FieldsTable(id="fields", cursor_type="row", zebra_stripes=True)
                yield Static("", id="hint")
        yield Input(placeholder="filter fields…", id="search")
        yield Footer()

    def on_mount(self) -> None:
        self.build_tree("structures")
        self.query_one("#search", Input).display = False
        self.query_one("#activity", Static).display = False
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
        if view:
            self.build_tree(view)

    def build_tree(self, view: str) -> None:
        tree: Tree = self.query_one("#nav", Tree)
        tree.clear()
        tree.root.expand()
        if view == "operations":
            self._build_operation_tree(tree)
        else:
            self._build_structure_tree(tree)

    def _build_operation_tree(self, tree: Tree) -> None:
        """Both kinds of entry, grouped by the subsystem they belong to.

        A step sequence and a single-moment analysis render differently but are
        both about one operation, so they belong in one list rather than two
        tabs.
        """
        items = list(WALKTHROUGHS) + algorithms()
        tree.root.data = Listing(
            "operations",
            "Sequences the kernel performs, and analyses of one moment in it.",
            tuple(items),
        )
        groups: dict[str, object] = {}

        def group(name: str, members) -> object:
            if name not in groups:
                groups[name] = tree.root.add(
                    name,
                    expand=True,
                    data=Listing(name, f"Operations belonging to {name}.", members),
                )
            return groups[name]

        for item in items:
            members = tuple(i for i in items if i.subsystem == item.subsystem)
            group(item.subsystem, members).add_leaf(item.label, data=item)

    def _build_structure_tree(self, tree: Tree) -> None:
        all_subsystems = subsystems()
        tree.root.data = Listing(
            "subsystems",
            "The parts of the kernel this tool has entry points into.",
            tuple(all_subsystems),
        )
        for subsystem in all_subsystems:
            branch = tree.root.add(subsystem.label, data=subsystem, expand=True)
            groups: dict[str, object] = {}
            for entry in subsystem.entries:
                parent = branch
                group = getattr(entry, "group", "")
                if group:
                    if group not in groups:
                        members = tuple(
                            e for e in subsystem.entries
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
            # Colour the userspace command so it is obviously not a kernel path.
            if self.userspace and len(cells) > 1 and row.kind in ("link", "field"):
                # Only colour cells that actually became a command: an
                # untranslatable field keeps its C type and should look normal.
                if row.kind == "link" or row.type_name != _type_of(row):
                    cells[1].stylize("cyan")
            table.add_row(*cells, key=str(index))
        self.update_hint()

    def update_hint(self) -> None:
        """Show documentation for the row under the cursor."""
        row = self.current_row()
        if row is None:
            self.query_one("#hint", Static).update("")
            return
        doc = self.struct_doc(self.stack[-1].obj if self.stack else None)
        source = doc.members.get(row.name, "") if doc else ""
        # Kernel source comment first, then whatever the decoder/link explains.
        parts = [p for p in (source, row.doc) if p]
        self.query_one("#hint", Static).update("  ·  ".join(parts))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.update_hint()

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
        node = next((n for n in _descendants(tree.root) if n.data is item), None)
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
        row = self.current_row()
        if row is None or not row.followable:
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

    def action_userspace(self) -> None:
        """Swap the origin column for how to get the same thing from userspace."""
        self.context.userspace = not self.context.userspace
        if self.stack:
            self.stack[-1].load()
            self.render_frame()
        self.notify(
            "showing userspace equivalents" if self.userspace else "showing kernel origins"
        )

    def action_refresh(self) -> None:
        """Re-read the current frame from live memory."""
        if not self.stack:
            return
        self.stack[-1].load()
        self.render_frame()
        self.notify("re-read from live kernel")

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
        row = self.current_row()

        # A walkthrough step carries "file:line" in its source column.
        if row is not None and ":" in row.type_name:
            path, _, line = row.type_name.rpartition(":")
            if line.isdigit():
                self.open_source(path, int(line), row.name)
                return

        doc = self.struct_doc(self.stack[-1].obj if self.stack else None)
        if doc is None:
            self.notify("still reading the source for this struct…")
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
