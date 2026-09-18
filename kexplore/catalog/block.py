"""The block layer: where a read becomes a request a driver can issue.

A filesystem submits a ``struct bio``: a target device, a starting sector, and
a list of pages. The block layer turns bios into ``struct request`` and hands
those to the driver. A device takes one of two paths, and ``q->mq_ops`` says
which:

* blk-mq, the multiqueue path. Every submitting CPU has a software queue
  (``blk_mq_ctx``), each of which maps to one of the device's hardware queues
  (``blk_mq_hw_ctx``). A request occupies a tag for its whole life, so the tag
  count is the queue depth. An I/O scheduler (mq-deadline, kyber, bfq) sits
  between the two and decides dispatch order.
* the bio-based path, taken by drivers that need no queueing of their own
  (zram, device mapper, loop). ``q->mq_ops`` is NULL, there are no hardware
  queues and no requests: the driver's ``submit_bio`` gets the bio directly.

Requests in flight are usually zero on an idle machine. A request exists only
between dispatch and completion, which for a virtio disk is measured in
microseconds.
"""

from __future__ import annotations

from drgn import Object, Program
from drgn.helpers.linux.block import (
    blk_rq_bytes,
    blk_rq_pos,
    disk_name,
    for_each_disk,
    for_each_partition,
    part_name,
    request_queue_busy_iter,
)
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.list import list_for_each_entry
from drgn.helpers.linux.percpu import per_cpu_ptr

from ..core import ctypes as ct
from .decoders import request_op_name
from .format import as_text
from .registry import Entry, Subsystem, register

SECTOR_SIZE = 512


def _capacity(sectors: int) -> str:
    """Sectors as a size. The block layer counts in 512-byte sectors whatever
    the device's real logical block size is."""
    mib = sectors * SECTOR_SIZE / (1024 * 1024)
    if mib >= 1024:
        return f"{mib / 1024:.1f}G"
    return f"{mib:.0f}M"


def _is_mq(queue: Object) -> bool:
    """Whether this queue takes the request path.

    ``mq_ops`` is the driver's blk-mq operation table. A bio-based driver
    leaves it NULL, and then ``nr_hw_queues`` is 0 and ``queue_hw_ctx`` must
    not be indexed.
    """
    return ct.safe(lambda: queue.mq_ops.value_() != 0, False)


def _elevator_name(queue: Object) -> str:
    if not ct.safe(lambda: queue.elevator.value_(), 0):
        return "none"
    return ct.safe(
        lambda: queue.elevator.type.elevator_name.string_().decode("utf-8", "replace"),
        "?",
    )


def _hw_queues(queue: Object):
    """The hardware queues of one request_queue, in queue_num order."""
    for index in range(int(queue.nr_hw_queues)):
        yield index, queue.queue_hw_ctx[index]


def disks(prog: Program):
    """Every gendisk, with its size and the path its I/O takes.

    A disk is what has a name in /proc/partitions and a queue underneath. The
    partitions of one disk share that queue: they differ only by a starting
    sector.
    """
    for disk in for_each_disk(prog):
        name = as_text(disk_name(disk))
        queue = disk.queue
        sectors = ct.safe(lambda: int(disk.part0.bd_nr_sectors), 0)
        if _is_mq(queue):
            path = f"blk-mq, {int(queue.nr_hw_queues)} hw queue(s), {_elevator_name(queue)}"
        else:
            path = "bio-based, no request queue"
        yield (
            f"{name:<10} {int(disk.major):>3}:{int(disk.first_minor):<3} "
            f"{_capacity(sectors):>8}  {path}",
            disk,
        )


def block_devices(prog: Program):
    """struct block_device: every whole disk and every partition.

    This is what a path under /dev resolves to, and what a mounted filesystem
    holds in ``sb->s_bdev``. ``bd_start_sect`` is where a partition begins on
    the disk it belongs to; it is 0 for a whole disk.
    """
    for bdev in for_each_partition(prog):
        name = as_text(part_name(bdev))
        sectors = ct.safe(lambda: int(bdev.bd_nr_sectors), 0)
        start = ct.safe(lambda: int(bdev.bd_start_sect), 0)
        yield (
            f"{name:<10} {_capacity(sectors):>8}  starts at sector {start}",
            bdev,
        )


