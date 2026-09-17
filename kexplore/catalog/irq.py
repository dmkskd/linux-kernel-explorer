"""Hardware interrupts and the work the kernel defers out of them.

A handler runs with interrupts of its line masked, so it does as little as
possible and hands the rest on. Three destinations take that work, and they
differ in what may run there:

* a softirq vector, run on the same CPU after the handler returns (or in
  ksoftirqd when they pile up). It cannot sleep.
* a tasklet, which is one softirq vector (TASKLET, HI) running a list of
  callbacks. It cannot sleep either.
* a workqueue item, run by a worker thread out of a worker pool. This one is
  in process context and may sleep, which is why a driver that needs to
  allocate or take a mutex ends up here.

``struct irq_desc`` holds the per-line state: the actions bound to it, the
chip that masks and acks it, and a per-CPU count of how often it has fired.
The counts come from the same place ``/proc/interrupts`` reads, summed over
online CPUs here, so a line that fires on one CPU only is still one row.
"""

from __future__ import annotations

from drgn import Object, Program
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.idr import idr_for_each
from drgn.helpers.linux.irq import (
    for_each_irq_desc,
    irq_desc_chip_name,
    irq_desc_kstat_cpu,
)
from drgn.helpers.linux.list import list_for_each_entry

from ..core import ctypes as ct
from .decoders import WQ_FLAG_NAMES
from .registry import Entry, Subsystem, register


def _text(value) -> str:
    """Decode a helper's result, which is bytes for some and str for others."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def _action_names(desc) -> str:
    names = []
    action = desc.action
    while action:
        names.append(ct.safe(lambda: action.name.string_().decode("utf-8", "replace"), "?"))
        action = action.next
    return ",".join(names)


def irq_descs(prog: Program):
    """Every allocated IRQ line, busiest first.

    Ordering by count puts the timer and the virtio queues at the top, which
    is where a question about interrupt load starts. A line with no action has
    a descriptor but nothing bound to it.
    """
    cpus = list(for_each_online_cpu(prog))
    rows = []
    for irq, desc in for_each_irq_desc(prog):
        total = sum(int(irq_desc_kstat_cpu(desc, cpu)) for cpu in cpus)
        rows.append((total, int(irq), _action_names(desc), _text(irq_desc_chip_name(desc)), desc))
    rows.sort(key=lambda row: (-row[0], row[1]))
    for total, irq, names, chip, desc in rows:
        label = f"irq {irq:<4} {names or '(no action)'}"
        yield f"{label:<34} {total:>10}  {chip}", desc


def active_irq_descs(prog: Program):
    """Only the lines that have fired, which is what /proc/interrupts lists."""
    cpus = list(for_each_online_cpu(prog))
    for total, irq, names, chip, desc in sorted(
        (
            (
                sum(int(irq_desc_kstat_cpu(desc, cpu)) for cpu in cpus),
                int(irq),
                _action_names(desc),
                _text(irq_desc_chip_name(desc)),
                desc,
            )
            for irq, desc in for_each_irq_desc(prog)
        ),
        key=lambda row: (-row[0], row[1]),
    ):
        if total:
            label = f"irq {irq:<4} {names or '(no action)'}"
            yield f"{label:<34} {total:>10}  {chip}", desc


def softirq_vectors(prog: Program):
    """The ten softirq vectors and the function each one runs.

    ``softirq_vec`` is an array of handlers indexed by vector, and
    ``softirq_to_name`` names the same indices. A NULL handler means the vector
    is compiled in but nothing registered for it.
    """
    names = prog["softirq_to_name"]
    vec = prog["softirq_vec"]
    for index in range(len(names)):
        handler = vec[index].action
        symbol = ct.safe(lambda: prog.symbol(handler).name, "(none)") if handler else "(none)"
        name = ct.safe(lambda: names[index].string_().decode("utf-8", "replace"), "?")
        yield f"{index}  {name:<10} {symbol}", vec[index]


def workqueues(prog: Program):
    """Every registered workqueue, with the flags that decide where it runs.

    Names are not unique: several workqueues can be created with the same
    name, so the flags and max_active are what tell two rows apart.
    """
    for wq in list_for_each_entry(
        "struct workqueue_struct", prog["workqueues"].address_of_(), "list"
    ):
        name = ct.safe(lambda: wq.name.string_().decode("utf-8", "replace"), "?")
        flags = int(wq.flags)
        spelled = ",".join(text for bit, text in WQ_FLAG_NAMES if flags & bit) or "-"
        yield f"{name:<24} {spelled:<28} max_active {int(wq.max_active)}", wq


def worker_pools(prog: Program):
    """The pools the worker threads come from, per CPU and per priority.

    ``cpu`` is -1 for an unbound pool, which is not tied to one CPU. ``nice``
    separates the normal pool from the high-priority one on the same CPU.
    """
    for _, address in idr_for_each(prog["worker_pool_idr"].address_of_()):
        pool = Object(prog, "struct worker_pool *", address)
        cpu = int(pool.cpu)
        where = f"cpu {cpu}" if cpu >= 0 else "unbound"
        nice = ct.safe(lambda: int(pool.attrs.nice), 0)
        yield (
            f"pool {int(pool.id):<3} {where:<9} nice {nice:<4} "
            f"{int(pool.nr_workers)} worker(s), {int(pool.nr_idle)} idle",
            pool,
        )


def pending_work(prog: Program):
    """Work items queued on a pool but not yet picked up by a worker.

    Usually empty: a pool with an idle worker runs the item immediately, so a
    row here means the work was queued while every worker was busy.
    """
    for _, address in idr_for_each(prog["worker_pool_idr"].address_of_()):
        pool = Object(prog, "struct worker_pool *", address)
        for work in list_for_each_entry(
            "struct work_struct", pool.worklist.address_of_(), "entry"
        ):
            func = ct.safe(lambda: prog.symbol(work.func).name, "?")
            yield f"pool {int(pool.id)}  {func}", work


register(
    Subsystem(
        key="irq",
        label="irq",
        doc="Interrupt lines, softirq vectors, and the workqueues and pools that run deferred work.",
        entries=[
            Entry(
                "descs",
                "IRQ lines",
                "struct irq_desc per allocated line, with its actions, chip and total count.",
                irq_descs,
            ),
            Entry(
                "active",
                "IRQ lines that have fired",
                "The lines with a non-zero count, the set /proc/interrupts shows.",
                active_irq_descs,
            ),
            Entry(
                "softirqs",
                "softirq vectors",
                "softirq_vec, the handler registered for each vector.",
                softirq_vectors,
            ),
            Entry(
                "workqueues",
                "workqueues",
                "struct workqueue_struct instances, with flags and max_active.",
                workqueues,
            ),
            Entry(
                "pools",
                "worker pools",
                "struct worker_pool per CPU and priority, with worker and idle counts.",
                worker_pools,
            ),
            Entry(
                "pending",
                "work items waiting",
                "struct work_struct queued on a pool worklist, waiting for a worker.",
                pending_work,
            ),
        ],
    )
)
