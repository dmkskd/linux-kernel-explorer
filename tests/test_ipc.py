"""The ipc branch: the three System V objects and what they connect to.

An idle machine has no IPC objects at all, so the helper creates them: a queue
holding an unreceived message, an array with a task blocked in semop(2), and
an attached segment. Everything asserted below depends on it running.

The objects are stable while the helper holds them, unlike a block request, so
the navigation is driven through the frames rather than around them.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

import drgn
from harness import settle, tree_nodes
from textual.widgets import DataTable, Tree

from kexplore.catalog.ipc import message_queues
from kexplore.catalog.registry import Entry, FactEntry
from kexplore.tui.app import Explorer
from kexplore.tui.graph import GraphScreen

ok = True

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helpers", "sysv_ipc.py")
HELPER_SECONDS = "120"
# The helper prints its ids once everything is created. Waiting for a queue
# with a message on it is the same signal, read from the kernel.
WAIT_SECONDS = 20


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


def open_row(app, table, match: str) -> bool:
    """Open the first row whose label contains ``match``."""
    for index, candidate in enumerate(app.stack[-1].rows):
        if match in candidate.name:
            table.move_cursor(row=index)
            app.action_follow()
            return True
    return False


async def open_live_queue(app, tree, table, pilot, settle_for) -> bool:
    """Open a queue whose sender is still running.

    A queue left behind by an earlier run has a message but no live sender,
    and its "last sender" link is hidden. Opening the first queue with a
    message would then land on the wrong one, so each is tried in turn.
    """
    open_entry(app, tree, "msg")
    await settle_for(app, pilot)
    candidates = [
        index for index, candidate in enumerate(app.stack[-1].rows)
        if "1 message(s)" in candidate.name
    ]
    for index in candidates:
        open_entry(app, tree, "msg")
        await settle_for(app, pilot)
        table.move_cursor(row=index)
        app.action_follow()
        await settle_for(app, pilot)
        if row(app, "last sender") is not None:
            return True
    return False


def wait_for_objects(prog) -> bool:
    """Wait until the helper's queue holds its message."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        for label, _ in message_queues(prog):
            if "1 message(s)" in label:
                return True
        time.sleep(0.5)
    return False


