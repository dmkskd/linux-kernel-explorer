"""Measurement rendering: column headers, buckets, counts and units."""

from __future__ import annotations

import asyncio
import sys

import drgn
from textual.widgets import DataTable, Tree

from harness import settle
from kexplore.catalog.registry import Measurement
from kexplore.tui.app import Explorer

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


async def main() -> int:
    prog = drgn.program_from_kernel()
    app = Explorer(prog)

    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)

        node = next(
            n
            for branch in tree.root.children
            for group in branch.children
            for n in ([group] + list(group.children))
            if isinstance(n.data, Measurement) and n.data.key == "runq_wait"
        )
        tree.select_node(node)
        await pilot.pause()

        headers = tuple(str(c.label) for c in table.columns.values())
        check(headers == ("bucket / key", "count", "distribution"),
              f"measurement columns are {headers}")

        # The tracer runs in a worker for its full duration.
        await settle(app, pilot)

        rows = app.stack[-1].rows
        check(rows[0].name == "measures", "definition shown first")
        check(any(r.name == "blind spot" for r in rows), "blind spot shown")

        header = next((r for r in rows if r.name.startswith("── ")), None)
        check(header is not None and "microseconds" in header.name,
              f"unit spelled out: {header.name if header else '?'}")

        buckets = [r for r in rows if r.name.strip().startswith("[")]
        check(len(buckets) > 0, f"{len(buckets)} histogram buckets")
        if buckets:
            b = buckets[0]
            check(b.type_name.strip().isdigit(),
                  f"count in its own column: {b.name.strip()} -> {b.type_name}")

        # Navigating back to a struct view must restore the struct columns.
        node = next(
            n for branch in tree.root.children for n in branch.children
            if getattr(n.data, "key", "") == "runqueues"
        )
        tree.select_node(node)
        await pilot.pause()
        headers = tuple(str(c.label) for c in table.columns.values())
        check(headers == ("field", "type", "value", "offset+size"),
              f"struct columns restored: {headers}")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
