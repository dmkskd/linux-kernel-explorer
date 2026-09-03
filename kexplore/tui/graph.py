"""Focus mode: the neighbourhood of one object, as a picture.

The field table answers "what is in this struct". This answers the other
question you have while staring at a ``task_struct``: *what does it depend on,
and how do you get there*. Boxes are structs; the line between two boxes is
labelled with the edge's name and, underneath, the traversal itself --
``task->mm``, ``task_rq() = cpu_rq(task_cpu(task))``, ``walks task->mm->mm_mt``.
That second line is the point. A kernel is data structures plus the operations
that move between them, and the operation is the half a struct browser
normally throws away.

Edges come from the curated ``LINKS`` table, the same ones the ``→`` rows in
the table view follow, so the picture never claims a relationship the rest of
the tool does not.
"""

from __future__ import annotations

from typing import Callable

from drgn import Object, TypeKind
from rich.text import Text
from textual.app import ComposeResult
from textual.color import Color
from textual.binding import Binding
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

# How many members of a collection to count before giving up and saying "many".
# Counting is the cheap part for most links, but a VMA's resident pages is a
# page table walk, so there has to be a stop.
COUNT_LIMIT = 500

# How many members of an expanded collection get their own box. A task's 156
# open files would bury the branch you were following; the rest stay behind a
# "… n more" box that opens as a list.
MEMBER_LIMIT = 12

