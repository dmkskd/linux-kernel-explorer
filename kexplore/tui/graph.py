"""Focus mode: the neighbourhood of one object, drawn as a graph.

The field table lists the members of a struct. This view shows the objects a
struct leads to and the operations that reach them. Each box is a struct, and
the line between two boxes is labelled with the link's name and, below it, the
operation that traverses it, for example ``task->mm``,
``task_rq() = cpu_rq(task_cpu(task))`` or ``walks task->mm->mm_mt``.

Edges come from the curated ``LINKS`` table that the ``→`` rows in the table
view also follow, so the graph shows the same relationships as the table
view.
"""

from __future__ import annotations

from collections.abc import Callable

from drgn import Object, TypeKind
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import ScrollableContainer
from textual.screen import Screen
from textual.widgets import Footer, Static

from ..catalog.format import task_comm
from ..catalog.links import links_for
from ..core import ctypes as ct
from ..core.graph import (
    BOX_HEIGHT,
    COL_GAP,
    Edge,
    Graph,
    Node,
    draw,
    layout,
    lines,
)
from ..core.nav import collect, collection_rows
from .clipboard import copy_to_system_clipboard

# Members of a collection counted before the count is shown as "many".
# Counting is fast for most links, but counting a VMA's resident pages walks
# the page tables, so the count is bounded.
COUNT_LIMIT = 500

# Members of an expanded collection drawn as boxes. A task's 156 open files
# would push the selected branch off screen, so the remaining members are
# grouped in a "… n more" box that opens as a list.
MEMBER_LIMIT = 12

def _palette(app) -> dict[str, str]:
    """Concrete colours for the canvas, from the active theme.

    The canvas is a rich ``Text``, not a styled widget tree, so it cannot use
    the ``$text-muted`` design tokens the stylesheet uses: rich parses style
    strings itself and does not recognise them. The colours are resolved here
    from the theme instead of being hardcoded, so the view follows the theme.
    """
    theme = app.current_theme
    foreground = theme.foreground or "#e0e0e0"
    background = theme.background or ("#101010" if theme.dark else "#f0f0f0")
    front, back = Color.parse(foreground), Color.parse(background)
    muted = front.blend(back, 0.40).hex
    faint = front.blend(back, 0.62).hex
    accent = theme.accent or foreground
    return {
        "root": f"bold {foreground}",
        "object": faint,
        "fanout": faint,
        "more": faint,
        "error": theme.error or foreground,
        "selected": f"bold {accent}",
        "edge": muted,
        "path": foreground,
        "op": f"italic {faint}",
        "op-selected": f"italic {accent}",
    }


def _identity(obj: Object) -> tuple[str, str, str]:
    """A box's label: the type, an identifier, and detail.

    The identifier is the address by default, because most structs have no
    shorter unambiguous identifier. A task is identified by pid and command
    instead, for example "611 auditd".

    Kernel addresses are shown by their low digits. Every address on screen
    shares the ``0xffff...`` prefix, which distinguishes none of them and would
    take a third of the box's width; the full value is shown in the info
    line.
    """
    tag = ct.tag_of(obj.type_) or ct.type_name(obj.type_)
    address = ct.safe(lambda: obj.value_(), 0)
    if not address:
        return tag, "?", ""
    full = f"{address:#x}"
    subtitle, detail = f"…{full[-8:]}", full
    if tag == "task_struct":
        pid = ct.safe(lambda: obj.pid.value_(), None)
        comm = ct.safe(lambda: task_comm(obj), "")
        if pid is not None:
            subtitle = f"{pid} {comm}".strip()
    return tag, subtitle, detail


def _as_pointer(obj: Object) -> Object:
    if ct.strip(obj.type_).kind == TypeKind.POINTER:
        return obj
    return obj.address_of_() if obj.address_ is not None else obj


def _key(obj: Object) -> str:
    """Identity for deduplication: the address, plus the type it is read as.

    The type matters because ``page`` and the ``folio`` overlaying it share an
    address but are not the same box.
    """
    address = ct.safe(lambda: obj.value_(), None)
    tag = ct.tag_of(obj.type_) or "?"
    return f"{tag}@{address:#x}" if address else f"{tag}@?"


