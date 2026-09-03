"""Exercise the process view, curated links, and source documentation."""

from __future__ import annotations

import asyncio
import sys

import drgn
from textual.widgets import DataTable, Static, Tree

from harness import settle
from kexplore.catalog.registry import Entry
from kexplore.tui.app import Explorer

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


def open_entry(app, tree, key):
    node = next(
        n
        for branch in tree.root.children
        for n in branch.children
        if isinstance(n.data, Entry) and n.data.key == key
    )
    app.stack.clear()
    tree.select_node(node)


async def main() -> int:
    prog = drgn.program_from_kernel()
    app = Explorer(prog)

    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)
        hint = app.query_one("#hint", Static)
        doc_pane = app.query_one("#doc", Static)

        check(app.source.available, f"kernel source available ({app.source.source_prefix})")

        # Multithreaded processes -> pick one -> its task_struct.
        open_entry(app, tree, "multithreaded")
        await pilot.pause()
        rows = app.stack[-1].rows
        check(len(rows) > 0, f"found {len(rows)} multithreaded processes")
        print(f"         e.g. {rows[0].name.strip()}")

        table.focus()
        app.action_follow()
        # The struct's documentation is recovered in a worker (pahole over the
        # vmlinux, then the source through debuginfod), so the doc and hint
        # lines below are empty until it lands.
        await settle(app, pilot)
        names = [r.name for r in app.stack[-1].rows]
        check("threads" in names, "task_struct offers a 'threads' link")
        check("mm (address space)" in names, "task_struct offers an 'mm' link")
        check(names.index("threads") < names.index("comm"), "links sort above raw fields")

        # Struct-level docs come from the kernel's own source.
        text = str(doc_pane.renderable)
        check("sched.h" in text, f"doc pane cites source: {text[-40:]}")

        # Follow threads -> a list of tasks in the group.
        table.move_cursor(row=names.index("threads"))
        app.update_hint()
        check("thread group" in str(hint.renderable), "link doc shown in hint line")
        app.action_follow()
        await pilot.pause()
        thread_rows = app.stack[-1].rows
        check(len(thread_rows) > 1, f"thread group expanded to {len(thread_rows)} tasks")
        check(
            any("leader" in r.name for r in thread_rows),
            "thread group contains its leader",
        )

        # Into one thread, then check a field comment from source.
        app.action_follow()
        await settle(app, pilot)
        task_names = [r.name for r in app.stack[-1].rows]
        check("comm" in task_names, "landed on a task_struct")
        table.move_cursor(row=task_names.index("comm"))
        app.update_hint()
        comm_doc = str(hint.renderable)
        check("executable name" in comm_doc, f"field doc from source: {comm_doc[:60]}")

        # mm link from a task with an address space.
        open_entry(app, tree, "init")
        await pilot.pause()
        app.action_follow()
        await pilot.pause()
        names = [r.name for r in app.stack[-1].rows]
        table.move_cursor(row=names.index("VMAs"))
        app.action_follow()
        await pilot.pause()
        check(len(app.stack[-1].rows) > 5, f"pid 1 has {len(app.stack[-1].rows)} VMAs")

        # Every curated link must survive being followed. Regression: single
        # object links (cred, parent, signal) went through `follow(x) or x`,
        # and drgn structs have no truth value.
        open_entry(app, tree, "init")
        await pilot.pause()
        app.action_follow()
        await pilot.pause()
        baseline = list(app.stack)

        for name in [r.name for r in app.stack[-1].rows if r.kind == "link"]:
            app.stack[:] = list(baseline)
            app.render_frame()
            names = [r.name for r in app.stack[-1].rows]
            table.move_cursor(row=names.index(name))
            try:
                app.action_follow()
                await pilot.pause()
            except Exception as exc:  # noqa: BLE001 - that's what we're testing
                check(False, f"link {name!r} raised {type(exc).__name__}: {exc}")
                continue
            check(len(app.stack) > len(baseline), f"link {name!r} followed")

        # Back out all the way.
        app.stack[:] = list(baseline)
        depth = len(app.stack)
        for _ in range(depth):
            app.action_back()
        check(len(app.stack) == 1, "backed out to the root frame")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
