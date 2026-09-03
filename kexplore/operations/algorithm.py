"""Algorithm views: the decision a kernel routine would make, and why. (WIP)

The structure browser shows values. Walkthroughs show a sequence. Neither shows
the reasoning: given this state, which task runs next, and what made it win.

An algorithm view states the rule, lists the inputs the rule considers, and
marks the outcome. The point is that the answer is recomputed from live state
rather than asserted, so it can be checked against what the kernel actually did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

from drgn import Object, Program


@dataclass(frozen=True)
class Observation:
    """One line of an algorithm's reasoning."""

    label: str
    value: str = ""
    why: str = ""
    # Only set this for objects that outlive the analysis. An experiment kills
    # what it created, so its rows must not be followable.
    obj: Object | None = None
    # Explanation for the hint line, keeping it out of the columns.
    doc_for: str = ""
    # Matrix views supply their own cells instead of label/value/why.
    cells: tuple[str, ...] | None = None
    # A drill-down for this row, and the columns it wants. Used when a summary
    # cell ("shared") hides the evidence behind it (the two pointers).
    expand: "Callable[[], list]" | None = None
    expand_columns: tuple[str, ...] | None = None
    # "heading", "input", "result" -- affects presentation only.
    kind: str = "input"


@dataclass(frozen=True)
class Algorithm:
    key: str
    label: str
    subsystem: str
    rule: str
    doc: str
    analyse: Callable[[Program], "Iterator[Observation]"] = field(repr=False)
    # Each algorithm names its own columns; a comparison is not a list of inputs.
    columns: tuple[str, ...] = ("input", "value", "why")
    # Experiments run a program and take seconds; the UI runs them in a worker
    # rather than blocking while they finish.
    background: bool = False

    def run(self, prog: Program) -> list[Observation]:
        try:
            return list(self.analyse(prog))
        except Exception as exc:  # noqa: BLE001 - report rather than crash the view
            return [Observation(f"{type(exc).__name__}: {exc}", kind="result")]


def _eevdf_pick(prog: Program) -> Iterator[Observation]:
    """Recompute which entity EEVDF would choose, per runqueue."""
    from drgn.helpers.linux.cpumask import for_each_online_cpu
    from drgn.helpers.linux.list import list_for_each_entry
    from drgn.helpers.linux.percpu import per_cpu
    from drgn.helpers.linux.rbtree import rbtree_inorder_for_each_entry
    from drgn.helpers.linux.sched import (
        sched_entity_is_task,
        sched_entity_to_task,
        task_group_name,
    )

    for cpu in for_each_online_cpu(prog):
        rq = per_cpu(prog["runqueues"], cpu)
        curr = rq.curr
        running = rq.nr_running.value_()
        yield Observation(
            f"cpu{cpu}",
            f"running {curr.comm.string_().decode()}[{curr.pid.value_()}]",
            f"nr_running={running}",
            rq.address_of_(),
            kind="heading",
        )
        if running == 0:
            yield Observation(
                "  idle",
                "nothing to pick",
                "no runnable tasks; the CPU is in the idle task waiting for a wakeup",
                kind="result",
            )
            continue

        for cfs in list_for_each_entry(
            "struct cfs_rq", rq.leaf_cfs_rq_list.address_of_(), "leaf_cfs_rq_list"
        ):
            queued = cfs.nr_queued.value_()
            if not queued:
                continue
            try:
                name = task_group_name(cfs.tg)
                name = name.decode() if isinstance(name, bytes) else str(name)
            except Exception:  # noqa: BLE001
                name = "?"

            zero = cfs.zero_vruntime.value_()
            weighted = cfs.sum_w_vruntime.value_()
            weight = cfs.sum_weight.value_()
            average = zero + (weighted // weight if weight else 0)

            yield Observation(
                f"  cfs_rq {name}",
                f"nr_queued={queued}",
                "one runqueue per cgroup; the scheduler descends this hierarchy",
                # list_for_each_entry already yields a pointer.
                cfs,
                kind="heading",
            )
            if weight:
                yield Observation(
                    "    avg_vruntime",
                    str(average),
                    f"zero_vruntime {zero} + sum_w_vruntime {weighted} "
                    f"/ sum_weight {weight}; entities behind this are eligible",
                )
            else:
                yield Observation(
                    "    avg_vruntime",
                    "not defined",
                    "sum_weight is 0: nothing is queued to average over, because "
                    "the only runnable entity here is the one on the CPU",
                )

            candidates = []
            for se in rbtree_inorder_for_each_entry(
                "struct sched_entity", cfs.tasks_timeline.rb_root, "run_node"
            ):
                vruntime = se.vruntime.value_()
                deadline = se.deadline.value_()
                if sched_entity_is_task(se):
                    task = sched_entity_to_task(se)
                    who = f"{task.comm.string_().decode()}[{task.pid.value_()}]"
                    obj = task
                else:
                    who = "(group entity)"
                    obj = se
                eligible = vruntime <= average
                candidates.append((deadline, who, eligible, se, obj))
                yield Observation(
                    f"    {who}",
                    f"deadline={deadline}",
                    f"vruntime={vruntime} lag={average - vruntime} "
                    f"weight={se.load.weight.value_()} "
                    + ("eligible" if eligible else "NOT eligible (ahead of average)"),
                    obj,
                )

            if not candidates:
                yield Observation(
                    "    nothing to choose",
                    "tree empty",
                    f"nr_queued={queued} but the tree holds none of them: an "
                    "entity is dequeued while it runs, so with one runnable "
                    "entity there is no alternative to compare against",
                    kind="result",
                )
                continue

            eligible = [c for c in candidates if c[2]]
            if eligible:
                winner = min(eligible, key=lambda c: c[0])
                yield Observation(
                    "    would pick",
                    winner[1],
                    f"earliest deadline ({winner[0]}) among eligible entities",
                    winner[4],
                    kind="result",
                )
            else:
                yield Observation(
                    "    would pick",
                    "none eligible",
                    "every entity is ahead of avg_vruntime; the tree is re-based "
                    "as vruntime advances",
                    kind="result",
                )


EEVDF = Algorithm(
    key="eevdf_pick",
    label="which task runs next (EEVDF)",
    subsystem="sched",
    rule=(
        "Among entities that are eligible (vruntime <= avg_vruntime, i.e. they "
        "have not yet had more than their share), pick the one with the earliest "
        "virtual deadline."
    ),
    doc="Recomputes pick_next_entity from live runqueue state, per CPU.",
    analyse=_eevdf_pick,
)

ALGORITHMS: list[Algorithm] = [EEVDF]


def register_algorithm(algorithm: Algorithm) -> Algorithm:
    """Let other modules add analyses without this one importing them."""
    ALGORITHMS.append(algorithm)
    return algorithm


def algorithms() -> list[Algorithm]:
    """Every analysis, in registration order.

    Importing is the side effect, the same way ``subsystems()`` does it: an
    analysis defined elsewhere registers itself by being imported here, rather
    than by the frontend importing it for its side effects and having to say so
    with a noqa.
    """
    from . import clone_experiment  # noqa: F401

    return list(ALGORITHMS)