def graph_key(obj: Object) -> str:
    """The key of the box ``obj`` would occupy, however it is spelled.

    A struct and a pointer to it map to the same box. Callers outside this
    module that compare graphs use this function, so they do not convert a
    value to a pointer themselves.
    """
    return _key(_as_pointer(obj))


def _resolve(link, obj: Object, sample: int = 4):
    """A link's result as either one object or a counted collection.

    Returns ``(single, items, count)``: exactly one of ``single`` and ``items``
    is set. A collection that turns out to hold one member is treated as a
    single object, so the member is drawn instead of a "children ×1" box.

    ``sample`` is how many members to keep. A collapsed collection needs only
    enough to name its element type; an expanded one needs a box each.
    """
    result = link.resolve(obj)
    if isinstance(result, Object):
        return result, None, 1

    items: list[tuple[str, Object]] = []
    count = 0
    for item in result:
        count += 1
        if len(items) < sample:
            items.append(item)
        if count >= COUNT_LIMIT:
            count = -1  # "many"
            break
    if count == 0:
        return None, None, 0
    if count == 1:
        return items[0][1], None, 1
    return None, items, count


def _link_count(obj: Object) -> int:
    """How many curated edges leave ``obj``.

    Does not resolve the edges: the table lookup and the predicates are cheap,
    whereas resolving walks maple trees and page tables. A collapsed box can
    therefore show "+7" without reading kernel memory.
    """
    return sum(1 for link in links_for(obj) if link.visible(obj))


def build(obj: Object, expanded: frozenset[str]) -> Graph:
    """Walk curated links out of ``obj``, breadth first, into ``expanded``.

    Only nodes whose key is in ``expanded`` have their links resolved; the
    rest become collapsed boxes showing their edge count. Expansion is per
    node, not a global depth, so that one branch can be followed several
    levels deep while every other branch stays collapsed.

    Breadth first rather than depth first so the tree edge into a node is the
    shortest path to it, which is what makes the picture's columns mean
    "distance from here".
    """
    root = _as_pointer(obj)
    root_key = _key(root)
    # Built like any other box, so that it has a link_count: a root without one
    # reports no edges, cannot be expanded, and so could not be reopened after
    # being collapsed.
    nodes = {root_key: _object_node(root, 0, expanded, "", kind="root")}
    edges: list[Edge] = []

    frontier = [(root_key, root, 0)]
    while frontier:
        key, current, level = frontier.pop(0)
        if key not in expanded:
            continue
        for link in links_for(current):
            if not link.visible(current):
                continue
            fan = f"{key}#{link.label}"
            try:
                single, items, count = _resolve(
                    link, current, MEMBER_LIMIT + 1 if fan in expanded else 4
                )
            except Exception as exc:  # noqa: BLE001 - a bad edge is one box
                bad = f"{key}!{link.label}"
                nodes[bad] = Node(
                    bad, type(exc).__name__, str(exc)[:40], level + 1, "error"
                )
                edges.append(Edge(key, bad, link.label, link.origin))
                continue

            if count == 0:
                continue

            if single is not None:
                target = _as_pointer(single)
                if not ct.safe(lambda: target.value_(), 0):
                    continue  # a NULL edge is not a dependency
                child = _key(target)
                if child in nodes:
                    # Already drawn. Recorded on the source as a return edge
                    # instead of drawing a line back across the canvas.
                    nodes[key].returns.append(f"{link.label} → {nodes[child].subtitle}")
                    edges.append(Edge(key, child, link.label, link.origin, back=True))
                    continue
                nodes[child] = _object_node(target, level + 1, expanded, link.doc)
                edges.append(Edge(key, child, link.label, link.origin))
                frontier.append((child, target, level + 1))
                continue

            # A collection is drawn as one box showing its size until it is
            # opened, so that a task's 156 open files do not push the selected
            # branch off screen.
            size = "many" if count < 0 else f"×{count}"
            # The box shows the type of the collection's members; the edge
            # already shows the collection's name, so the link label is not
            # repeated in the box.
            tags = {ct.tag_of(member.type_) for _, member in items}
            title = tags.pop() if len(tags) == 1 else link.label
            nodes[fan] = Node(
                fan,
                title or link.label,
                size,
                level + 1,
                "fanout",
                doc=link.doc,
                collapsed=fan not in expanded,
                link_count=0 if count < 0 else count,
                expand=_expander(link, current),
            )
            edges.append(Edge(key, fan, link.label, link.origin))

            if fan not in expanded:
                continue

            # Opened: each member is drawn as its own box and can be expanded
            # in turn, so following one file to its inode takes the same two
            # keystrokes as any other edge.
            for item_label, member in items[:MEMBER_LIMIT]:
                target = _as_pointer(member)
                if not ct.safe(lambda: target.value_(), 0):
                    continue
                child = _key(target)
                if child in nodes:
                    nodes[fan].returns.append(f"{item_label} → {nodes[child].subtitle}")
                    edges.append(Edge(fan, child, item_label, "", back=True))
                    continue
                nodes[child] = _object_node(target, level + 2, expanded, link.doc)
                edges.append(Edge(fan, child, item_label, ""))
                frontier.append((child, target, level + 2))

            rest = -1 if count < 0 else count - MEMBER_LIMIT
            if rest != 0:
                more = f"{fan}~more"
                nodes[more] = Node(
                    more,
                    "…",
                    "more" if rest < 0 else f"{rest} more",
                    level + 2,
                    "more",
                    doc=f"the rest of {link.label}",
                    expand=_expander(link, current),
                )
                edges.append(Edge(fan, more, "", ""))

    return Graph(root_key, nodes, edges)


