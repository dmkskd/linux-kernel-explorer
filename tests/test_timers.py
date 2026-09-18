"""The time branch: the two timer machines, and what their timers point at.

A timer is only interesting if you can tell what it will do when it fires.
These are the two cases the kernel makes recoverable: an hrtimer whose
callback is ``hrtimer_wakeup`` belongs to a sleeping task, and a wheel timer
whose callback is ``delayed_work_timer_fn`` belongs to a delayed work item and
so to a workqueue. Both are container_of hops with no field pointing back, so
they are worth a test.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import drgn
from harness import settle, tree_nodes
from textual.widgets import DataTable, Tree

from kexplore.catalog.registry import Entry
from kexplore.tui.app import Explorer

# TASK_COMM_LEN is 16 including the NUL, so this has to fit in 15 characters.
SLEEPER = "kexplore-sleep"

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

    # Park a thread in a long sleep, so there is an hrtimer_wakeup timer
    # belonging to a task this test can recognise. The rows name the task, and
    # a task is a thread: the name set here is what lands in its comm, cut to
    # the 15 characters the kernel keeps.
    sleeping = threading.Thread(target=time.sleep, args=(60,), daemon=True,
                                name=SLEEPER)
    sleeping.start()

    app = Explorer(prog)

    async with app.run_test(size=(140, 45)) as pilot:
        tree = app.query_one("#nav", Tree)
        table = app.query_one("#fields", DataTable)
        table.focus()

        # --- hrtimers -----------------------------------------------------
        open_entry(app, tree, "hrtimers")
        await settle(app, pilot)
        rows = app.stack[-1].rows
        check(len(rows) > 1, f"{len(rows)} hrtimers queued")
        if len(rows) <= 1:
            print(f"         frame={app.stack[-1].label!r} "
                  f"rows={[(r.name, r.kind, r.note) for r in rows]}")
        # Follow this test's own sleeping thread, not whichever sleeper is
        # first: a short sleep elsewhere on the machine expires and is freed
        # between the listing and the follow, and reading it then faults.
        mine = f" {SLEEPER}"
        check(any(mine in r.name for r in rows),
              f"this test's sleeping thread has a timer, named in the row: {SLEEPER}")

        check(follow_containing(app, table, mine), "opened this test's sleeper timer")
        await settle(app, pilot)
        expires = row(app, "= expires in")
        check(expires is not None and "measured against" in expires.value,
              f"the expiry is relative and says what it was measured against: "
              f"{getattr(expires, 'value', '?')}")
        check(row(app, "= runs in") is not None,
              f"the timer says where it fires: {getattr(row(app, '= runs in'), 'value', '?')}")
        check(row(app, "clock base") is not None, "the timer links to its clock base")
        check(row(app, "sleeping task") is not None,
              "an hrtimer_wakeup timer links to the task it will wake")

        check(follow_named(app, table, "sleeping task"), "followed it to the task")
        await settle(app, pilot)
        check(row(app, "= role (pid vs tgid)") is not None,
              "landed on a task_struct, which brings its own links")

        # A timer that is not a sleeper must not offer the task link.
        open_entry(app, tree, "hrtimers")
        await settle(app, pilot)
        other = next((r for r in app.stack[-1].rows if "hrtimer_wakeup" not in r.name), None)
        if other is not None:
            follow_named(app, table, other.name)
            await settle(app, pilot)
            check(row(app, "sleeping task") is None,
                  f"a non-sleeper timer hides the task link ({other.name.split()[-1]})")

        # --- the wheel ----------------------------------------------------
        open_entry(app, tree, "wheel_timers")
        await settle(app, pilot)
        wheel = app.stack[-1].rows
        check(len(wheel) > 1, f"{len(wheel)} timers on the wheel")

        if follow_containing(app, table, "delayed_work_timer_fn"):
            await settle(app, pilot)
            expires = row(app, "= expires in")
            check(expires is not None and "HZ=" in expires.value,
                  f"wheel expiry is in jiffies and ms, with HZ read from the "
                  f"kernel: {getattr(expires, 'value', '?')}")
            check(row(app, "delayed work") is not None and row(app, "workqueue") is not None,
                  "a delayed work timer links to its work item and its queue")
            check(follow_named(app, table, "workqueue"), "followed it to the workqueue")
            await settle(app, pilot)
            check(row(app, "= flags") is not None,
                  "landed in the irq branch's workqueue view")
        else:
            check(False, "no delayed work timer queued to follow")

        # --- bases and hardware -------------------------------------------
        open_entry(app, tree, "wheel_bases")
        await settle(app, pilot)
        bases = [r.name for r in app.stack[-1].rows]
        check(bases and all("cpu" in b for b in bases), f"{len(bases)} wheel bases")
        check(any("deferrable" in b for b in bases),
              f"the bases are named, not numbered: {bases[0]}")

        open_entry(app, tree, "clocksources")
        await settle(app, pilot)
        check(len(app.stack[-1].rows) >= 1, "clocksources listed")
        app.action_follow()
        await settle(app, pilot)
        resolution = row(app, "= resolution")
        check(resolution is not None and "ns per cycle" in resolution.value,
              f"a clocksource states its resolution: {getattr(resolution, 'value', '?')}")

        open_entry(app, tree, "tick")
        await settle(app, pilot)
        check(len(app.stack[-1].rows) >= 1, "tick devices listed")
        app.action_follow()
        await settle(app, pilot)
        check(row(app, "clock event device") is not None,
              "a tick device links to the clock event device behind it")
        check(follow_named(app, table, "clock event device"), "followed it")
        await settle(app, pilot)
        check(row(app, "= state") is not None and row(app, "= event handler") is not None,
              "the device says its state and its handler")

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
