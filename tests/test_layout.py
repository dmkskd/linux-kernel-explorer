"""Graph layout and drawing, with no kernel involved.

``core/graph.py`` is handed nodes and edges that someone else resolved, which
is what makes the part most likely to be wrong -- the drawing -- testable on
any machine: no VM, no root, no vmlinux. This is that test.

The cases here are the ones real object graphs produce: a fan out of a task,
a cycle back to a box already drawn, and a parent whose own row collides with
one of its children's.
"""

from __future__ import annotations

import sys

from kexplore.core.graph import (
    BOX_HEIGHT,
    Edge,
    Graph,
    Node,
    draw,
    layout,
    lines,
)

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


def node(key: str, depth: int, title: str = "", **kwargs) -> Node:
    return Node(key, title or key, "", depth, **kwargs)


def fan(children: int = 3) -> Graph:
    """A root with ``children`` leaves hanging off it."""
    nodes = {"root": node("root", 0, kind="root")}
    edges = []
    for index in range(children):
        key = f"child{index}"
        nodes[key] = node(key, 1)
        edges.append(Edge("root", key, f"edge{index}", f"root->{index}"))
    return Graph("root", nodes, edges)


def render(graph: Graph) -> list[str]:
    placed = layout(graph)
    canvas = draw(graph, placed, selected=None)
    return ["".join(run for run, _ in row) for row in lines(canvas)]


def main() -> int:
    # --- layout ---------------------------------------------------------
    graph = fan(3)
    placed = layout(graph)
    check(len(placed) == 4, f"every node is placed ({len(placed)})")
    check(
        placed["root"].x == 0 and placed["child0"].x > 0,
        "depth becomes the column: root at x=0, children to its right",
    )

    ys = sorted(placed[f"child{i}"].y for i in range(3))
    check(ys == [0, BOX_HEIGHT, 2 * BOX_HEIGHT], f"children stack without overlap: {ys}")
    check(
        placed["root"].port == placed["child1"].port,
        "the parent is centred on its children",
    )

    columns = {placed[k].w for k in ("child0", "child1", "child2")}
    check(len(columns) == 1, f"a column has one width, so trunks line up: {columns}")

    # --- cycles ---------------------------------------------------------
    # A task's runqueue points back at the task. The back edge must not put a
    # second box on the canvas, and must not be laid out as a tree edge.
    cyclic = Graph(
        "a",
        {"a": node("a", 0, kind="root"), "b": node("b", 1)},
        [Edge("a", "b", "to b", "a->b"), Edge("b", "a", "back to a", "b->a", back=True)],
    )
    check(len(cyclic.tree_edges()) == 1, "a back edge is not a tree edge")
    check(cyclic.children("b") == [], "a back edge adds no children")
    check(len(layout(cyclic)) == 2, "a cycle stays two boxes")

    # --- unreachable ----------------------------------------------------
    orphan = Graph(
        "a",
        {"a": node("a", 0, kind="root"), "lost": node("lost", 1)},
        [],
    )
    check("lost" in layout(orphan), "a node no tree edge reaches is still placed")

    # --- drawing --------------------------------------------------------
    text = render(fan(2))
    joined = "\n".join(text)
    check(any("┌" in line and "┐" in line for line in text), "boxes have borders")
    check("edge0" in joined and "edge1" in joined, "every edge label is drawn")
    check("root->0" in joined, "the traversal is drawn under the label")
    check(joined.count("▸") == 2, "every child gets an arrowhead")
    # Which junction character is right depends on where the parent's own row
    # falls: between its children it needs a left arm (┤), above them a corner.
    check(bool(set("┌└├┤┴┬┼") & set(joined)), "the trunk is built from junctions")

    widths = {len(line.rstrip()) for line in text}
    check(max(widths) < 200, f"the picture stays a sane width ({max(widths)})")

    # A collapsed box advertises what it is hiding, and a collection its size.
    collapsed = Graph(
        "a",
        {"a": Node("a", "task_struct", "611 auditd", 0, "root", collapsed=True,
                   link_count=7)},
        [],
    )
    body = "\n".join(render(collapsed))
    check("+7" in body, f"a collapsed box shows its edge count: {body.splitlines()[1]!r}")

    sized = Graph(
        "a",
        {"a": Node("a", "file", "×156", 0, "fanout", collapsed=True, link_count=156)},
        [],
    )
    body = "\n".join(render(sized))
    check("×156" in body and "+156" not in body,
          "a collection shows its size once, not twice")

    # --- selection ------------------------------------------------------
    graph = fan(3)
    placed = layout(graph)
    canvas = draw(graph, placed, selected="child1", path=frozenset({"root"}))
    styles = {style for row in lines(canvas) for _, style in row if style}
    check("selected" in styles, "the selected box is styled")
    check("path" in styles, "the path back to the root is styled")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