def _object_node(
    target: Object,
    level: int,
    expanded: frozenset[str],
    doc: str,
    kind: str = "object",
) -> Node:
    """A box for one struct, collapsed unless it has been opened."""
    title, subtitle, detail = _identity(target)
    outgoing = ct.safe(lambda: _link_count(target), 0)
    return Node(
        _key(target),
        title,
        subtitle,
        level,
        kind,
        target,
        doc,
        collapsed=_key(target) not in expanded and bool(outgoing),
        link_count=outgoing,
        detail=detail,
    )


def _expander(link, obj: Object) -> Callable[[], list]:
    """Open a fan-out box as the list view the table already knows how to show."""
    return lambda: collection_rows(collect(link.label, lambda: link.resolve(obj)))


class GraphScroll(ScrollableContainer):
    """The viewport.

    Not focusable, because a focused scroll container binds the arrow keys to
    scrolling, and the screen would not receive them to move the selection.
    The view scrolls to follow the selection.
    """

    can_focus = False
    can_focus_children = False


class GraphCanvas(Static):
    """The drawing. A plain Static; the graph is small enough to redraw whole."""

    def show(self, graph: Graph, placed, selected: str | None, path) -> None:
        palette = _palette(self.app)
        canvas = draw(graph, placed, selected, path)
        text = Text()
        for index, row in enumerate(lines(canvas)):
            if index:
                text.append("\n")
            for run, style in row:
                text.append(run, palette.get(style, ""))
        self.update(text)


