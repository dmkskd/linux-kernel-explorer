"""Headless smoke test for the TUI.

Drives the app through Textual's pilot against the live kernel, so it catches
compose/layout errors and navigation regressions without a terminal. Run with:

    ./run.sh  # normal UI
    limactl shell kernel-lab sudo env ... python3 tests/smoke.py
"""

from __future__ import annotations

import asyncio
import sys

import drgn
from textual.widgets import DataTable, Static, Tree

from kexplore.catalog.registry import Entry
from kexplore.tui.app import Explorer


def check(condition: bool, message: str) -> bool:
    print(("  ok   " if condition else "  FAIL ") + message)
    return condition


async def main() -> int:
    prog = drgn.program_from_kernel()
    app = Explorer(prog)
    ok = True

    async with app.run_test(size=(120, 40)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)
        path = app.query_one("#path", Static)

        entries = [n for n in tree.root.children for n in n.children]
        ok &= check(len(entries) >= 15, f"tree exposes {len(entries)} entries")

        # Open sched > runqueues.
        runqueues = next(
            n for n in entries if isinstance(n.data, Entry) and n.data.key == "runqueues"
        )
        tree.select_node(runqueues)
        await pilot.pause()
        ok &= check(table.row_count >= 1, f"runqueues listed {table.row_count} CPUs")

        # Follow cpu0's rq, then into a pointer field.
        table.focus()
        app.action_follow()
        await pilot.pause()
        ok &= check(len(app.stack) == 2, "following a CPU pushed a frame")
        fields = [r.name for r in app.stack[-1].rows]
        ok &= check("nr_running" in fields, "struct rq has nr_running")
        ok &= check("curr" in fields, "struct rq has curr")

        # curr is a task_struct pointer: follow it and confirm we land on a task.
        index = fields.index("curr")
        table.move_cursor(row=index)
        app.action_follow()
        await pilot.pause()
        task_fields = [r.name for r in app.stack[-1].rows]
        ok &= check(len(app.stack) == 3, "followed rq.curr")
        ok &= check("comm" in task_fields, "landed on a task_struct (has comm)")
        comm = next(r.value for r in app.stack[-1].rows if r.name == "comm")
        print(f"         rq.curr.comm = {comm}")
        ok &= check("›" in str(path.renderable), "breadcrumb shows the path")

        # Filtering.
        app.filter = "pid"
        ok &= check(
            all("pid" in r.name.lower() or "pid" in r.value.lower() for r in app.visible_rows()),
            f"filter 'pid' matched {len(app.visible_rows())} rows",
        )
        app.filter = ""

        # Back out.
        app.action_back()
        await pilot.pause()
        ok &= check(len(app.stack) == 2, "backspace popped the frame")

        # Refresh re-reads live memory.
        app.action_refresh()
        await pilot.pause()
        ok &= check(len(app.stack[-1].rows) > 0, "refresh re-read the frame")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
