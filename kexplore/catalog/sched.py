"""Scheduler: runqueues, tasks, and what is actually running."""

from __future__ import annotations

from drgn import Program
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.percpu import per_cpu
from drgn.helpers.linux.sched import cpu_curr

from .registry import Entry, Subsystem, register
from .format import task_comm


def runqueues(prog: Program):
    for cpu in for_each_online_cpu(prog):
        yield f"cpu{cpu}", per_cpu(prog["runqueues"], cpu)


def running(prog: Program):
    for cpu in for_each_online_cpu(prog):
        task = cpu_curr(prog, cpu)
        yield f"cpu{cpu}: {task_comm(task)} [{task.pid.value_()}]", task


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
        ],
    )
)
