"""skb queue walking, page resolution, and derived rows.

An idle system has no queued skbs at all, so this makes its own: it binds a UDP
socket, sends to it, and never reads, which is what tests/helpers/stuck_socket.py
does interactively. It used to rely on whatever the machine happened to have
queued, which made the result depend on how long the tests before it took.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys

import drgn
from harness import settle
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


def queued_socket() -> tuple[socket.socket, socket.socket]:
    """Park unread datagrams in a receive queue, and hold it open.

    Both sockets are returned so the caller keeps them alive: closing the
    listener frees the skbs, and the test is walking them.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for index in range(3):
        sender.sendto(b"kexplore-test-packet-%d" % index, listener.getsockname())
    return listener, sender


async def main() -> int:
    prog = drgn.program_from_kernel()
    listener, sender = queued_socket()
    app = Explorer(prog)

    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)
        table.focus()

        # --- skb ---------------------------------------------------------
        open_entry(app, tree, "receive")
        await pilot.pause()
        rows = app.stack[-1].rows
        # Walk the skb this test queued, not whatever happened to be first: the
        # machine's own queued skbs come and go, and one that is freed between
        # the listing and the follow fails here as if the walk were broken.
        fixture = f"[{os.getpid()}] fd {listener.fileno()} "
        mine = next((r for r in rows if fixture in r.name), None)
        check(mine is not None and mine.obj is not None,
              f"{len(rows)} queued skbs found, including this test's")
        print(f"         {mine.name if mine else rows[0].name if rows else '(none)'}")
        if mine is None:
            print("\nFAIL")
            return 1

        follow_named(app, table, mine.name)
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
        # Not the first VMA: a mapping can have no resident page at all, and
        # pid 1's first one regularly does not. Take the first that resolves to
        # a page, which is what the rest of this section needs.
        open_entry(app, tree, "vmas_pid1")
        await settle(app, pilot)
        candidates = len(app.stack[-1].rows)
        depth = len(app.stack)
        page_rows: list = []
        for index in range(min(candidates, 12)):
            table.move_cursor(row=index)
            app.action_follow()
            await settle(app, pilot)
            if index == 0:
                check(row(app, "= range") is not None,
                      f"vma derived range = {(row(app, '= range') or None) and row(app, '= range').value}")
                check(row(app, "resident pages") is not None,
                      "vma links to its resident pages")
            if row(app, "resident pages") is not None:
                follow_named(app, table, "resident pages")
                await settle(app, pilot)
                rows = app.stack[-1].rows
                if rows and rows[0].obj is not None:
                    page_rows = rows
                    break
            while len(app.stack) > depth:
                app.action_back()
            await settle(app, pilot)

        check(bool(page_rows),
              f"a VMA with resident pages, out of {candidates} tried at most 12")
        if not page_rows:
            print("\nFAIL")
            return 1
        print(f"         {page_rows[0].name}")
        # The cursor is wherever the search left it, and the rows below are a
        # different list. Follow the first page, not the nth.
        table.move_cursor(row=0)

        # --- page -> zone -> node, and page -> the VMAs mapping it ---------
        # The physical side of the same frame: which allocator it came from,
        # and everyone whose page tables reach it.
        app.action_follow()
        await pilot.pause()
        check(row(app, "zone") is not None, "a page links to its zone")
        check(row(app, "mapped by") is not None, "a page links to what maps it")

        follow_named(app, table, "mapped by")
        await settle(app, pilot)
        mappers = app.stack[-1].rows
        # A page mapped by one VMA opens that VMA, not a list of one. Both
        # shapes are correct; which one appears depends on whether the page is
        # shared, which is not something this test gets to choose.
        if row(app, "= range") is not None:
            check(row(app, "mm") is not None,
                  "one VMA maps this page, and it opened directly")
            print(f"         one mapper: {row(app, '= range').value}")
        else:
            check(mappers and all(r.obj is not None for r in mappers),
                  f"{len(mappers)} VMA(s) map this page")
            check(any("maps it" in r.name for r in mappers),
                  f"at least one is confirmed by a page table walk: "
                  f"{mappers[0].name}")
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

    listener.close()
    sender.close()

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
