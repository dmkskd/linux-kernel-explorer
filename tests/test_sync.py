"""The sync branch: what a task is waiting for.

Locks are not enumerable -- a mutex is a few words inside whatever it protects
and the kernel keeps no registry -- so what this branch lists is the waiting.
The test parks a thread on a contended userspace lock, which puts a futex_q in
that process's private hash, and follows it back to the task.
"""

from __future__ import annotations

import asyncio
import sys
import threading

import drgn
from harness import settle, tree_nodes
from textual.widgets import DataTable, Tree

from kexplore.catalog.registry import Entry, FactEntry
from kexplore.tui.app import Explorer

# Fits in the 15 characters the kernel keeps of a thread name.
WAITER = "kexplore-futex"

ok = True


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


def open_entry(app, tree, key):
    node = next(
        n for n in tree_nodes(tree)
        if isinstance(n.data, (Entry, FactEntry)) and n.data.key == key
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


def follow_containing(app, table, fragment) -> bool:
    names = [r.name for r in app.stack[-1].rows]
    match = next((n for n in names if fragment in n), None)
    if match is None:
        return False
    table.move_cursor(row=names.index(match))
    app.action_follow()
    return True


async def main() -> int:
    prog = drgn.program_from_kernel()

    # Park a thread on a contended lock: it blocks in futex_wait, which queues
    # a futex_q in this process's private hash under a recognisable name.
    held = threading.Lock()
    held.acquire()
    threading.Thread(target=held.acquire, daemon=True, name=WAITER).start()

    app = Explorer(prog)

    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)
        table.focus()

        # --- futex waiters -------------------------------------------------
        open_entry(app, tree, "futex_waiters")
        await settle(app, pilot)
        rows = app.stack[-1].rows
        check(len(rows) > 1, f"{len(rows)} futex waiters")
        mine = next((r for r in rows if WAITER in r.name), None)
        check(mine is not None, f"this test's blocked thread is listed: {WAITER}")
        check(mine is not None and mine.cells is not None
              and any(c.startswith("private, pid") for c in mine.cells),
              f"and is found in its process's private hash: "
              f"{mine.cells if mine else '(missing)'}")
        columns = tuple(str(c.label) for c in table.columns.values())
        check("futex word (user address)" in columns and "hash" in columns,
              f"the list is columns, not one crowded label: {columns}")

        if mine is not None:
            follow_containing(app, table, WAITER)
            await settle(app, pilot)
            key = row(app, "= futex key")
            check(key is not None and "userspace address" in key.value,
                  f"the waiter names the address it is keyed on: "
                  f"{getattr(key, 'value', '?')}")
            check(row(app, "task") is not None, "the waiter links to its task")
            check(follow_named(app, table, "task"), "followed it to the task")
            await settle(app, pilot)
            check(row(app, "= role (pid vs tgid)") is not None,
                  "landed on the blocked thread's task_struct")

        # --- RCU ------------------------------------------------------------
        open_entry(app, tree, "rcu")
        await settle(app, pilot)
        facts = {r.name: r.value for r in app.stack[-1].rows}
        check("grace-period state" in facts and facts["grace-period state"].startswith("RCU_GP"),
              f"the grace-period state is named by the kernel's own table: "
              f"{facts.get('grace-period state', '?')}")
        check("grace periods completed" in facts,
              f"completed grace periods reported: {facts.get('grace periods completed', '?')}")

        open_entry(app, tree, "rcu_cpus")
        await settle(app, pilot)
        check(len(app.stack[-1].rows) >= 1, f"{len(app.stack[-1].rows)} CPUs report RCU state")
        app.action_follow()
        await settle(app, pilot)
        callbacks = row(app, "= callbacks")
        check(callbacks is not None and "grace period" in callbacks.value,
              f"a CPU says what it owes: {getattr(callbacks, 'value', '?')}")
        check(row(app, "rcu_node") is not None, "and links to the node it reports to")

        # --- the two lists that are empty on a healthy machine ---------------
        for key, label in (("mutex_blocked", "tasks blocked on a mutex"),
                           ("rt_blocked", "tasks blocked on an rt_mutex")):
            open_entry(app, tree, key)
            await settle(app, pilot)
            rows = app.stack[-1].rows
            kinds = {r.kind for r in rows}
            check("error" not in kinds,
                  f"{label}: {len(rows)} row(s), no walk error")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
