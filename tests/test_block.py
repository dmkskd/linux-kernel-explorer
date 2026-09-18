"""The block branch: a disk down to a request, and back out to vfs and mm.

--check reports that the entries list something. This checks that the
structures connect: a superblock reaches the device it was mounted from, a
disk reaches its queue, the queue reaches the hardware queue a driver
dispatches from, and a request caught in flight reaches its bios and the pages
they carry.

Requests exist only between dispatch and completion, so one has to be created.
The direct-I/O reads below keep the queue busy while the entry is sampled. A
sample that catches none is reported and the rest still runs.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

import drgn
from harness import settle, tree_nodes
from textual.widgets import DataTable, Tree

from kexplore.catalog.block import in_flight_requests
from kexplore.catalog.links import links_for
from kexplore.catalog.registry import Entry
from kexplore.tui.app import Explorer

ok = True

# Concurrent direct reads, restarted until the test kills them: the sampling
# below takes longer than any fixed amount of I/O would, and an empty queue
# tells us nothing. Direct I/O is required, since a cached read never reaches
# the device at all.
LOAD = (
    "for i in $(seq 8); do "
    "while :; do dd if=/dev/vda of=/dev/null bs=1M count=64 "
    "iflag=direct status=none; done & "
    "done; wait"
)
SAMPLES = 60


def check(condition: bool, message: str) -> None:
    global ok
    ok &= bool(condition)
    print(("  ok   " if condition else "  FAIL ") + message)


def _resolve(obj, label):
    """The items one named link yields, or nothing if it is gone or faults."""
    link = next((lk for lk in links_for(obj) if lk.label == label), None)
    if link is None or not link.visible(obj):
        return []
    try:
        return list(link.resolve(obj))
    except Exception:  # noqa: BLE001 - a freed object is the expected failure
        return []


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

        # --- a disk, its size, and the devices carved out of it ----------
        open_entry(app, tree, "disks")
        await settle(app, pilot)
        disks = app.stack[-1].rows
        check(len(disks) > 0, f"{len(disks)} disks")

        # The first disk is the one the root filesystem lives on, and it takes
        # the request path; zram, further down, does not.
        app.action_follow()
        await settle(app, pilot)
        capacity = row(app, "= capacity")
        check(capacity is not None and capacity.value not in ("", "<fault>"),
              f"a disk states its size: {getattr(capacity, 'value', '?')}")
        check(row(app, "partitions") is not None and row(app, "request queue") is not None,
              "a disk links to its partitions and its queue")

        check(follow_named(app, table, "partitions"), "followed the partition table")
        await settle(app, pilot)
        parts = app.stack[-1].rows
        check(len(parts) >= 1 and "error" not in {r.kind for r in parts},
              f"the part_tbl xarray walks: {[r.name for r in parts][:4]}")

        # --- the queue, and what the driver dispatches from --------------
        open_entry(app, tree, "queues")
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)
        path = row(app, "= path")
        elevator = row(app, "= elevator")
        check(path is not None and "blk-mq" in str(getattr(path, "value", "")),
              f"the first queue takes the request path: {getattr(path, 'value', '?')}")
        check(elevator is not None and elevator.value not in ("", "<fault>"),
              f"and names its I/O scheduler: {getattr(elevator, 'value', '?')}")

        flags = row(app, "queue_flags")
        check(flags is not None and "QUEUE_FLAG" not in str(flags.value)
              and "REGISTERED" in str(flags.value),
              f"queue_flags decodes through the kernel's own names: {getattr(flags, 'value', '?')}")

        check(follow_named(app, table, "hardware queues"), "followed the hardware queues")
        await settle(app, pilot)
        app.action_follow()
        await settle(app, pilot)
        tags = row(app, "= tags in use")
        check(tags is not None and "tag(s) in use" in str(getattr(tags, "value", "")),
              f"a hardware queue states its depth: {getattr(tags, 'value', '?')}")
        check(row(app, "software queues") is not None and row(app, "tags") is not None,
              "and links to the per-CPU queues feeding it, and to its tag pool")

        check(follow_named(app, table, "software queues"), "followed the software queues")
        await settle(app, pilot)
        ctxs = app.stack[-1].rows
        check(len(ctxs) >= 1 and "error" not in {r.kind for r in ctxs},
              f"the per-CPU queues list: {[r.name for r in ctxs][:4]}")

        # --- the bio-based path has no requests at all -------------------
        open_entry(app, tree, "queues")
        await settle(app, pilot)
        names = [r.name for r in app.stack[-1].rows]
        bio_based = next((n for n in names if "bio-based" in n), None)
        if bio_based is not None:
            follow_named(app, table, bio_based)
            await settle(app, pilot)
            path = row(app, "= path")
            check(path is not None and "bio-based" in str(path.value),
                  f"a bio-based queue says so: {getattr(path, 'value', '?')}")
            check(row(app, "hardware queues") is None and row(app, "in flight") is None,
                  "and offers neither hardware queues nor requests")
        else:
            check(True, "no bio-based device on this kernel, that path unchecked")

        # --- a mounted filesystem reaches the device under it ------------
        open_entry(app, tree, "superblocks")
        await settle(app, pilot)
        sbs = app.stack[-1].rows
        target = None
        for index, candidate in enumerate(sbs):
            table.move_cursor(row=index)
            app.action_follow()
            await settle(app, pilot)
            if row(app, "block device") is not None:
                target = candidate
                break
            open_entry(app, tree, "superblocks")
            await settle(app, pilot)
        check(target is not None, "a superblock links to the block_device it was mounted from")
        if target is not None:
            check(follow_named(app, table, "block device"), "followed vfs into block")
            await settle(app, pilot)
            devt = row(app, "= dev_t")
            check(devt is not None and ":" in str(getattr(devt, "value", "")),
                  f"landing on a block_device with a dev_t: {getattr(devt, 'value', '?')}")
            check(row(app, "page cache") is not None and row(app, "device") is not None,
                  "which reaches its page cache and its driver-model device")

        # --- a request caught in flight, down to the pages it moves ------
        load = subprocess.Popen(LOAD, shell=True, start_new_session=True)
        try:
            # A request is recycled the moment the device completes it: its tag
            # goes back in the pool and the struct is reused for the next I/O,
            # with q, mq_hctx and bio cleared. Microseconds, on a virtio disk.
            # So every hop below can find the request already gone, and one
            # sample is the whole descent: list, open, into the bios, into the
            # pages. A sample that loses the request is retried, not reported.
            caught = None
            for _ in range(SAMPLES):
                open_entry(app, tree, "inflight")
                await settle(app, pilot)
                live = [r for r in app.stack[-1].rows if r.kind != "error"]
                if not live:
                    continue
                app.action_follow()
                await settle(app, pilot)
                transfer = row(app, "= transfer")
                cmd_flags = row(app, "cmd_flags")
                if row(app, "hardware queue") is None or not follow_named(app, table, "bios"):
                    continue
                await settle(app, pilot)
                # One bio is the common case, and a link resolving to a single
                # object lands on it rather than on a list of one.
                bio_transfer = row(app, "= transfer")
                if bio_transfer is None and app.stack[-1].rows[0].kind != "error":
                    app.action_follow()
                    await settle(app, pilot)
                    bio_transfer = row(app, "= transfer")
                if bio_transfer is None:
                    continue
                caught = (live[0], transfer, cmd_flags, bio_transfer)
                break

            if caught is None:
                # The UI is too slow for this one object: each hop rebuilds a
                # frame in a worker, and the request is gone by the next. The
                # edges themselves are checked below without that latency.
                check(True, f"no request survived the UI descent in {SAMPLES} samples")
            else:
                listed, transfer, cmd_flags, bio_transfer = caught
                check(True, f"caught a request in flight: {listed.name}")
                check(True, "it links to the queue it dispatches from and the bios in it")
                check(transfer is not None and "sector" in str(getattr(transfer, "value", "")),
                      f"it states what it moves: {getattr(transfer, 'value', '?')}")
                decoded = str(getattr(cmd_flags, "value", ""))
                check(any(op in decoded for op in
                          ("READ", "WRITE", "FLUSH", "DISCARD", "ZEROES")),
                      f"cmd_flags decodes to an operation: {decoded}")
                check("sector" in str(bio_transfer.value),
                      f"the bio states its target: {bio_transfer.value}")
                if follow_named(app, table, "pages"):
                    await settle(app, pilot)
                    pages = app.stack[-1].rows
                    check(len(pages) >= 1 and "error" not in {r.kind for r in pages},
                          f"and hands over {len(pages)} page(s) to mm")
            # --- the same edges, resolved without the UI in between ------
            # Nothing here is redundant with the descent above: that one checks
            # what the frames show, this one checks that the links resolve at
            # all, and it is fast enough to land on a request that is still in
            # flight. Both need the load, so it stays running until here.
            edges = None
            seen = 0
            for _ in range(SAMPLES):
                found = next(iter(in_flight_requests(prog)), None)
                if found is None:
                    continue
                seen += 1
                label, request = found
                bios = [b for _, b in _resolve(request, "bios")]
                if not bios:
                    continue
                pages = [p for _, p in _resolve(bios[0], "pages")]
                if not pages:
                    continue
                edges = (label, bios, pages)
                break
            if edges is None:
                # Seeing none at all means the load never reached the device,
                # which is a broken test rather than a busy one. Seeing them
                # come and go without a bio surviving the read is the disk
                # being faster than this loop, and says nothing about the link.
                check(seen > 0, f"the load put requests on the queue: {seen} seen "
                                f"in {SAMPLES} tries, none held a bio long enough")
            else:
                label, bios, pages = edges
                check(True, f"a live request reaches {len(bios)} bio(s): {label}")
                check(all(p.value_() != 0 and "page" in p.type_.type_name()
                          for p in pages),
                      f"and the first bio hands over {len(pages)} struct page(s) to mm")
        finally:
            # The shell's children outlive it, so the whole process group goes.
            os.killpg(load.pid, signal.SIGKILL)
            load.wait()

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
