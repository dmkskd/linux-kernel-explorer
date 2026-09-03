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

from .registry import Entry, Subsystem, register
from .format import task_comm

# include/linux/sched.h
EXIT_ZOMBIE = 0x20


def _is_leader(task) -> bool:
    return task.pid == task.tgid


def processes(prog: Program):
    """Group leaders only -- one row per group, as ps would show."""
    for task in for_each_task(prog):
        if not _is_leader(task):
            continue
        threads = task.signal.nr_threads.value_()
        state = task_state_to_char(task)
        suffix = f"  ({threads} threads)" if threads > 1 else ""
        yield f"{task.pid.value_():>7} {state}  {task_comm(task)}{suffix}", task


def multithreaded(prog: Program):
    """Group leaders whose group has more than one thread."""
    for task in for_each_task(prog):
        if not _is_leader(task):
            continue
        threads = task.signal.nr_threads.value_()
        if threads > 1:
            yield f"{task.pid.value_():>7}  {task_comm(task)}  ({threads} threads)", task


def kernel_threads(prog: Program):
    """Tasks with PF_KTHREAD set.

    Not ``mm == NULL``: an exiting userspace task has already had its mm
    dropped by exit_mm(), so a zombie matches that test without being a kernel
    thread. PF_KTHREAD is the flag the kernel itself checks.
    """
    for task in for_each_task(prog):
        if task_is_kthread(task):
            yield f"{task.pid.value_():>7} {task_state_to_char(task)}  {task_comm(task)}", task


def no_mm(prog: Program):
    """Tasks with no address space, kthread or not.

    Differs from the PF_KTHREAD list by exiting userspace tasks, which is the
    point of listing it separately.
    """
    for task in for_each_task(prog):
        if not task.mm.value_():
            kind = "kthread" if task_is_kthread(task) else "userspace, exiting"
            yield f"{task.pid.value_():>7} {task_comm(task)}  [{kind}]", task


def zombies(prog: Program):
    """Tasks in EXIT_ZOMBIE: exited, not yet reaped by their parent."""
    for task in for_each_task(prog):
        if task.exit_state.value_() & EXIT_ZOMBIE:
            yield f"{task.pid.value_():>7} {task_comm(task)}", task


register(
    Subsystem(
        key="process",
        label="process",
        doc="Group leaders, the threads in their groups, and kernel threads. "
            "Every row is one task_struct.",
        entries=[
            Entry(
                "processes",
                "processes",
                "One task per group: the leader. Follow 'threads' for the rest.",
                processes,
            ),
            Entry(
                "multithreaded",
                "multithreaded only",
                "Leaders of groups with nr_threads > 1.",
                multithreaded,
            ),
            Entry(
                "kthreads",
                "kernel threads",
                "Tasks with PF_KTHREAD set. Each leads a group of its own.",
                kernel_threads,
            ),
            Entry(
                "no_mm",
                "tasks with no mm",
                "mm == NULL: kthreads plus exiting userspace tasks.",
                no_mm,
            ),
            Entry(
                "zombies",
                "zombies",
                "exit_state & EXIT_ZOMBIE: exited, not yet reaped.",
                zombies,
            ),
            Entry("init", "init (pid 1)", "The task that leads pid 1's group.",
                  lambda prog: find_task(prog, 1)),
        ],
    )
)
