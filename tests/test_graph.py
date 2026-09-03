"""Focus mode: the graph screen, driven headlessly.

``core/graph.py`` is covered without a kernel by test_layout.py. This is the
other half -- the part that resolves real links into boxes, hands off to the
table view, and comes back to the picture it left.
"""

from __future__ import annotations

import asyncio
import sys

import drgn
from textual.widgets import DataTable, Tree

from harness import settle
from kexplore.catalog.registry import Entry
from kexplore.tui.app import Explorer
from kexplore.tui.graph import GraphScreen

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


async def main() -> int:
    prog = drgn.program_from_kernel()
    app = Explorer(prog)

    async with app.run_test(size=(160, 50)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)
        await settle(app, pilot)

        # Land on a task: it has the richest set of curated links.
        node = next(
            n for branch in tree.root.children for n in branch.children
            if isinstance(n.data, Entry) and n.data.key == "init"
        )
        tree.select_node(node)
        await settle(app, pilot)
        table.focus()
        app.action_follow()
        await settle(app, pilot)
        check(app.stack[-1].obj is not None, "opened pid 1's task_struct")

        # g: the neighbourhood as a picture.
        app.action_graph()
        await settle(app, pilot)
        screen = app.screen
        check(isinstance(screen, GraphScreen), f"g opened focus mode ({type(screen).__name__})")

        graph = screen.graph
        check(graph is not None, "the graph resolved")
        check(len(graph.nodes) > 3, f"the root fanned out to {len(graph.nodes)} boxes")
        check(screen.selected == graph.root, "the centre starts selected")

        root = graph.nodes[graph.root]
        check(root.title == "task_struct", f"the centre is the task ({root.title})")
        check("1 " in root.subtitle or root.subtitle.startswith("1"),
              f"a task is named, not just addressed: {root.subtitle!r}")

        edges = [e.label for e in graph.tree_edges()]
        check("mm (address space)" in edges or "runqueue" in edges,
              f"curated edges are drawn: {edges[:4]}")
        check(all(graph.edge(e.src, e.dst).op for e in graph.tree_edges()),
              "every edge carries the traversal that walks it")

        # Only the centre starts expanded, so the rest advertise a count.
        collapsed = [n for n in graph.nodes.values() if n.collapsed]
        check(bool(collapsed), f"{len(collapsed)} boxes start collapsed")

        # Moving right opens a shut box and steps into it.
        before = len(graph.nodes)
        screen.action_move("right")
        await settle(app, pilot)
        check(screen.selected != graph.root, "right moved outward")
        screen.action_move("right")
        await settle(app, pilot)
        check(len(screen.graph.nodes) >= before,
              f"expanding grew the picture to {len(screen.graph.nodes)}")

        # z collapses everything off the current branch. Asserted as a property
        # of the expansion set, not as a box count: every rebuild re-resolves
        # live state, so a collection that grew an entry between two builds can
        # add a box while the picture is genuinely narrower.
        screen.action_isolate()
        await settle(app, pilot)
        keep = screen.path_to_root(screen.selected) | {screen.selected}
        check(set(screen.expanded) <= keep,
              f"z left only the branch you are on expanded: {len(screen.expanded)} open")
        check(screen.selected in screen.graph.nodes, "the selection survived")

        # f hands off to the table, and escape comes back to the same picture.
        opened = set(screen.expanded)
        selected = screen.selected
        node_kind = screen.graph.nodes[selected].kind
        screen.action_fields()
        await settle(app, pilot)
        check(not isinstance(app.screen, GraphScreen), "f left the graph")
        check(app.graph_state is not None, "the graph remembered what was open")

        app.action_graph_back()
        await settle(app, pilot)
        check(isinstance(app.screen, GraphScreen), "escape reopened the graph")
        if node_kind != "fanout":
            check(set(app.screen.expanded) == opened,
                  "it reopened with the branches still open, not from scratch")

        app.screen.action_leave()
        await pilot.pause()
        check(not isinstance(app.screen, GraphScreen), "escape closes focus mode")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
