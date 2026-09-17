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

Times here are absolute values in the clock each timer is queued against. The
"expires in" row compares against the same clock read from userspace, which is
only meaningful for the live local kernel, and says so.
"""

from __future__ import annotations

from drgn import Object, Program, container_of
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.list import hlist_for_each_entry, list_for_each_entry
from drgn.helpers.linux.percpu import per_cpu
from drgn.helpers.linux.rbtree import rbtree_inorder_for_each_entry

from ..core import ctypes as ct
from .decoders import (
    CLOCK_EVENT_STATES,
    CLOCK_NAMES,
    TICK_MODES,
    WHEEL_BASES,
)
from .registry import Entry, Subsystem, register


def _wheel_base_name(index: int, count: int) -> str:
    names = WHEEL_BASES.get(count)
    return names[index] if names else f"base {index}"


def _timer_owner(timer: Object, func: str) -> str:
    """Who armed this timer, for the callbacks that make that recoverable.

    A timer is a callback and an expiry; nothing in it says who it is for. Two
    callbacks are the exception, because the struct holding the timer can be
    recovered from it: hrtimer_wakeup means a task is sleeping on it, and a
    row that names the task is the difference between a list of identical
    callbacks and a list you can act on.
    """
    if func != "hrtimer_wakeup":
        return ""
    task = ct.safe(
        lambda: container_of(timer, "struct hrtimer_sleeper", "timer").task, None
    )
    if task is None or not ct.safe(lambda: task.value_(), 0):
        return ""
    pid = ct.safe(lambda: int(task.pid), None)
    comm = ct.safe(lambda: task.comm.string_().decode("utf-8", "replace"), "?")
    return f"waking {pid} {comm}" if pid is not None else ""


def hrtimer_queues(prog: Program):
    """Every hrtimer currently queued, per CPU and per clock base.

    The queue is a red-black tree ordered by expiry, so the rows come out in
    the order the CPU will fire them. A base with index >= 4 is the soft half:
    its timers run in the TIMER softirq rather than in the interrupt itself.
    """
    for cpu in for_each_online_cpu(prog):
        cpu_base = per_cpu(prog["hrtimer_bases"], cpu)
        for index in range(len(cpu_base.clock_base)):
            base = cpu_base.clock_base[index]
            clock = CLOCK_NAMES.get(int(base.clockid), str(int(base.clockid)))
            soft = "soft" if int(base.index) >= 4 else "hard"
            for node in rbtree_inorder_for_each_entry(
                "struct timerqueue_node", base.active.rb_root.rb_root.address_of_(), "node"
            ):
                timer = container_of(node, "struct hrtimer", "node")
                func = ct.safe(lambda: prog.symbol(timer.function).name, "?")
                yield f"cpu {cpu}  {clock:<9} {soft}  {func:<24}{_timer_owner(timer, func)}", timer


def hrtimer_cpu_bases(prog: Program):
    """The per-CPU hrtimer state: how many have fired, and what is next."""
    for cpu in for_each_online_cpu(prog):
        base = per_cpu(prog["hrtimer_bases"], cpu)
        resolution = "high-res" if int(base.hres_active) else "low-res"
        yield (
            f"cpu {cpu}  {resolution}  {int(base.nr_events)} events, "
            f"{int(base.nr_hangs)} hangs",
            base.address_of_(),
        )


def timer_wheel_bases(prog: Program):
    """The timer wheel bases, two per CPU.

    A deferrable timer is one an idle CPU may leave until it wakes for
    something else; a global one may be taken over by another CPU, while a
    pinned one may not.
    """
    for cpu in for_each_online_cpu(prog):
        bases = per_cpu(prog["timer_bases"], cpu)
        count = len(bases)
        for index in range(count):
            base = bases[index]
            kind = _wheel_base_name(index, count)
            yield (
                f"cpu {cpu}  {kind:<20} {int(base.timers_pending)} pending",
                base.address_of_(),
            )


def _wheel_timers(base: Object):
    """The timers in one wheel base, walking every bucket."""
    for vector in range(len(base.vectors)):
        yield from hlist_for_each_entry(
            "struct timer_list", base.vectors[vector].address_of_(), "entry"
        )


def wheel_timers(prog: Program):
    """Every timer queued on the wheel, with the function it will call."""
    now = int(prog["jiffies"])
    for cpu in for_each_online_cpu(prog):
        bases = per_cpu(prog["timer_bases"], cpu)
        count = len(bases)
        for index in range(count):
            kind = _wheel_base_name(index, count)
            for timer in _wheel_timers(bases[index]):
                func = ct.safe(lambda: prog.symbol(timer.function).name, "?")
                delta = int(timer.expires) - now
                yield f"cpu {cpu}  {kind:<20} {func:<28} {delta:>8} jiffies", timer


def clocksources(prog: Program):
    """What the kernel reads to know what time it is, best rating first."""
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
        state = CLOCK_EVENT_STATES.get(
            int(device.state_use_accessors), str(int(device.state_use_accessors))
        )
        yield f"{name:<16} rating {int(device.rating):<4} {state}", device


def tick_devices(prog: Program):
    """The clock event device each CPU takes its tick from."""
    for cpu in for_each_online_cpu(prog):
        device = per_cpu(prog["tick_cpu_device"], cpu)
        name = ct.safe(
            lambda: device.evtdev.name.string_().decode("utf-8", "replace"), "none"
        )
        mode = TICK_MODES.get(int(device.mode), str(int(device.mode)))
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
