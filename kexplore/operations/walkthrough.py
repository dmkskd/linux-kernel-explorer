"""Walkthroughs: an operation described as an ordered sequence of steps.

The structure browser answers "what is in this struct". A walkthrough answers
"what happens during this operation, and which structures take part". The two
are separate views over the same kernel, not replacements for each other.

Each step names a kernel function, which is resolved to file and line at
display time, and optionally supplies the structures involved so they can be
opened in the structure browser.

These sequences are hand-written. They describe the common path and skip error
handling and special cases, so a step may correspond to several functions in
the source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

from drgn import Object, Program

# A step's structures resolver returns labelled objects to navigate into.
Resolver = Callable[[Program], "Iterator[tuple[str, Object]]"]


@dataclass(frozen=True)
class Step:
    """One stage of an operation."""

    function: str
    summary: str
    detail: str = ""
    # Structures this step reads or writes, resolved live.
    structures: Resolver | None = None


@dataclass(frozen=True)
class Walkthrough:
    key: str
    label: str
    subsystem: str
    doc: str
    steps: list[Step] = field(default_factory=list)


def _rq_of_current(prog: Program):
    from drgn.helpers.linux.cpumask import for_each_online_cpu
    from drgn.helpers.linux.percpu import per_cpu

    for cpu in for_each_online_cpu(prog):
        yield f"cpu{cpu} runqueue", per_cpu(prog["runqueues"], cpu).address_of_()


def _a_running_task(prog: Program):
    from drgn.helpers.linux.cpumask import for_each_online_cpu
    from drgn.helpers.linux.sched import cpu_curr

    for cpu in for_each_online_cpu(prog):
        task = cpu_curr(prog, cpu)
        yield f"cpu{cpu} curr: {task.comm.string_().decode()}", task


def _cfs_rqs(prog: Program):
    from drgn.helpers.linux.cpumask import for_each_online_cpu
    from drgn.helpers.linux.list import list_for_each_entry
    from drgn.helpers.linux.percpu import per_cpu
    from drgn.helpers.linux.sched import task_group_name

    for cpu in for_each_online_cpu(prog):
        rq = per_cpu(prog["runqueues"], cpu)
        for cfs in list_for_each_entry(
            "struct cfs_rq", rq.leaf_cfs_rq_list.address_of_(), "leaf_cfs_rq_list"
        ):
            try:
                name = task_group_name(cfs.tg)
                name = name.decode() if isinstance(name, bytes) else str(name)
            except Exception:  # noqa: BLE001
                name = "?"
            yield f"cpu{cpu} cfs_rq {name}", cfs


def _init_mm(prog: Program):
    yield "init_mm", prog["init_mm"].address_of_()


def _vmas_of_init(prog: Program):
    from drgn.helpers.linux.mm import for_each_vma, vma_name
    from drgn.helpers.linux.pid import find_task

    task = find_task(prog, 1)
    for vma in for_each_vma(task.mm):
        name = vma_name(vma)
        label = name.decode("utf-8", "replace") if name else "anon"
        yield f"{vma.vm_start.value_():#x} {label}", vma


WAKEUP = Walkthrough(
    key="wakeup",
    label="a task is woken and starts running",
    subsystem="sched",
    doc="From another task calling wake_up to this task executing on a CPU.",
    steps=[
        Step(
            "try_to_wake_up",
            "Entry point. Checks the task is really blocked and claims it.",
            "Emits sched_waking. From here the task is committed to being woken, "
            "so this is the start of the interval that 'how long do runnable "
            "tasks wait for a CPU' measures.",
            _a_running_task,
        ),
        Step(
            "select_task_rq_fair",
            "Chooses which CPU the task should run on.",
            "Walks the sched_domain topology looking for an idle CPU near the "
            "waker, balancing cache locality against queue depth.",
            _rq_of_current,
        ),
        Step(
            "enqueue_task_fair",
            "Adds the task's sched_entity to that CPU's cfs_rq.",
            "With group scheduling the entity is added to the cfs_rq of its "
            "cgroup, not the root one, and each parent entity is enqueued in "
            "turn up the hierarchy.",
            _cfs_rqs,
        ),
        Step(
            "ttwu_do_activate",
            "Marks the task runnable and requests a reschedule.",
            "If the target CPU is not the current one, the task is queued to it "
            "and an IPI is sent; sched_wakeup is emitted on the target after the "
            "IPI is handled.",
        ),
        Step(
            "__schedule",
            "The target CPU picks a new task.",
            "Marked notrace, so it cannot be probed. Its work is visible through "
            "the functions it calls and the sched_switch tracepoint it emits.",
        ),
        Step(
            "pick_next_task_fair",
            "Selects the entity with the earliest eligible deadline.",
            "Descends the cfs_rq hierarchy, picking a group entity at each level "
            "until it reaches a task entity.",
            _cfs_rqs,
        ),
        Step(
            "context_switch",
            "Switches address space and registers to the new task.",
            "Emits sched_switch, which ends the wait interval and begins the "
            "on-CPU interval.",
            _a_running_task,
        ),
    ],
)

PAGE_FAULT = Walkthrough(
    key="page_fault",
    label="a page fault is resolved",
    subsystem="mm",
    doc="From a userspace access on an unmapped address to a mapped page.",
    steps=[
        Step(
            "do_page_fault",
            "Architecture entry point for the fault.",
            "Reads the faulting address and the fault flags, then decides "
            "whether this is a user or kernel fault.",
        ),
        Step(
            "find_vma",
            "Finds the vm_area_struct covering the faulting address.",
            "No VMA means the address was never mapped, and the fault becomes "
            "SIGSEGV. The VMA's flags decide whether the access is permitted.",
            _vmas_of_init,
        ),
        Step(
            "handle_mm_fault",
            "Walks the page tables down to the failing level.",
            "Descends pgd, p4d, pud, pmd to the pte, allocating table pages "
            "where they are missing.",
            _init_mm,
        ),
        Step(
            "do_anonymous_page",
            "Allocates a page for anonymous memory.",
            "A read fault on untouched anonymous memory maps the shared zero "
            "page instead of allocating, so only a write allocates.",
        ),
        Step(
            "alloc_pages",
            "Takes a page from the buddy allocator.",
            "Chooses a zone from the node's zonelist, then removes a block of "
            "the requested order from that zone's free_area.",
        ),
        Step(
            "set_pte_at",
            "Installs the page table entry.",
            "After this the mapping exists and the faulting instruction is "
            "restarted, this time succeeding.",
        ),
    ],
)

WALKTHROUGHS = [WAKEUP, PAGE_FAULT]