def request_queues(prog: Program):
    """One struct request_queue per disk, with its depth and elevator.

    ``nr_requests`` is the scheduler's limit on queued requests; the tag count
    on the hardware queue is the hard limit the driver imposes.
    """
    for disk in for_each_disk(prog):
        queue = disk.queue
        kind = "blk-mq" if _is_mq(queue) else "bio-based"
        yield (
            f"{as_text(disk_name(disk)):<10} {kind:<10} "
            f"nr_requests {int(queue.nr_requests):<5} elevator {_elevator_name(queue)}",
            queue,
        )


def hw_queues(prog: Program):
    """struct blk_mq_hw_ctx: what the driver dispatches from.

    ``nr_ctx`` is how many per-CPU software queues feed this one. ``tags``
    holds the request pool: a request must take a tag before it can be
    dispatched, so ``nr_tags`` is the queue depth.
    """
    for disk in for_each_disk(prog):
        queue = disk.queue
        if not _is_mq(queue):
            continue
        name = as_text(disk_name(disk))
        for index, hctx in _hw_queues(queue):
            depth = ct.safe(lambda: int(hctx.tags.nr_tags), 0)
            active = ct.safe(lambda: int(hctx.nr_active), 0)
            yield (
                f"{name:<10} hctx {index:<3} {int(hctx.nr_ctx)} cpu queue(s), "
                f"{depth} tag(s), {active} active",
                hctx,
            )


def sw_queues(prog: Program):
    """struct blk_mq_ctx: the per-CPU queue a submitting task lands on.

    Allocated per CPU, so a bio submitted on CPU 2 is staged on CPU 2's queue
    and never touches another CPU's lock. ``index_hw`` says which hardware
    queue this one feeds for each mapping type.
    """
    for disk in for_each_disk(prog):
        queue = disk.queue
        if not ct.safe(lambda: queue.queue_ctx.value_(), 0):
            continue
        name = as_text(disk_name(disk))
        for cpu in for_each_online_cpu(prog):
            ctx = per_cpu_ptr(queue.queue_ctx, cpu)
            hw = ct.safe(lambda: int(ctx.index_hw[0]), 0)
            yield f"{name:<10} cpu {cpu:<3} feeds hctx {hw}", ctx


def in_flight_requests(prog: Program):
    """struct request dispatched to a driver and not yet completed.

    ``request_queue_busy_iter`` walks the tag bitmap, which is the only place
    a live request is registered: there is no list of them. Empty on an idle
    machine, and a row here is one I/O the device has been given.
    """
    for disk in for_each_disk(prog):
        queue = disk.queue
        if not _is_mq(queue):
            continue
        name = as_text(disk_name(disk))
        for request in request_queue_busy_iter(queue):
            op = request_op_name(request.cmd_flags)
            sector = ct.safe(lambda: int(blk_rq_pos(request)), 0)
            nbytes = ct.safe(lambda: int(blk_rq_bytes(request)), 0)
            yield (
                f"{name:<10} tag {int(request.tag):<5} {op:<8} "
                f"sector {sector:<12} {nbytes} bytes",
                request,
            )


def io_schedulers(prog: Program):
    """struct elevator_type: the I/O schedulers this kernel has registered.

    Registration is what makes a name usable in
    /sys/block/<disk>/queue/scheduler; a queue points at one of these through
    ``q->elevator->type``.
    """
    for elevator in list_for_each_entry(
        "struct elevator_type", prog["elv_list"].address_of_(), "list"
    ):
        name = ct.safe(
            lambda: elevator.elevator_name.string_().decode("utf-8", "replace"), "?"
        )
        yield f"{name}", elevator


register(
    Subsystem(
        key="block",
        label="block",
        doc="Disks, their request queues, and the requests a driver has in flight.",
        entries=[
            Entry(
                "disks",
                "disks",
                "struct gendisk per disk, with its size and queue path.",
                disks,
            ),
            Entry(
                "bdevs",
                "block devices and partitions",
                "struct block_device: what /dev names and a mount holds open.",
                block_devices,
            ),
            Entry(
                "queues",
                "request queues",
                "struct request_queue per disk, with depth and elevator.",
                request_queues,
            ),
            Entry(
                "hctx",
                "hardware queues",
                "struct blk_mq_hw_ctx, what the driver dispatches from.",
                hw_queues,
            ),
            Entry(
                "ctx",
                "software queues (per-cpu)",
                "struct blk_mq_ctx, the per-CPU queue a submission lands on.",
                sw_queues,
            ),
            Entry(
                "inflight",
                "requests in flight",
                "struct request dispatched and not yet completed; found via the tag bitmap.",
                in_flight_requests,
            ),
            Entry(
                "elevators",
                "I/O schedulers",
                "struct elevator_type registered in elv_list.",
                io_schedulers,
            ),
        ],
    )
)
