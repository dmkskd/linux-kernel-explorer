"""Process-centric entry points.

These terms are used as follows:

* **task**: one ``task_struct``, the entity that runs on a CPU. The scheduler
  selects ``sched_entity`` structures and descends through group entities
  until it reaches a task's entity, so a task is the end of that descent, not
  the only entity scheduled.
* **thread**: a task as a member of a thread group. Every task is a thread,
  including the group leader.
* **process**: a thread group, the tasks that share a ``signal_struct``,
  identified by its leader's pid (equal to the tgid). The kernel has no process
  struct, so a process is a set of tasks, not an object.

The entries list one task per thread group, the leader, because that is the
task userspace calls the process. The other tasks of a group are reached
through the ``threads`` link.
"""

from __future__ import annotations

from drgn import Program
from drgn.helpers.linux.kthread import task_is_kthread
from drgn.helpers.linux.pid import find_task, for_each_task
from drgn.helpers.linux.sched import task_state_to_char

from ..core import ctypes as ct
from .format import task_comm
from .registry import Entry, Subsystem, register

# include/linux/sched.h
EXIT_ZOMBIE = 0x20


def _is_leader(task) -> bool:
    return task.pid == task.tgid


# Every list here lists tasks, so all use the same columns, with an extra
# column where an entry reports a property specific to it.
TASK_COLUMNS = ("pid", "state", "command")
OBJECT_COLUMNS = ("type", "address")


def task_columns(*extra: str) -> tuple[str, ...]:
    return TASK_COLUMNS + extra + OBJECT_COLUMNS


def _cells(task, *extra: str) -> tuple[str, ...]:
    """One task as columns: its userspace identity, then its kernel object.

    pid, state and command are the columns ps prints. The type and address,
    repeated on every row, identify each row as a kernel structure at a
    readable address.
    """
    return (
        # Right-aligned by padding, because the table left-aligns every
        # column.
        f"{task.pid.value_():>7}",
        task_state_to_char(task),
        task_comm(task),
        *extra,
        ct.type_name(task.type_),
        f"{task.value_():#x}",
    )


def processes(prog: Program):
    """Group leaders only -- one row per group, as ps would show."""
    for task in for_each_task(prog):
        if not _is_leader(task):
            continue
        threads = task.signal.nr_threads.value_()
        yield _cells(task, str(threads) if threads > 1 else ""), task


def init(prog: Program):
    """Pid 1, as a one-row list so it carries the same columns as the rest."""
    task = find_task(prog, 1)
    if task:
        yield _cells(task), task


def all_tasks(prog: Program):
    """Every task, threads included -- the same walk without the leader filter."""
    for task in for_each_task(prog):
        yield _cells(task, "" if _is_leader(task) else f"of {task.tgid.value_()}"), task


def kernel_threads(prog: Program):
    """Tasks with PF_KTHREAD set.

    Not ``mm == NULL``: an exiting userspace task has already had its mm
    dropped by exit_mm(), so a zombie matches that test without being a kernel
    thread. PF_KTHREAD is the flag the kernel itself checks.
    """
    for task in for_each_task(prog):
        if task_is_kthread(task):
            yield _cells(task), task


def zombies(prog: Program):
    """Tasks in EXIT_ZOMBIE: exited, not yet reaped by their parent."""
    for task in for_each_task(prog):
        if task.exit_state.value_() & EXIT_ZOMBIE:
            yield _cells(task), task


register(
    Subsystem(
        key="process",
        label="process",
        doc="task_struct instances grouped as thread-group leaders, tasks, kernel threads, and zombies.",
        entries=[
            Entry(
                "init",
                "init (pid 1)",
                "Thread-group leader for PID 1.",
                init,
                columns=task_columns(),
            ),
            Entry(
                "processes",
                "processes",
                "Thread-group leaders (task->pid == task->tgid).",
                processes,
                columns=task_columns("threads"),
            ),
            Entry(
                "tasks",
                "all tasks",
                "All task_struct instances, including non-leader threads.",
                all_tasks,
                columns=task_columns("thread of"),
            ),
            Entry(
                "kthreads",
                "kernel threads",
                "Tasks with task->flags & PF_KTHREAD.",
                kernel_threads,
                columns=task_columns(),
            ),
            Entry(
                "zombies",
                "zombies",
                "exit_state & EXIT_ZOMBIE: exited, not yet reaped by the parent.",
                zombies,
                columns=task_columns(),
            ),
        ],
    )
)