def _palette(app) -> dict[str, str]:
    """Concrete colours for the canvas, from the active theme.

    The canvas is a rich ``Text``, not a styled widget tree, so it cannot use
    the ``$text-muted`` design tokens the stylesheet uses -- rich parses a
    style string itself and has never heard of them. Resolve them here instead
    of hardcoding colours, so the view still follows the user's theme.
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
    """A box's label: the type, something to tell this one apart, and detail.

    Addresses are the honest default -- for most structs there is nothing
    shorter that is still unambiguous -- but a task is worth naming, because
    "611 auditd" is the whole reason you opened it.

    Kernel addresses are shown by their tail. Every one on screen shares the
    same ``0xffff...`` prefix, so the leading half distinguishes nothing while
    costing a third of the box's width; the full value goes to the info line.
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

    A struct and a pointer to it are the same box, so callers outside this
    module -- asking "is this the graph I left?" -- go through here rather than
    having to know that a value has to be turned into a pointer first.
    """
    return _key(_as_pointer(obj))


def _resolve(link, obj: Object, sample: int = 4):
    """A link's result as either one object or a counted collection.

    Returns ``(single, items, count)``: exactly one of ``single`` and ``items``
    is set. A collection that turns out to hold one member is treated as a
    single object, because drawing "children ×1" instead of the child would be
    hiding the answer behind a count.

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

    Deliberately does not resolve any of them: the table lookup and the
    predicates are cheap, while resolving is what walks maple trees and page
    tables. This is what lets a collapsed box say "+7" for free.
    """
    return sum(1 for link in links_for(obj) if link.visible(obj))


def build(obj: Object, expanded: frozenset[str]) -> Graph:
    """Walk curated links out of ``obj``, breadth first, into ``expanded``.

    Only nodes whose key is in ``expanded`` have their links resolved; the
    rest become collapsed boxes advertising an edge count. Expansion is per
    node rather than a global depth because a useful picture is one branch
    followed a long way with everything else shut, which a depth number cannot
    express.

    Breadth first rather than depth first so the tree edge into a node is the
    shortest path to it, which is what makes the picture's columns mean
    "distance from here".
    """
    root = _as_pointer(obj)
    root_key = _key(root)
    # Built like any other box, not by hand: a root without link_count reports
    # no edges and refuses to expand, so collapsing it would be a dead end.
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
                    # Already drawn. Record it on the source as a return edge
                    # rather than routing a line back across the canvas.
                    nodes[key].returns.append(f"{link.label} → {nodes[child].subtitle}")
                    edges.append(Edge(key, child, link.label, link.origin, back=True))
                    continue
                nodes[child] = _object_node(target, level + 1, expanded, link.doc)
                edges.append(Edge(key, child, link.label, link.origin))
                frontier.append((child, target, level + 1))
                continue

            # A collection is one box carrying its size, until it is opened.
            # Drawing all of it unasked would bury the branch you are on under
            # a task's 156 open files.
            size = "many" if count < 0 else f"×{count}"
            # The box says what is in the collection; the edge already says
            # what the collection is called, so repeating the link label here
            # would spend the widest line in the picture on nothing.
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

            # Opened: each member gets its own box, and stays expandable in
            # turn, so following one file into its inode is the same two
            # keystrokes as anything else.
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

    Deliberately not focusable: a focused scroll container binds the arrow
    keys to scrolling, which would swallow them before the screen could move
    the selection. Scrolling here follows the selection instead of competing
    with it.
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
        Binding("backspace", "pop", "back"),
        Binding("f", "fields", "details"),
    ]

    def __init__(
        self, explorer, obj: Object, label: str, state: dict | None = None
    ) -> None:
        super().__init__()
        self.explorer = explorer
        # Each re-centring pushes here, so backspace walks back out.
        self.history: list[tuple[Object, str]] = [(obj, label)]
        self.graph: Graph | None = None
        self.placed: dict = {}
        self.selected: str | None = None
        # Keys whose links have been walked. Only the root starts open, so the
        # first picture is one level and everything after that is asked for.
        self.expanded: frozenset[str] = frozenset()
        # Reopening after a trip to the table restores the shape that was on
        # screen: expansions are work the user did, not a cache.
        self.restoring = state["selected"] if state else None
        if state is not None:
            self.history = list(state["history"])
            self.expanded = state["expanded"]

    def compose(self) -> ComposeResult:
        yield Static("", id="graph-path")
        with GraphScroll(id="graph-scroll"):
            yield GraphCanvas(id="graph-canvas")
        yield Static("", id="graph-info")
        yield Footer()

    def on_mount(self) -> None:
        if self.restoring is None:
            self.expanded = frozenset({graph_key(self.history[0][0])})
        self.rebuild(keep=self.restoring)

    # ------------------------------------------------------------- building

    def rebuild(self, keep: str | None = None) -> None:
        """Re-resolve the picture. ``keep`` is the selection to restore."""
        obj, label = self.history[-1]
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
        # Expanding must not move the cursor: the box you opened is the one
        # you are still looking at.
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

        The graph's own navigation is the interesting one -- which edges you
        followed to get here -- so the path line tracks the selection, not
        just the re-centres.
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

        Computed rather than handed to ``scroll_to_region``: the region worth
        showing is the edge label plus the box, which is wider than the
        viewport on a narrow terminal. Asked to fit a region it cannot,
        ``scroll_to_region`` settles for the left of it, which is the label --
        leaving the box itself off the right edge, exactly when expanding
        outward matters most. So the box is placed first and the label is a
        preference applied only if there is room left over.
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
        # The box itself is non-negotiable: right edge first, then left, so a
        # box wider than the viewport still shows its start.
        if x1 > scroll_x + width:
            scroll_x = x1 - width
        if x0 < scroll_x:
            scroll_x = x0
        # Then reveal the edge that reaches it, but never at the box's expense.
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
            # The root has no incoming edge, so say what it is instead of
            # leaving the line blank.
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

        # Right opens a shut box and then walks into it; left shuts an open one
        # and then walks back out. That is the flow a tree gives you, and it
        # means expanding never needs a second key.
        if direction == "right" and self._expandable(node):
            if self.selected not in self.expanded:
                self.expanded = self.expanded | {self.selected}
                self.rebuild(keep=self.selected)
                return
        if direction == "left" and self.selected in self.expanded:
            if self.graph.children(self.selected):
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
            # Land on the child nearest this box's own row, so moving outward
            # follows the line you are looking at.
            here = self.placed[self.selected].port
            self.selected = min(kids, key=lambda k: abs(self.placed[k].port - here))

        self.refresh_canvas()

    def action_toggle(self) -> None:
        """Open the selected box's edges, or shut them again.

        Everything happens on the canvas: a collection opens into a box per
        member here, rather than dropping out of the picture into a list.
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

        The whole subtree, not just one level: leaving orphaned expansions
        behind would make reopening the box restore a shape you had already
        dismissed.
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
            # Name the list after the edge, not after the box: the box says
            # "task_struct", the edge says "threads", and "threads" is the
            # breadcrumb you want.
            parent = self.graph.parent(self.selected)
            edge = self.graph.edge(parent, self.selected) if parent else None
            self.explorer.open_rows(edge.label if edge else node.title, node.expand, node.doc)
            self.app.pop_screen()
            return
        if node.obj is None:
            self.app.bell()
            return
        self.history.append((node.obj, f"{node.title} {node.subtitle}"))
        # Keys are addresses, so an old expansion could accidentally match a
        # box in the new picture. Start it shut.
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

        This is the only key that leaves the graph. Expanding stays here, so
        the two are never confused with each other.
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
        """Remember the picture so reopening it does not start from scratch.

        ``handoff`` is the box whose detail view we are leaving for; coming
        back from that view is what should restore this graph, as opposed to
        pressing g somewhere unrelated.
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
