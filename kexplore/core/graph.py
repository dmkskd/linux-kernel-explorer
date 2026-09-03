"""A neighbourhood of the object graph, laid out for a character grid.

Nothing here reads kernel memory. It is handed nodes and edges that someone
else already resolved and decides where the boxes go, so the part most likely
to be wrong -- the drawing -- can be tested on any machine, with no VM and no
vmlinux.

The layout is a tidy tree, not a force simulation: columns are depth, and a
parent sits centred on its children. Real object graphs are full of cycles
(a task's runqueue points back at the task), so the tree is the *first* path
found to each node and every other edge into it becomes a back-edge, drawn as
a badge rather than a line. Lines that loop across a character grid are how
these pictures stop being readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# A box is three lines: border, content, border. The type and the identity
# share the content line -- stacking them doubled the height of every picture
# for two words that read fine side by side.
BOX_HEIGHT = 3
# No blank row between boxes. Each edge already owns two lines -- its name and
# its operation -- and those sit in the gap column, so stacked boxes read as a
# list without a separator and the picture loses a quarter of its height.
ROW_GAP = 0
# Room between one column's right edge and the next one's left, holding the
# trunk, the edge's name and the operation that traverses it.
COL_GAP = 38
# The name sits between the trunk and the target box; the rest is dashes.
MAX_LABEL = COL_GAP - 8


@dataclass
class Node:
    """One box.

    ``key`` is identity, not position: two edges reaching the same address
    must produce the same key or the picture grows duplicate boxes for what
    is one struct.
    """

    key: str
    title: str
    subtitle: str
    depth: int
    kind: str = "object"  # root | object | fanout | error
    obj: Any = None
    doc: str = ""
    # Collapsed nodes have edges that were deliberately not walked. The count
    # comes from the link table, not from resolving anything, so a box can
    # advertise what it is hiding without reading kernel memory for it.
    collapsed: bool = False
    link_count: int = 0
    # What the box had to shorten to fit -- the full address, normally. Shown
    # in the info line, so the box can stay narrow without losing it.
    detail: str = ""
    # Fan-out nodes stand for a whole collection; this opens it as a list.
    expand: Callable[[], list] | None = None
    # Labels of edges that leave this node and land on a box already drawn.
    returns: list[str] = field(default_factory=list)

    @property
    def content(self) -> str:
        text = f"{self.title}  {self.subtitle}".rstrip()
        # A collection already advertises its size as "×156"; adding "+156"
        # would say the same thing twice in the widest line of the picture.
        if self.collapsed and self.link_count and self.kind not in ("fanout", "more"):
            text += f"  +{self.link_count}"
        return text

    @property
    def width(self) -> int:
        # Content, a space either side, the badge column, and two borders.
        return len(self.content) + 5


@dataclass
class Edge:
    """A traversal, not just a line.

    An edge is the interesting half: a box is a struct sitting in memory, but
    getting from one to the next is always *something the kernel does* --
    dereference a member, walk a list, call ``task_rq()``. ``op`` is that
    step, spelled the way the kernel spells it, and it is drawn under the
    edge's name rather than hidden in a detail pane.
    """

    src: str
    dst: str
    label: str
    op: str = ""
    # True when dst was already reached by a shorter or equal path, so this
    # edge is not what put the box on the canvas.
    back: bool = False


@dataclass
class Graph:
    root: str
    nodes: dict[str, Node]
    edges: list[Edge]

    def tree_edges(self) -> list[Edge]:
        return [e for e in self.edges if not e.back]

    def children(self, key: str) -> list[str]:
        return [e.dst for e in self.tree_edges() if e.src == key]

    def parent(self, key: str) -> str | None:
        for edge in self.tree_edges():
            if edge.dst == key:
                return edge.src
        return None

    def edge(self, src: str, dst: str) -> Edge | None:
        for edge in self.edges:
            if edge.src == src and edge.dst == dst:
                return edge
        return None


@dataclass
class Placed:
    node: Node
    x: int
    y: int
    w: int
    h: int = BOX_HEIGHT

    @property
    def port(self) -> int:
        """The row an edge attaches to: the title line, not the border."""
        return self.y + 1


def layout(graph: Graph) -> dict[str, Placed]:
    """Assign every node a position, root at the left.

    Children are stacked in edge order and the parent is centred on them,
    which is what makes the fan out of a task readable: the mm, the runqueue
    and the parent task each get their own row and nothing crosses.
    """
    by_depth: dict[int, list[str]] = {}
    for key, node in graph.nodes.items():
        by_depth.setdefault(node.depth, []).append(key)

    # Every box in a column takes the column's width, and the column is as
    # wide as its widest box. Uniform width is not cosmetic: the trunk is
    # placed just past the column's right edge, so a narrow box would put its
    # trunk inside the band where its own neighbours are drawn.
    column_x: dict[int, int] = {}
    column_w: dict[int, int] = {}
    x = 0
    for depth in sorted(by_depth):
        column_x[depth] = x
        column_w[depth] = max(graph.nodes[k].width for k in by_depth[depth])
        x += column_w[depth] + COL_GAP

    placed: dict[str, Placed] = {}
    # Next free row per column. Kept per column rather than globally so a deep
    # branch does not push its cousins down for no reason.
    cursor: dict[int, int] = {}

    def place(key: str) -> int:
        node = graph.nodes[key]
        kids = graph.children(key)
        rows = [place(child) for child in kids]
        top = cursor.get(node.depth, 0)
        if rows:
            # Centre on the children, but never overlap what this column has
            # already placed.
            centred = (min(rows) + max(rows)) // 2
            y = max(centred, top)
        else:
            y = top
        cursor[node.depth] = y + BOX_HEIGHT + ROW_GAP
        placed[key] = Placed(node, column_x[node.depth], y, column_w[node.depth])
        return y

    place(graph.root)
    # Anything unreachable through tree edges (should not happen, but a bad
    # resolver should not silently vanish) goes below everything else.
    for key in graph.nodes:
        if key not in placed:
            node = graph.nodes[key]
            y = cursor.get(node.depth, 0)
            cursor[node.depth] = y + BOX_HEIGHT + ROW_GAP
            placed[key] = Placed(node, column_x[node.depth], y, column_w[node.depth])
    return placed


# Which box-drawing character has exactly these arms. Building the trunk from
# arms rather than special-casing first/last child is what keeps the junction
# right when a parent's own row is also one of its children's rows.
_ARMS = {
    frozenset("ud"): "│",
    frozenset("lr"): "─",
    frozenset("rd"): "┌",
    frozenset("ld"): "┐",
    frozenset("ru"): "└",
    frozenset("lu"): "┘",
    frozenset("udr"): "├",
    frozenset("udl"): "┤",
    frozenset("lrd"): "┬",
    frozenset("lru"): "┴",
    frozenset("udlr"): "┼",
    frozenset("u"): "│",
    frozenset("d"): "│",
    frozenset("l"): "─",
    frozenset("r"): "─",
}


class Canvas:
    """A sparse character grid with a style name per cell."""

    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], tuple[str, str]] = {}

    def put(self, y: int, x: int, char: str, style: str = "") -> None:
        self.cells[(y, x)] = (char, style)

    def text(self, y: int, x: int, value: str, style: str = "") -> None:
        for offset, char in enumerate(value):
            self.put(y, x + offset, char, style)

    def hline(self, y: int, x0: int, x1: int, style: str = "") -> None:
        for x in range(x0, x1):
            if (y, x) not in self.cells:
                self.put(y, x, "─", style)

    def size(self) -> tuple[int, int]:
        if not self.cells:
            return 0, 0
        return (
            max(y for y, _ in self.cells) + 1,
            max(x for _, x in self.cells) + 1,
        )


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"


def draw(
    graph: Graph,
    placed: dict[str, Placed],
    selected: str | None,
    path: frozenset[str] = frozenset(),
) -> Canvas:
    """Render boxes and connectors into a canvas.

    ``path`` is the chain of ancestors from the root to the selected node.
    Lighting it is what keeps a wide picture legible: without it every box has
    the same weight and there is no visual thread back to where you started.
    """
    canvas = Canvas()

    def tone(key: str, when_plain: str) -> str:
        if key == selected:
            return "selected"
        if key in path:
            return "path"
        return when_plain

    for key, spot in placed.items():
        node = spot.node
        style = tone(key, node.kind)
        inner = spot.w - 2
        # The badge marks a node that other edges also reach; which edges is
        # in the info line, because naming them here would not fit.
        badge = "↺" if node.returns else " "
        body = _clip(node.content, inner - 3)
        canvas.text(spot.y, spot.x, "┌" + "─" * inner + "┐", style)
        canvas.text(
            spot.y + 1, spot.x, "│ " + body.ljust(inner - 3) + badge + " │", style
        )
        canvas.text(spot.y + 2, spot.x, "└" + "─" * inner + "┘", style)

    for key, spot in placed.items():
        kids = [k for k in graph.children(key) if k in placed]
        if not kids:
            continue
        trunk = spot.x + spot.w + 3
        ports = [placed[k].port for k in kids]
        top, bottom = min(ports + [spot.port]), max(ports + [spot.port])

        # The stub out of the parent, into the trunk.
        canvas.hline(spot.port, spot.x + spot.w, trunk, "edge")

        for y in range(top, bottom + 1):
            arms = set()
            if y > top:
                arms.add("u")
            if y < bottom:
                arms.add("d")
            if y == spot.port:
                arms.add("l")
            if y in ports:
                arms.add("r")
            canvas.put(y, trunk, _ARMS.get(frozenset(arms), "│"), "edge")

        for child in kids:
            target = placed[child]
            edge = graph.edge(key, child)
            name = _clip(edge.label if edge else "", MAX_LABEL)
            style = tone(child, "edge")
            # An unlabelled edge draws no gap in the run -- "└─  ───▸" reads
            # as a label that failed to render.
            if name:
                canvas.text(target.port, trunk + 2, f" {name} ", style)
            canvas.hline(target.port, trunk + 1, target.x, "edge")
            canvas.put(target.port, target.x - 1, "▸", style)
            # The operation rides one line below the name. This is the line
            # that turns the picture from a diagram of structs into a diagram
            # of what the kernel does to get between them.
            if edge and edge.op:
                # Stop a column short of the box, so a long traversal never
                # reads as if it were part of the struct it points at.
                room = target.x - trunk - 4
                canvas.text(
                    target.port + 1,
                    trunk + 3,
                    _clip(edge.op, room),
                    "op-selected" if child in path or child == selected else "op",
                )

    return canvas


def lines(canvas: Canvas) -> list[list[tuple[str, str]]]:
    """The canvas as rows of (text, style) runs, ready for a Text widget."""
    height, width = canvas.size()
    out: list[list[tuple[str, str]]] = []
    for y in range(height):
        runs: list[tuple[str, str]] = []
        current = ""
        style = ""
        for x in range(width):
            char, cell_style = canvas.cells.get((y, x), (" ", ""))
            if cell_style != style:
                if current:
                    runs.append((current, style))
                current, style = "", cell_style
            current += char
        if current:
            runs.append((current.rstrip() if not style else current, style))
        out.append(runs)
    return out
