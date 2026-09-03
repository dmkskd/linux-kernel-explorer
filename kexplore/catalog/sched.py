"""Scheduler: runqueues, tasks, and what is actually running."""

from __future__ import annotations

from drgn import Program
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.percpu import per_cpu
from drgn.helpers.linux.pid import for_each_task
from drgn.helpers.linux.sched import cpu_curr, task_state_to_char

from .registry import Entry, Subsystem, register
from .format import task_comm


def runqueues(prog: Program):
    for cpu in for_each_online_cpu(prog):
        yield f"cpu{cpu}", per_cpu(prog["runqueues"], cpu)


def running(prog: Program):
    for cpu in for_each_online_cpu(prog):
        task = cpu_curr(prog, cpu)
        yield f"cpu{cpu}: {task_comm(task)} [{task.pid.value_()}]", task


def tasks(prog: Program):
    for task in for_each_task(prog):
        state = task_state_to_char(task)
        yield f"{task.pid.value_():>7} {state}  {task_comm(task)}", task


register(
    Subsystem(
        key="sched",
        label="sched",
        doc="CFS/EEVDF runqueues and the task structures they schedule.",
        entries=[
            Entry(
                "runqueues",
                "runqueues (per-cpu)",
                "struct rq per online CPU: nr_running, nr_switches, curr, idle.",
                runqueues,
            ),
            Entry(
                "running",
                "currently running",
                "rq->curr for each online CPU -- what is on-cpu right now.",
                running,
            ),
            Entry(
                "init_task",
                "init_task",
                "The root of the task list; every task links back here.",
                lambda prog: prog["init_task"],
            ),
            Entry(
                "tasks",
                "all tasks",
                "Every task_struct, walked via the pid hash.",
                tasks,
            ),
        ],
    )
)