class GraphScreen(Screen):
    """Focus mode over one object."""

    # Nothing on this screen takes focus, so every key reaches these bindings.
    AUTO_FOCUS = None

    BINDINGS = [
        Binding("escape,g", "leave", "back to fields"),
        Binding("up,k", "move('up')", "up"),
        Binding("down,j", "move('down')", "down"),
        Binding("left,h", "move('left')", "toward root"),
        Binding("right,l", "move('right')", "outward"),
        Binding("enter,space", "toggle", "expand/collapse"),
        Binding("z", "isolate", "collapse others"),
        Binding("c", "recentre", "re-centre here"),
        Binding("y", "copy_node", "copy node"),
        Binding("m", "toggle_mouse", "mouse"),
        Binding("backspace", "pop", "back"),
        Binding("f", "fields", "details"),
    ]

    def __init__(
        self, explorer, obj: Object, label: str, state: dict | None = None
    ) -> None:
        super().__init__()
        self.explorer = explorer
        # The roots in re-centring order, the current root last; backspace pops it.
        self.history: list[tuple[Object, str]] = [(obj, label)]
        self.graph: Graph | None = None
        self.placed: dict = {}
        self.selected: str | None = None
        # Keys whose links have been walked. Only the root starts expanded, so
        # the first graph is one level deep and each further level is expanded
        # on request.
        self.expanded: frozenset[str] = frozenset()
        # Reopening the graph after visiting the table restores the boxes that
        # were expanded, because the user expanded them.
        self.restoring = state["selected"] if state else None
        if state is not None:
            self.history = list(state["history"])
            self.expanded = state["expanded"]

    def compose(self) -> ComposeResult:
        yield Static("", id="graph-path", markup=False)
        with GraphScroll(id="graph-scroll"):
            yield GraphCanvas(id="graph-canvas")
        yield Static("", id="graph-info", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        if self.restoring is None:
            self.expanded = frozenset({graph_key(self.history[0][0])})
        self.rebuild(keep=self.restoring)

    # ------------------------------------------------------------- building

    def rebuild(self, keep: str | None = None) -> None:
        """Re-resolve the picture. ``keep`` is the selection to restore."""
        obj, _label = self.history[-1]
        expanded = self.expanded
        self.query_one("#graph-info", Static).update("resolving links…")

        def work() -> None:
            try:
                graph = build(obj, expanded)
            except Exception as exc:  # noqa: BLE001 - report, don't kill the UI
                self.app.call_from_thread(self._failed, exc)
                return
            self.app.call_from_thread(self._built, graph, keep)

        self.run_worker(work, thread=True)

    def _failed(self, exc: Exception) -> None:
        self.query_one("#graph-info", Static).update(f"{type(exc).__name__}: {exc}")

    def _built(self, graph: Graph, keep: str | None = None) -> None:
        self.graph = graph
        self.placed = layout(graph)
        # Expanding does not move the selection, which stays on the expanded
        # box.
        self.selected = keep if keep in graph.nodes else graph.root
        self.refresh_canvas()

    def path_to_root(self, key: str | None) -> frozenset[str]:
        """Every node between the root and ``key``, exclusive of ``key``."""
        chain: set[str] = set()
        current = self.graph.parent(key) if key and self.graph else None
        while current is not None:
            chain.add(current)
            current = self.graph.parent(current)
        return frozenset(chain)

    def update_breadcrumb(self) -> None:
        """The trail from the centre to the selected box, in edge names.

        The path line shows the edges followed within the graph to reach the
        selection, not only the re-centrings.
        """
        trail = [label for _, label in self.history]
        if self.graph is not None and self.selected is not None:
            steps: list[str] = []
            current = self.selected
            while (parent := self.graph.parent(current)) is not None:
                edge = self.graph.edge(parent, current)
                steps.append(edge.label if edge and edge.label else "…")
                current = parent
            trail += reversed(steps)
        self.query_one("#graph-path", Static).update("graph  ·  " + " › ".join(trail))

    def refresh_canvas(self) -> None:
        if self.graph is None:
            return
        self.update_breadcrumb()
        self.query_one("#graph-canvas", GraphCanvas).show(
            self.graph, self.placed, self.selected, self.path_to_root(self.selected)
        )
        self.update_info()
        self.scroll_to_selected()

    def scroll_to_selected(self) -> None:
        """Bring the selected box into view, with its incoming edge if it fits.

        Computed here instead of with ``scroll_to_region``. The region to show
        is the edge label and the box, which is wider than the viewport on a
        narrow terminal, and given a region wider than the viewport,
        ``scroll_to_region`` shows its left part: the label, with the box off
        the right edge. So the box is brought into view first, and the label
        only if there is room.
        """
        spot = self.placed.get(self.selected) if self.selected else None
        if spot is None:
            return
        container = self.query_one("#graph-scroll", GraphScroll)
        width, height = container.content_size.width, container.content_size.height
        if not width or not height:
            return

        # The canvas is padded, so text coordinates are not container ones.
        pad = self.query_one("#graph-canvas", GraphCanvas).styles.padding
        x0, y0 = spot.x + pad.left, spot.y + pad.top
        x1, y1 = x0 + spot.w, y0 + BOX_HEIGHT

        scroll_x, scroll_y = container.scroll_offset
        # The box is always brought into view: right edge first, then left, so
        # a box wider than the viewport shows its start.
        if x1 > scroll_x + width:
            scroll_x = x1 - width
        if x0 < scroll_x:
            scroll_x = x0
        # Then the incoming edge, only if the box stays in view.
        lead = max(0, x0 - COL_GAP - 2)
        if lead < scroll_x and x1 - lead <= width:
            scroll_x = lead

        if y1 > scroll_y + height:
            scroll_y = y1 - height
        if y0 < scroll_y:
            scroll_y = y0

        container.scroll_to(x=scroll_x, y=scroll_y, animate=False)

    def update_info(self) -> None:
        if self.graph is None or self.selected is None:
            return
        node = self.graph.nodes[self.selected]
        parts: list[str] = []
        parent = self.graph.parent(self.selected)
        if parent is not None:
            edge = self.graph.edge(parent, self.selected)
            if edge is not None:
                origin = f"  [{edge.op}]" if edge.op else ""
                parts.append(f"{self.graph.nodes[parent].title}: {edge.label}{origin}")
        else:
            # The root has no incoming edge, so the line names the root.
            parts.append(f"{node.title} {node.subtitle}  (centre of this graph)")
        if node.detail:
            parts.append(node.detail)
        if node.doc:
            parts.append(node.doc)
        if node.returns:
            parts.append("also reached by: " + ", ".join(node.returns))
        if node.kind == "fanout":
            parts.append("enter: open as a list")
        elif self.selected in self.expanded:
            parts.append("enter: collapse")
        elif node.link_count:
            parts.append(f"enter: expand {node.link_count} edges")
        self.query_one("#graph-info", Static).update("  ·  ".join(parts))

    # ------------------------------------------------------------ navigation

    def _column(self, depth: int) -> list[str]:
        keys = [k for k, n in self.graph.nodes.items() if n.depth == depth]
        return sorted(keys, key=lambda k: self.placed[k].y)

    def _expandable(self, node) -> bool:
        """Whether this box has edges it could open."""
        if node.kind in ("fanout",):
            return True
        return node.kind in ("object", "root") and bool(node.link_count)

    def action_move(self, direction: str) -> None:
        if self.graph is None or self.selected is None:
            return
        node = self.graph.nodes[self.selected]

        # Right expands a collapsed box, then moves into it; left collapses an
        # expanded box, then moves back out, as in a tree widget. Expanding
        # needs no separate key.
        if (
            direction == "right"
            and self._expandable(node)
            and self.selected not in self.expanded
        ):
            self.expanded = self.expanded | {self.selected}
            self.rebuild(keep=self.selected)
            return
        if (
            direction == "left"
            and self.selected in self.expanded
            and self.graph.children(self.selected)
        ):
            self._collapse(self.selected)
            return

        if direction in ("up", "down"):
            column = self._column(node.depth)
            index = column.index(self.selected)
            index += -1 if direction == "up" else 1
            if 0 <= index < len(column):
                self.selected = column[index]
            else:
                self.app.bell()
                return
        elif direction == "left":
            parent = self.graph.parent(self.selected)
            if parent is None:
                self.app.bell()
                return
            self.selected = parent
        else:
            kids = [k for k in self.graph.children(self.selected) if k in self.placed]
            if not kids:
                self.app.bell()
                return
            # Select the child nearest this box's row, so that moving right
            # follows the edge drawn from it.
            here = self.placed[self.selected].port
            self.selected = min(kids, key=lambda k: abs(self.placed[k].port - here))

        self.refresh_canvas()

    def action_toggle(self) -> None:
        """Open the selected box's edges, or shut them again.

        The result is drawn on the canvas: a collection opens into one box per
        member instead of switching to a list.
        """
        if self.graph is None or self.selected is None:
            return
        node = self.graph.nodes[self.selected]
        if self.selected in self.expanded:
            self._collapse(self.selected)
        elif self._expandable(node):
            self.expanded = self.expanded | {self.selected}
            self.rebuild(keep=self.selected)
        else:
            self.app.bell()

    def _collapse(self, key: str) -> None:
        """Shut a box and everything under it.

        The whole subtree is collapsed, not one level, so that reopening the
        box does not restore branches below it that were collapsed with it.
        """
        self.expanded = frozenset(
            k for k in self.expanded if not self._under(k, key)
        ) - {key}
        self.rebuild(keep=key)

    def _under(self, key: str, ancestor: str) -> bool:
        current = self.graph.parent(key)
        while current is not None:
            if current == ancestor:
                return True
            current = self.graph.parent(current)
        return False

    def action_isolate(self) -> None:
        """Shut every branch except the one the selected box is on."""
        if self.graph is None or self.selected is None:
            return
        keep = self.path_to_root(self.selected) | {self.selected}
        self.expanded = frozenset(k for k in self.expanded if k in keep)
        self.rebuild(keep=self.selected)

    def action_recentre(self) -> None:
        """Make the selected box the new root, or open a fan-out as a list."""
        if self.graph is None or self.selected is None:
            return
        node = self.graph.nodes[self.selected]
        if node.kind == "fanout" and node.expand is not None:
            # The list is named after the edge, not the box: the box shows
            # "task_struct", the edge "threads", and the breadcrumb shows
            # "threads".
            parent = self.graph.parent(self.selected)
            edge = self.graph.edge(parent, self.selected) if parent else None
            self.explorer.open_rows(edge.label if edge else node.title, node.expand, node.doc)
            self.app.pop_screen()
            return
        if node.obj is None:
            self.app.bell()
            return
        self.history.append((node.obj, f"{node.title} {node.subtitle}"))
        # Keys are addresses, so an expansion from the old graph could match a
        # box in the new one. The new graph starts collapsed.
        self.expanded = frozenset({graph_key(node.obj)})
        self.rebuild()

    def action_pop(self) -> None:
        if len(self.history) > 1:
            self.history.pop()
            self.expanded = frozenset({graph_key(self.history[-1][0])})
            self.rebuild()
        else:
            self.app.pop_screen()

    def action_fields(self) -> None:
        """Leave the picture for the table: fields for a struct, rows for a set.

        This is the only key that leaves the graph; expanding stays in it, so
        the two actions use different keys.
        """
        if self.graph is None or self.selected is None:
            return
        node = self.graph.nodes[self.selected]
        if node.expand is not None:
            parent = self.graph.parent(self.selected)
            edge = self.graph.edge(parent, self.selected) if parent else None
            self.save_state()
            self.app.pop_screen()
            self.explorer.open_rows(
                edge.label if edge and edge.label else node.title,
                node.expand,
                node.doc,
            )
            return
        if node.obj is None:
            self.app.bell()
            return
        self.save_state(handoff=graph_key(node.obj))
        self.app.pop_screen()
        self.explorer.open_object(f"{node.title} {node.subtitle}", node.obj, node.doc)

    def save_state(self, handoff: str | None = None) -> None:
        """Save the graph, so that reopening it restores its state.

        ``handoff`` is the box whose detail view is being opened. Returning from
        that view restores this graph; pressing g on another object does not.
        """
        root = graph_key(self.history[-1][0])
        returns = {root}
        if handoff:
            returns.add(handoff)
        self.explorer.graph_state = {
            "history": list(self.history),
            "expanded": self.expanded,
            "selected": self.selected,
            "returns": returns,
        }

    def action_leave(self) -> None:
        self.save_state()
        self.app.pop_screen()

    def action_copy_node(self) -> None:
        """Copy the selected graph node's address or identifier to clipboard."""
        if self.graph is None or self.selected is None:
            return
        node = self.graph.nodes.get(self.selected)
        if node is None:
            return
        text = node.detail or node.subtitle or node.title
        if node.obj is not None:
            try:
                addr = int(node.obj.value_())
                text = f"{addr:#x}"
            except Exception:
                pass
        copy_to_system_clipboard(text, self.app)
        self.app.notify(f"Copied node: {text}", title="Clipboard", timeout=2.5, markup=False)

    def action_toggle_mouse(self) -> None:
        if hasattr(self.app, "action_toggle_mouse"):
            self.app.action_toggle_mouse()
