"""Timers, and the hardware and bookkeeping that make them fire.

Two timer machines, with different promises:

* the timer wheel (``struct timer_list``, ``struct timer_base``) rounds to a
  jiffy and buckets timers by how far away they are. Cheap to add and cancel,
  imprecise on purpose, and used where a timeout is a deadline that normally
  never arrives.
* hrtimers (``struct hrtimer``, ``struct hrtimer_clock_base``) keep a per-CPU
  red-black tree ordered by nanosecond expiry, and program the clock event
  device for the earliest one. Used where the expiry itself is the point:
  ``nanosleep``, a scheduler deadline, a poll timeout.

Under both sits the hardware: a ``clocksource`` is what the kernel reads to
know the time, a ``clock_event_device`` is what it programs to be interrupted
at a given time, and the tick device is the per-CPU pairing of the two.

Every walk here reads a structure the kernel is still changing, without taking
its lock. A timer that fires is dequeued and freed, and a walk that was
following it then faults. So each queue is read on its own through
``_guarded``: a fault costs that one queue, which says so in a row of its own,
and the other CPUs still report.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from drgn import Object, Program, container_of
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.list import hlist_for_each_entry, list_for_each_entry
from drgn.helpers.linux.percpu import per_cpu
from drgn.helpers.linux.rbtree import rbtree_inorder_for_each_entry

from ..core import ctypes as ct
from .decoders import (
    CLOCK_EVENT_STATES,
    CLOCK_NAMES,
    HRTIMER_WAKEUP,
    TICK_MODES,
    WHEEL_BASES,
)
from .registry import Entry, Subsystem, register

# A row is a label and the object behind it, or the label alone when there is
# nothing to open.
Row = tuple[str, "Object | None"]


def _guarded(walk: Callable[[], Iterator[Row]], describe: Callable[[str], str]) -> list[Row]:
    """Every row from ``walk``, or one row saying the structure changed.

    The rows are collected before any are handed back, so that a fault part
    way through a queue reports the queue as unreadable rather than showing
    half of it as if that were all of it. ``describe`` turns the exception's
    name into a row that says which queue was lost.
    """
    rows: list[Row] = []
    try:
        rows.extend(walk())
    except Exception as exc:  # noqa: BLE001 - live structures, read unlocked
        return [(describe(type(exc).__name__), None)]
    return rows


def _symbol(obj: Object, func) -> str:
    return ct.safe(lambda: obj.prog_.symbol(func).name, "?")


def _wheel_base_name(index: int, count: int) -> str:
    names = WHEEL_BASES.get(count)
    return names[index] if names else f"base {index}"


def _timer_owner(timer: Object, func: str) -> str:
    """Who armed this timer, for the one callback that makes that recoverable.

    A timer is a callback and an expiry; nothing in it says who it is for.
    ``hrtimer_wakeup`` is the exception: the struct holding the timer can be
    recovered from it, and it holds the sleeping task. Naming the task is the
    difference between a list of identical callbacks and a list you can act on.
    """
    if func != HRTIMER_WAKEUP:
        return ""
    task = ct.safe(
        lambda: container_of(timer, "struct hrtimer_sleeper", "timer").task, None
    )
    if task is None or not ct.safe(lambda: task.value_(), 0):
        return ""
    pid = ct.safe(lambda: int(task.pid), None)
    if pid is None:
        return ""
    comm = ct.safe(lambda: task.comm.string_().decode("utf-8", "replace"), "?")
    return f"waking {pid} {comm}"


def _clock_bases(cpu_base: Object) -> Iterator[tuple[Object, str]]:
    """Each clock base on this CPU, with the prefix its rows carry.

    The array holds each clock twice: the first half fires in the interrupt,
    the second in the TIMER softirq. Which half a base is in is its position,
    so the split is read from the array's length rather than written as a
    constant that a kernel adding a clock would make wrong.
    """
    bases = cpu_base.clock_base
    hard_bases = len(bases) // 2
    cpu = int(cpu_base.cpu)
    for index in range(len(bases)):
        base = bases[index]
        clockid = ct.safe(lambda: int(base.clockid), None)
        clock = CLOCK_NAMES.get(clockid, "?" if clockid is None else str(clockid))
        half = "hard" if index < hard_bases else "soft"
        yield base, f"cpu {cpu}  {clock:<9} {half}  "


def hrtimer_queues(prog: Program):
    """Every hrtimer currently queued, per CPU and per clock base.

    Each queue is a red-black tree ordered by expiry, so the rows come out in
    the order the CPU will fire them.
    """
    for cpu in for_each_online_cpu(prog):
        cpu_base = per_cpu(prog["hrtimer_bases"], cpu)
        for base, prefix in _clock_bases(cpu_base):
            yield from _guarded(
                lambda base=base, prefix=prefix: _base_timers(base, prefix),
                lambda why, prefix=prefix: f"{prefix}(queue changed while being read: {why})",
            )


def _base_timers(base: Object, prefix: str) -> Iterator[Row]:
    for node in rbtree_inorder_for_each_entry(
        "struct timerqueue_node", base.active.rb_root.rb_root.address_of_(), "node"
    ):
        timer = container_of(node, "struct hrtimer", "node")
        func = _symbol(timer, timer.function)
        # Padded so the owner column lines up, then trimmed: a timer with no
        # recoverable owner should not end in the width of a missing column.
        yield f"{prefix}{func:<24}{_timer_owner(timer, func)}".rstrip(), timer


def hrtimer_cpu_bases(prog: Program):
    """The per-CPU hrtimer state: how many have fired, and how it is driven.

    "high-res" means this CPU programs the clock event device per timer.
    Without it the timers are only checked on the tick, which is the fallback
    when no device can be programmed that finely.
    """
    for cpu in for_each_online_cpu(prog):
        base = per_cpu(prog["hrtimer_bases"], cpu)
        resolution = "high-res" if base.hres_active.value_() else "low-res"
        yield (
            f"cpu {cpu}  {resolution}  {int(base.nr_events)} events, "
            f"{int(base.nr_hangs)} hangs",
            base.address_of_(),
        )


def _wheel_state(base: Object) -> str:
    """What this base is holding.

    ``timers_pending`` is a bool, not a count: it says only whether anything
    is queued. ``pending_map`` has one bit per bucket, so its set bits are the
    buckets in use, and a bucket can hold several timers. Neither is a timer
    count, and neither is presented as one.
    """
    if not ct.safe(lambda: base.timers_pending.value_(), False):
        return "nothing queued"
    buckets = ct.safe(
        lambda: sum(bin(int(word)).count("1") for word in base.pending_map), None
    )
    return f"queued, in {buckets} bucket(s)" if buckets is not None else "queued"


def timer_wheel_bases(prog: Program):
    """The timer wheel bases, one per kind of timer per CPU.

    A deferrable timer is one an idle CPU may leave until it wakes for
    something else; a global one may be taken over by another CPU, while a
    pinned one may not. How many bases exist depends on the build, so they are
    named by position rather than assumed.
    """
    for cpu in for_each_online_cpu(prog):
        bases = per_cpu(prog["timer_bases"], cpu)
        count = len(bases)
        for index in range(count):
            base = bases[index]
            kind = _wheel_base_name(index, count)
            yield f"cpu {cpu}  {kind:<20} {_wheel_state(base)}", base.address_of_()


def _bucket_timers(base: Object, vector: int, prefix: str, now: int) -> Iterator[Row]:
    for timer in hlist_for_each_entry(
        "struct timer_list", base.vectors[vector].address_of_(), "entry"
    ):
        func = _symbol(timer, timer.function)
        yield f"{prefix}{func:<28} {int(timer.expires) - now:>8} jiffies", timer


def wheel_timers(prog: Program):
    """Every timer queued on the wheel, with the function it will call.

    Buckets are read one at a time, for the reason given at the top of this
    file: one bucket's worth is a smaller loss than the whole wheel.
    """
    now = int(prog["jiffies"])
    for cpu in for_each_online_cpu(prog):
        bases = per_cpu(prog["timer_bases"], cpu)
        count = len(bases)
        for index in range(count):
            base = bases[index]
            prefix = f"cpu {cpu}  {_wheel_base_name(index, count):<20} "
            for vector in range(len(base.vectors)):
                yield from _guarded(
                    lambda base=base, vector=vector: _bucket_timers(
                        base, vector, prefix, now
                    ),
                    lambda why, vector=vector: (
                        f"{prefix}(bucket {vector} changed while being read: {why})"
                    ),
                )


def clocksources(prog: Program):
    """What the kernel reads to know what time it is, best rating first.

    The order is the list's own: ``clocksource_enqueue`` inserts each source
    ahead of the first one with a lower rating, so the head of the list is the
    source in use.
    """
    for source in list_for_each_entry(
        "struct clocksource", prog["clocksource_list"].address_of_(), "list"
    ):
        name = ct.safe(lambda: source.name.string_().decode("utf-8", "replace"), "?")
        yield f"{name:<20} rating {int(source.rating)}", source


def clockevent_devices(prog: Program):
    """What the kernel programs to be interrupted at a given time."""
    for device in list_for_each_entry(
        "struct clock_event_device", prog["clockevent_devices"].address_of_(), "list"
    ):
        name = ct.safe(lambda: device.name.string_().decode("utf-8", "replace"), "?")
        raw = int(device.state_use_accessors)
        state = CLOCK_EVENT_STATES.get(raw, str(raw))
        yield f"{name:<16} rating {int(device.rating):<4} {state}", device


def tick_devices(prog: Program):
    """The clock event device each CPU takes its tick from."""
    for cpu in for_each_online_cpu(prog):
        device = per_cpu(prog["tick_cpu_device"], cpu)
        name = ct.safe(
            lambda: device.evtdev.name.string_().decode("utf-8", "replace"), "none"
        )
        raw = int(device.mode)
        mode = TICK_MODES.get(raw, str(raw))
        yield f"cpu {cpu}  {mode:<9} {name}", device.address_of_()


register(
    Subsystem(
        key="time",
        label="time",
        doc="Timer wheel and hrtimer queues, clocksources, clock event devices and the tick.",
        entries=[
            Entry(
                "hrtimers",
                "hrtimers queued",
                "struct hrtimer in each CPU's clock bases, in expiry order.",
                hrtimer_queues,
            ),
            Entry(
                "hrtimer_bases",
                "hrtimer state per CPU",
                "struct hrtimer_cpu_base: resolution, events fired, hangs.",
                hrtimer_cpu_bases,
            ),
            Entry(
                "wheel_timers",
                "timer wheel timers",
                "struct timer_list queued in the wheel buckets.",
                wheel_timers,
            ),
            Entry(
                "wheel_bases",
                "timer wheel bases",
                "struct timer_base per CPU: pinned, migratable and deferrable.",
                timer_wheel_bases,
            ),
            Entry(
                "clocksources",
                "clocksources",
                "struct clocksource: what the kernel reads to tell the time.",
                clocksources,
            ),
            Entry(
                "clockevents",
                "clock event devices",
                "struct clock_event_device: what it programs to be interrupted.",
                clockevent_devices,
            ),
            Entry(
                "tick",
                "tick devices",
                "struct tick_device per CPU, and the mode it runs in.",
                tick_devices,
            ),
        ],
    )
)
