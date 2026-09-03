"""skb queue walking, page resolution, and derived rows.

Assumes a socket with unread data exists (see tests/helpers/stuck_socket.py), because
an idle system has no queued skbs to look at at all.
"""

from __future__ import annotations

import asyncio
import sys

import drgn
from textual.widgets import DataTable, Tree

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


def row(app, name):
    return next((r for r in app.stack[-1].rows if r.name == name), None)


def follow_named(app, table, name):
    names = [r.name for r in app.stack[-1].rows]
    table.move_cursor(row=names.index(name))
    app.action_follow()


async def main() -> int:
    prog = drgn.program_from_kernel()
    app = Explorer(prog)

    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)
        table.focus()

        # --- skb ---------------------------------------------------------
        open_entry(app, tree, "receive")
        await pilot.pause()
        rows = app.stack[-1].rows
        check(len(rows) > 0 and rows[0].obj is not None, f"{len(rows)} queued skbs found")
        print(f"         {rows[0].name}")

        app.action_follow()
        await pilot.pause()
        for label in ("= len", "= headroom", "= tailroom", "= truesize", "= device"):
            r = row(app, label)
            check(r is not None, f"skb derived {label} = {r.value if r else '?'}")
        check(row(app, "shinfo") is not None, "skb links to shared info")

        follow_named(app, table, "shinfo")
        await pilot.pause()
        names = [r.name for r in app.stack[-1].rows]
        check("nr_frags" in names, "reached skb_shared_info (has nr_frags)")

        # qdisc queues use a different list shape; must not raise.
        open_entry(app, tree, "qdisc")
        await pilot.pause()
        err = [r for r in app.stack[-1].rows if r.note == "error"]
        check(not err, f"qdisc walk clean: {err[0].name if err else 'ok'}")

        # --- page --------------------------------------------------------
        open_entry(app, tree, "resident")
        await pilot.pause()
        check(len(app.stack[-1].rows) > 10, f"{len(app.stack[-1].rows)} resident pages")

        app.action_follow()
        await pilot.pause()
        for label in ("= pfn", "= physical address", "= flags", "= refcount"):
            r = row(app, label)
            check(r is not None and r.value not in ("", "<fault>"),
                  f"page derived {label} = {(r.value if r else '?')[:60]}")

        # --- vma -> page bridge ------------------------------------------
        open_entry(app, tree, "vmas_pid1")
        await pilot.pause()
        app.action_follow()
        await pilot.pause()
        check(row(app, "= range") is not None, f"vma derived range = {row(app, '= range').value}")
        check(row(app, "resident pages") is not None, "vma links to its resident pages")

        follow_named(app, table, "resident pages")
        await pilot.pause()
        page_rows = app.stack[-1].rows
        check(page_rows[0].obj is not None, f"vma resolved to {len(page_rows)} pages")
        print(f"         {page_rows[0].name}")

        # --- page -> zone -> node, and page -> the VMAs mapping it ---------
        # The physical side of the same frame: which allocator it came from,
        # and everyone whose page tables reach it.
        app.action_follow()
        await pilot.pause()
        check(row(app, "zone") is not None, "a page links to its zone")
        check(row(app, "mapped by") is not None, "a page links to what maps it")

        follow_named(app, table, "mapped by")
        await pilot.pause()
        mappers = app.stack[-1].rows
        check(mappers and all(r.obj is not None for r in mappers),
              f"{len(mappers)} VMA(s) map this page")
        check(any("maps it" in r.name for r in mappers),
              f"at least one is confirmed by a page table walk: {mappers[0].name}")
        print(f"         {mappers[0].name}")

        app.action_back()
        await pilot.pause()
        follow_named(app, table, "zone")
        await pilot.pause()
        check(row(app, "node") is not None, "a zone links to its NUMA node")
        name = row(app, "name")
        check(name is not None, f"landed on a zone: name = {name.value if name else '?'}")

        follow_named(app, table, "node")
        await pilot.pause()
        check(row(app, "node_id") is not None,
              f"reached pglist_data: node_id = {(row(app, 'node_id') or None) and row(app, 'node_id').value}")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
