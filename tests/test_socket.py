"""Socket navigation, conditional links, and NULL-pointer regressions."""

from __future__ import annotations

import asyncio
import sys

import drgn
from drgn.helpers.linux.pid import for_each_task
from textual.widgets import DataTable, Tree

from kexplore.catalog.links import links_for
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

        # struct socket -> sk -> protocol-specific cast.
        open_entry(app, tree, "process_sockets")
        await pilot.pause()
        check(len(app.stack[-1].rows) > 10, f"{len(app.stack[-1].rows)} socket fds found")

        table.focus()
        app.action_follow()
        await pilot.pause()
        names = [r.name for r in app.stack[-1].rows]
        check("sk (protocol half)" in names, "struct socket links to its sock")
        check("ops" in names and "state" in names, "struct socket fields present")

        follow_named(app, table, "sk (protocol half)")
        await pilot.pause()
        sock_names = [r.name for r in app.stack[-1].rows]
        check("sk_prot" in sock_names or "proto" in sock_names, "landed on struct sock")

        # Conditional links: exactly one protocol cast should be offered.
        casts = [n for n in sock_names if n.startswith("as ")]
        check(len(casts) <= 1, f"at most one protocol cast offered: {casts}")

        # A TCP listening socket must offer the tcp_sock cast and follow it.
        open_entry(app, tree, "tcp_listen")
        await pilot.pause()
        check(len(app.stack[-1].rows) > 0, f"{len(app.stack[-1].rows)} TCP listeners")
        app.action_follow()
        await pilot.pause()
        names = [r.name for r in app.stack[-1].rows]
        check("as tcp_sock" in names, "TCP sock offers the tcp_sock cast")
        follow_named(app, table, "as tcp_sock")
        await pilot.pause()
        tcp_fields = [r.name for r in app.stack[-1].rows]
        check("snd_cwnd" in tcp_fields, "cast reached tcp_sock (has snd_cwnd)")

        # unix sockets must NOT offer the tcp cast.
        open_entry(app, tree, "unix")
        await pilot.pause()
        app.action_follow()
        await pilot.pause()
        names = [r.name for r in app.stack[-1].rows]
        check("as tcp_sock" not in names, "unix sock hides the tcp_sock cast")
        check("as unix_sock" in names, "unix sock offers the unix_sock cast")

        # Regression: a kernel thread's mm is NULL. Following it must warn,
        # not push a pointer frame that then crashes struct_doc with
        # "pointer type does not have a tag".
        open_entry(app, tree, "kthreads")
        await pilot.pause()
        app.action_follow()
        await pilot.pause()
        depth = len(app.stack)
        names = [r.name for r in app.stack[-1].rows]
        check("mm (address space)" in names, "kthread still lists the mm link")
        try:
            follow_named(app, table, "mm (address space)")
            await pilot.pause()
            check(len(app.stack) == depth, "following NULL mm did not push a frame")
        except Exception as exc:  # noqa: BLE001
            check(False, f"following NULL mm raised {type(exc).__name__}: {exc}")

        # A struct file that is not a socket must hide the socket link.
        task = next(t for t in for_each_task(prog) if t.pid.value_() == 1)
        from drgn.helpers.linux.fs import for_each_file

        non_socket = next(
            f
            for _, f in for_each_file(task)
            if f.value_() and f.f_op != prog["socket_file_ops"].address_of_()
        )
        visible = [l.label for l in links_for(non_socket) if l.visible(non_socket)]
        check("socket" not in visible, f"non-socket file hides socket link: {visible}")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
