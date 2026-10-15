"""Layout of a neighbourhood of the object graph on a character grid.

This module does not read kernel memory. It receives nodes and edges that are
already resolved and computes where each box is drawn, so the layout can be
tested without a kernel or a vmlinux.

The layout is a tidy tree: each column is one depth, and a parent is centred
on its children. Object graphs contain cycles (a task's runqueue points back
to the task), so each node is placed on the first path found to it, and every
other edge into it is drawn as a back-edge badge instead of a line, because
lines routed back across a character grid cannot be followed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# A box is three lines: border, content, border. The type and the identity
# share the content line, which keeps every box one content line high.
BOX_HEIGHT = 3
# Boxes in a column are stacked without a blank row between them, because an
# edge's name and operation are drawn in the gap between columns.
ROW_GAP = 0
# Room between one column's right edge and the next one's left, holding the
# trunk, the edge's name and the operation that traverses it.
COL_GAP = 38
# The name sits between the trunk and the target box; the rest is dashes.
MAX_LABEL = COL_GAP - 8


@dataclass
class Node:
    """One box.

    ``key`` identifies the object, not its position: two edges that reach the
    same address must produce the same key, or the same struct is drawn as two
    boxes.
    """

    key: str
    title: str
    subtitle: str
    depth: int
    kind: str = "object"  # root | object | fanout | error
    obj: Any = None
    doc: str = ""
    # A collapsed node has edges that were not walked. ``link_count`` comes
    # from the link table without resolving the links, so the box shows how
    # many edges it hides without reading kernel memory.
    collapsed: bool = False
    link_count: int = 0
    # The text the box shortened to fit, normally the full address. It is
    # shown in the info line.
    detail: str = ""
    # Fan-out nodes stand for a whole collection; this opens it as a list.
    expand: Callable[[], list] | None = None
    # Labels of edges that leave this node and land on a box already drawn.
    returns: list[str] = field(default_factory=list)

    @property
    def content(self) -> str:
        text = f"{self.title}  {self.subtitle}".rstrip()
        # A collection already shows its size, as "×156", so "+156" is not
        # added to it.
        if self.collapsed and self.link_count and self.kind not in ("fanout", "more"):
            text += f"  +{self.link_count}"
        return text

    @property
    def width(self) -> int:
        # Content, a space either side, the badge column, and two borders.
        return len(self.content) + 5


@dataclass
class Edge:
    """The operation that leads from one object to another.

    ``op`` is that operation as the kernel writes it: a member dereference, a
    list walk, or a call such as ``task_rq()``. It is drawn under the edge's
    name.
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

    Children are stacked in edge order and the parent is centred on them, so
    each child of a task (the mm, the runqueue, the parent task) has its own
    row and no two edges cross.
    """
    by_depth: dict[int, list[str]] = {}
    for key, node in graph.nodes.items():
        by_depth.setdefault(node.depth, []).append(key)

    # Every box in a column takes the width of the widest box. The trunk is
    # drawn just past the column's right edge, so a narrower box would place
    # its trunk inside the area where the boxes beside it are drawn.
    column_x: dict[int, int] = {}
    column_w: dict[int, int] = {}
    x = 0
    for depth in sorted(by_depth):
        column_x[depth] = x
        column_w[depth] = max(graph.nodes[k].width for k in by_depth[depth])
        x += column_w[depth] + COL_GAP

    placed: dict[str, Placed] = {}
    # Next free row per column. A per-column cursor keeps a deep branch from
    # moving down the boxes of other branches in the columns it does not use.
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
    # A node not reachable through tree edges indicates a resolver fault. It
    # is placed below all other nodes so that it remains visible.
    for key in graph.nodes:
        if key not in placed:
            node = graph.nodes[key]
            y = cursor.get(node.depth, 0)
            cursor[node.depth] = y + BOX_HEIGHT + ROW_GAP
            placed[key] = Placed(node, column_x[node.depth], y, column_w[node.depth])
    return placed


# The box-drawing character for each set of arms. Composing the trunk from
# arms, instead of special-casing the first and last child, draws the correct
# junction when a parent's row is also one of its children's rows.
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

    ``path`` is the chain of ancestors from the root to the selected node, and
    is highlighted so the route from the root to the selection is visible.
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
        # The badge marks a node that other edges also reach. The info line
        # names those edges, because the box has no room for them.
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
        top, bottom = min([*ports, spot.port]), max([*ports, spot.port])

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
            # An unlabelled edge is drawn as a continuous line, because a gap
            # in it would look like a missing label.
            if name:
                canvas.text(target.port, trunk + 2, f" {name} ", style)
            canvas.hline(target.port, trunk + 1, target.x, "edge")
            canvas.put(target.port, target.x - 1, "▸", style)
            # The operation is drawn one line below the edge's name.
            if edge and edge.op:
                # The text ends one column before the target box, so that it
                # is not read as part of that box.
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