async def main() -> int:
    prog = drgn.program_from_kernel()
    app = Explorer(prog)
    helper = subprocess.Popen(
        [sys.executable, HELPER, HELPER_SECONDS],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        if not wait_for_objects(prog):
            check(False, f"the helper created no IPC objects within {WAIT_SECONDS}s")
            print("\nFAIL")
            return 1
        check(True, "the helper's message queue, semaphore array and segment exist")

        async with app.run_test(size=(140, 45)) as pilot:
            tree = app.query_one("#nav", Tree)
            table = app.query_one("#fields", DataTable)
            table.focus()

            # --- the namespace, and what it holds ------------------------
            open_entry(app, tree, "namespaces")
            await settle(app, pilot)
            check(len(app.stack[-1].rows) >= 1,
                  f"{len(app.stack[-1].rows)} IPC namespace(s)")
            app.action_follow()
            await settle(app, pilot)
            holds = row(app, "= holds")
            check(holds is not None and "message queue" in str(getattr(holds, "value", "")),
                  f"a namespace states what it holds: {getattr(holds, 'value', '?')}")
            check(all(row(app, name) is not None for name in
                      ("message queues", "semaphore arrays", "shared memory")),
                  "and links to all three kinds of object")

            # --- a queue, its message, and the process that sent it ------
            check(await open_live_queue(app, tree, table, pilot, settle),
                  "opened the queue whose sender is still running")
            usage = row(app, "= usage")
            check(usage is not None and "of 16384 bytes used" in str(getattr(usage, "value", "")),
                  f"the queue states its usage: {getattr(usage, 'value', '?')}")
            check(row(app, "permissions") is not None and row(app, "messages") is not None,
                  "and links to its permissions and its messages")

            check(follow_named(app, table, "last sender"), "followed the last sender")
            await settle(app, pilot)
            check(row(app, "= role (pid vs tgid)") is not None,
                  "landing on the task_struct that called msgsnd(2)")

            await open_live_queue(app, tree, table, pilot, settle)
            check(follow_named(app, table, "messages"), "followed the message list")
            await settle(app, pilot)
            if row(app, "= message") is None:
                app.action_follow()
                await settle(app, pilot)
            message = row(app, "= message")
            check(message is not None and "bytes" in str(getattr(message, "value", "")),
                  f"the message states its type and size: {getattr(message, 'value', '?')}")

            # --- the key and mode, decoded ------------------------------
            await open_live_queue(app, tree, table, pilot, settle)
            check(follow_named(app, table, "permissions"), "followed the permissions")
            await settle(app, pilot)
            key = row(app, "= key")
            mode = row(app, "= mode")
            check(key is not None and "IPC_PRIVATE" in str(getattr(key, "value", "")),
                  f"a keyless queue says so: {getattr(key, 'value', '?')}")
            check(mode is not None and "owner" in str(getattr(mode, "value", "")),
                  f"and spells out its mode: {getattr(mode, 'value', '?')}")

            # --- a semaphore array, and the task blocked on it -----------
            open_entry(app, tree, "waiters")
            await settle(app, pilot)
            waiters = app.stack[-1].rows
            check(len(waiters) >= 1 and "error" not in {r.kind for r in waiters},
                  f"{len(waiters)} task(s) blocked in semop: {[r.name for r in waiters][:2]}")
            app.action_follow()
            await settle(app, pilot)
            waiting_for = row(app, "= waiting for")
            check(waiting_for is not None
                  and "operation" in str(getattr(waiting_for, "value", "")),
                  f"the pending operation is described: {getattr(waiting_for, 'value', '?')}")
            check(follow_named(app, table, "blocked task"), "followed it to the task")
            await settle(app, pilot)
            check(row(app, "= role (pid vs tgid)") is not None,
                  "landing on the task_struct waiting in the kernel")

            open_entry(app, tree, "sem")
            await settle(app, pilot)
            check(open_row(app, table, "2 semaphore(s)"), "opened the helper's array")
            await settle(app, pilot)
            values = row(app, "= values")
            check(values is not None and str(getattr(values, "value", "")).startswith("["),
                  f"the array shows its values: {getattr(values, 'value', '?')}")
            check(follow_named(app, table, "semaphores"), "followed into the semaphores")
            await settle(app, pilot)
            members = app.stack[-1].rows
            check(len(members) == 2, f"both semaphores are listed: {[r.name for r in members]}")

            # --- a segment, and the tmpfs file holding its pages ---------
            open_entry(app, tree, "shm")
            await settle(app, pilot)
            check(open_row(app, table, "1 attached"), "opened the attached segment")
            await settle(app, pilot)
            size = row(app, "= size")
            check(size is not None and "page(s)" in str(getattr(size, "value", "")),
                  f"the segment states its size: {getattr(size, 'value', '?')}")
            check(row(app, "creator") is not None,
                  "and links to the process that called shmget(2)")
            check(follow_named(app, table, "file"), "followed the segment into vfs")
            await settle(app, pilot)
            check(row(app, "inode") is not None,
                  "landing on the tmpfs struct file, which reaches its inode")

            # --- g on a namespace, with a worker reporting underneath ----
            # The struct documentation for a type is read in a worker that
            # runs pahole over the whole vmlinux, and it reports when it is
            # done. Pressing g first puts the graph screen on top, and on
            # Textual 2 an app-level widget query then resolves against that
            # screen alone and finds nothing. This is what crashed on the
            # Debian lab: the main screen is addressed explicitly now.
            open_entry(app, tree, "namespaces")
            await settle(app, pilot)
            app.action_follow()
            await settle(app, pilot)
            app.action_graph()
            await settle(app, pilot)
            check(isinstance(app.screen, GraphScreen),
                  f"g opened the graph ({type(app.screen).__name__})")
            try:
                app.set_activity("reading kernel source for struct ipc_namespace…")
                app.update_doc()
                app.update_hint()
                app.set_activity("")
                check(True, "a worker can report while the graph screen is up")
            except Exception as exc:  # noqa: BLE001 - this is the regression
                check(False, f"a worker reporting under the graph raised: "
                             f"{type(exc).__name__}: {exc}")
            check(app.panel("#activity") is not None,
                  "and the main screen's status line is still addressable")
            app.screen.action_leave()
            await settle(app, pilot)

            # --- the limits, and the default that means no limit ---------
            open_entry(app, tree, "limits")
            await settle(app, pilot)
            facts = {r.name: str(r.value) for r in app.stack[-1].rows}
            check("max size of one segment" in facts,
                  f"{len(facts)} limits reported")
            shmmax = facts.get("max size of one segment", "")
            check("bytes" in shmmax,
                  f"a limit carries its unit: {shmmax[:60]}")
            check("no limit" not in shmmax or "ULONG_MAX" in shmmax,
                  f"and the ULONG_MAX default is named, not left raw: {shmmax[:90]}")
    finally:
        os.killpg(helper.pid, signal.SIGTERM)
        helper.wait()

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
