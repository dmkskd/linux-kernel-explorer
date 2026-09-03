"""Process-centric entry points.

Three terms, used precisely throughout:

* **task** -- one ``task_struct``, and what ends up running on a CPU. The
  scheduler picks ``sched_entity``s and descends through group entities until
  it reaches a task's, so a task is the end of that descent, not the only
  thing scheduled.
* **thread** -- a task seen as a member of a thread group. Every task is a
  thread, the leader included.
* **process** -- the *thread group*: the tasks sharing a ``signal_struct``,
  named by its leader's pid (== tgid). It is a set, not an object, because the
  kernel has no process struct.

So these entries list one task per thread group -- the leader -- because that
is the task userspace would call the process. The rest of a group is reached
through the ``threads`` link.
"""

from __future__ import annotations

from drgn import Program
from drgn.helpers.linux.kthread import task_is_kthread
from drgn.helpers.linux.pid import find_task, for_each_task
from drgn.helpers.linux.sched import task_state_to_char

from ..core import ctypes as ct
from .registry import Entry, Subsystem, register
from .format import task_comm

# include/linux/sched.h
EXIT_ZOMBIE = 0x20


def _is_leader(task) -> bool:
    return task.pid == task.tgid


# Every list here is a list of tasks, so they answer with the same columns and
# insert one more only where that entry has something of its own to report.
TASK_COLUMNS = ("pid", "state", "command")
OBJECT_COLUMNS = ("type", "address")


def task_columns(*extra: str) -> tuple[str, ...]:
    return TASK_COLUMNS + extra + OBJECT_COLUMNS


def _cells(task, *extra: str) -> tuple[str, ...]:
    """One task as columns: what it is to userspace, then what it is here.

    The type and address repeat on every row, which is the point: a table of
    pid, state and command is ps output, and these two say that each row is a
    kernel structure at an address you can read.
    """
    return (
        # Right-aligned by padding: the table sizes a column to its widest cell
        # and left-aligns everything in it, so a ragged pid column is the only
        # other option.
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
        doc="Group leaders, the threads in their groups, and kernel threads. "
            "Every row is one task_struct.",
        entries=[
            Entry(
                "init",
                "init (pid 1)",
                "The task that leads pid 1's group, as somewhere to start.",
                init,
                columns=task_columns(),
            ),
            Entry(
                "processes",
                "processes",
                "Thread group leaders, one row each.",
                processes,
                columns=task_columns("threads"),
            ),
            Entry(
                "tasks",
                "all tasks",
                "Every task, threads included.",
                all_tasks,
                columns=task_columns("thread of"),
            ),
            Entry(
                "kthreads",
                "kernel threads",
                "Tasks with PF_KTHREAD set. Each leads a group of its own.",
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
