"""The irq branch: entries, the edges out of them, and the graph they form.

The interesting part is not that the entries list something, which --check
already says, but that the structures connect: an IRQ line to its handler, a
worker pool to its threads, a kworker task back to the pool it takes work
from. A structure with no edges is a dead end in the UI.
"""

from __future__ import annotations

import asyncio
import sys

import drgn
from harness import settle, tree_nodes
from textual.widgets import DataTable, Tree

from kexplore.catalog.registry import Entry
from kexplore.tui.app import Explorer
from kexplore.tui.graph import GraphScreen

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


def open_entry(app, tree, key):
    node = next(
        n for n in tree_nodes(tree)
        if isinstance(n.data, Entry) and n.data.key == key
    )
    app.stack.clear()
    tree.select_node(node)


def row(app, name):
    return next((r for r in app.stack[-1].rows if r.name == name), None)


def follow_named(app, table, name) -> bool:
    names = [r.name for r in app.stack[-1].rows]
    if name not in names:
        return False
    table.move_cursor(row=names.index(name))
    app.action_follow()
    return True


async def main() -> int:
    prog = drgn.program_from_kernel()
    app = Explorer(prog)

    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)
        table.focus()

        # --- an IRQ line to the handler bound to it ----------------------
        open_entry(app, tree, "active")
        await settle(app, pilot)
        rows = app.stack[-1].rows
        check(len(rows) > 1, f"{len(rows)} IRQ lines have fired")

        # The busiest line is first and always has an action bound to it.
        app.action_follow()
        await settle(app, pilot)
        for label in ("= count", "= hwirq"):
            r = row(app, label)
            check(r is not None and r.value not in ("", "<fault>"),
                  f"irq_desc derived {label} = {(r.value if r else '?')[:50]}")
        check(row(app, "actions") is not None and row(app, "irq_chip") is not None,
              "an IRQ line links to its actions and its chip")

        check(follow_named(app, table, "actions"), "followed the action list")
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)
        check(row(app, "= handler") is not None,
              f"the action names its handler: {getattr(row(app, '= handler'), 'value', '?')}")
        check(row(app, "irq_desc") is not None, "and links back to the line")

        # --- a worker pool to its threads, and back to the pool ----------
        open_entry(app, tree, "pools")
        await settle(app, pilot)
        pools = app.stack[-1].rows
        check(len(pools) > 1, f"{len(pools)} worker pools")

        # A BH pool's worker has no task (its work runs in softirq context), so
        # pick a pool backed by kthreads: the first with more than one worker.
        threaded = next((r for r in pools if "worker(s)" in r.name
                         and int(r.name.split("nice")[1].split()[1]) > 1), pools[0])
        follow_named(app, table, threaded.name)
        await settle(app, pilot)
        context = row(app, "= runs work in")
        check(context is not None, f"a pool says where its work runs: {getattr(context, 'value', '?')}")
        check(row(app, "workers") is not None, "a threaded pool links to its workers")

        # The BH pools are the ones this used to fault on: their single worker
        # has a NULL task, and naming it dereferenced that.
        open_entry(app, tree, "pools")
        await settle(app, pilot)
        bh = next((r for r in app.stack[-1].rows if " 1 worker(s)" in r.name), None)
        if bh is not None:
            follow_named(app, table, bh.name)
            await settle(app, pilot)
            workers = row(app, "workers")
            check(workers is not None, "a BH pool still lists its worker")
            if workers is not None:
                follow_named(app, table, "workers")
                await settle(app, pilot)
                rows = app.stack[-1].rows
                names = [r.name for r in rows]
                check("error" not in {r.kind for r in rows},
                      f"walking it does not fault: {names[:3]}")
                # One worker, so following the link lands on the worker itself
                # rather than a list of one.
                task_link = next(
                    (r for r in rows if r.name == "task" and r.kind == "link"), None
                )
                check(row(app, "= doing") is not None and task_link is None,
                      f"the BH worker offers no task link, only the NULL field: "
                      f"{names[:3]}")

        open_entry(app, tree, "pools")
        await settle(app, pilot)
        follow_named(app, table, threaded.name)
        await settle(app, pilot)
        check(follow_named(app, table, "workers"), "followed the worker list")
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)
        check(row(app, "= doing") is not None,
              f"a worker says what it is running: {getattr(row(app, '= doing'), 'value', '?')}")
        check(row(app, "task") is not None and row(app, "pool") is not None,
              "a worker links to its task and its pool")

        check(follow_named(app, table, "task"), "followed the worker's task")
        await settle(app, pilot)
        check(row(app, "worker (kworker)") is not None,
              "the kworker task links back into the pool it takes work from")
        check(row(app, "VMAs") is None and row(app, "mm (address space)") is not None,
              "a kernel thread hides the VMA walk but still lists its NULL mm")

        # --- a workqueue to the pools that run its work ------------------
        open_entry(app, tree, "workqueues")
        await settle(app, pilot)
        check(len(app.stack[-1].rows) > 1, f"{len(app.stack[-1].rows)} workqueues")
        app.action_follow()
        await settle(app, pilot)
        check(row(app, "= flags") is not None, "a workqueue spells out its flags")
        check(follow_named(app, table, "pool_workqueues"), "followed pwqs")
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)
        check(row(app, "pool") is not None and row(app, "workqueue") is not None,
              "a pool_workqueue joins the queue to the pool")

        # --- the same edges as a picture ---------------------------------
        open_entry(app, tree, "pools")
        await settle(app, pilot)
        follow_named(app, table, threaded.name)
        await settle(app, pilot)
        app.action_graph()
        await settle(app, pilot)
        screen = app.screen
        check(isinstance(screen, GraphScreen), f"g opened focus mode ({type(screen).__name__})")
        if isinstance(screen, GraphScreen):
            graph = screen.graph
            check(graph is not None and len(graph.nodes) > 1,
                  f"the pool fanned out to {len(graph.nodes) if graph else 0} boxes")
            edges = [e.label for e in graph.tree_edges()]
            check("workers" in edges or "queued work" in edges,
                  f"the pool's edges are drawn: {edges[:4]}")
            screen.action_leave()
            await pilot.pause()

        # --- softirq vectors ---------------------------------------------
        open_entry(app, tree, "softirqs")
        await settle(app, pilot)
        vectors = app.stack[-1].rows
        check(len(vectors) >= 8, f"{len(vectors)} softirq vectors")
        app.action_follow()
        await settle(app, pilot)
        handler = row(app, "= handler")
        check(handler is not None and handler.value not in ("", "<fault>"),
              f"a vector names the function it runs: {getattr(handler, 'value', '?')}")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
