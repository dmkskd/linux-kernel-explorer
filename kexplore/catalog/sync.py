"""What tasks are waiting for, and the grace periods that free memory.

Locks cannot be enumerated: a mutex is four words embedded in the structure
it protects, and the kernel keeps no registry of them. What can be enumerated
is the waiting, which is what identifies a stalled machine:

* a task blocked on a mutex records it in ``task->blocked_on``, and the mutex
  records its holder in ``mutex->owner``. Those two together are a wait chain:
  who is blocked, on what, and who is holding it.
* a task blocked in userspace on a contended lock is parked on a futex. Its
  ``futex_q`` is queued in a hash bucket: in the process's own table at
  ``mm->futex_phash`` for a private futex, and in the global table for a
  futex shared between processes.
* RCU (read-copy-update) has no waiters to list: a reader takes no lock,
  so nothing records it. The name is the writer's sequence: copy the object,
  change it, publish the new pointer, and free the old copy later. A writer that replaces a pointer therefore cannot free the old object
  at once, and waits for a grace period: the interval after which every CPU
  has passed through a quiescent state (a context switch, going idle, or
  returning to userspace), meaning no reader that started before the update
  is still running. What can be reported is how far the current grace period
  has advanced, and how much freeing is queued behind it.

The lock debugging that would make more of this visible is off in most builds.
Without ``CONFIG_LOCKDEP`` there is no ``held_locks`` on a task, so the locks a
task holds cannot be listed, only the one it is blocked on.
"""

from __future__ import annotations

from drgn import Object, Program, TypeKind
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.locking import mutex_owner
from drgn.helpers.linux.percpu import per_cpu
from drgn.helpers.linux.pid import for_each_task
from drgn.helpers.linux.plist import plist_for_each_entry

from ..core import ctypes as ct
from .compat import futex_support, has_member, mutex_wait_support, type_has_member
from .format import task_comm
from .registry import Entry, Fact, FactEntry, Subsystem, register

# Every list here is a list of waiting, so each says who is waiting first and
# what it is a structure of last, the way the task lists do.
WAITER_COLUMNS = ("pid", "command")
OBJECT_COLUMNS = ("type", "address")


def _task_cells(task: Object) -> tuple[str, str]:
    pid = ct.safe(lambda: int(task.pid), None)
    return (
        f"{pid:>7}" if pid is not None else "?",
        ct.safe(lambda: task_comm(task), "?"),
    )


def _task_label(task: Object) -> str:
    """A task in one cell, for a column that reports another task."""
    pid, comm = _task_cells(task)
    return f"{pid.strip()} {comm}"


def _object_cells(obj: Object) -> tuple[str, str]:
    return ct.type_name(obj.type_), f"{obj.value_():#x}"


def _hash_waiters(buckets: Object, count: int, table: str):
    """The futex waiters in one hash table.

    A bucket holds its waiters in a plist ordered by priority, so a real-time
    waiter is woken before a normal one on the same futex.
    """
    for index in range(count):
        bucket = buckets[index]
        for waiter in plist_for_each_entry(
            "struct futex_q", bucket.chain.address_of_(), "list"
        ):
            key = ct.safe(lambda: int(waiter.key.private.address), None)
            yield (
                (
                    *_task_cells(waiter.task),
                    f"{key:#x}" if key is not None else "?",
                    table,
                    *_object_cells(waiter),
                ),
                waiter,
            )


def futex_waiters(prog: Program):
    """Every task parked on a futex, using the detected hash layout.

    A process-private futex is hashed in that process's own table
    (``mm->futex_phash``); a futex shared between processes goes in the global
    table, which is indexed by NUMA node first and then by hash
    (kernel/futex/core.c). Both are walked here, so a waiter is listed once
    wherever it is queued. Kernels without private hashes use one global table
    for private and shared futexes, with its size recorded in hashsize.
    """
    seen: set[int] = set()
    tasks = for_each_task(prog) if has_member(prog, "struct mm_struct", "futex_phash") else ()
    for task in tasks:
        mm = ct.safe(lambda: task.mm, None)
        if mm is None or not ct.safe(lambda: mm.value_(), 0):
            continue
        if mm.value_() in seen:
            continue
        seen.add(mm.value_())
        private = ct.safe(lambda: mm.futex_phash, None)
        if private is None or not ct.safe(lambda: private.value_(), 0):
            continue
        count = ct.safe(lambda: int(private.hash_mask) + 1, 0)
        yield from _hash_waiters(private.queues, count, f"private, pid {int(task.tgid)}")

    data = prog["__futex_data"]
    if type_has_member(data.type_, "hashsize"):
        yield from _hash_waiters(data.queues, int(data.hashsize), "global (private and shared)")
        return
    mask = int(data.hashmask)
    if data.queues.type_.kind != TypeKind.ARRAY:
        yield from _hash_waiters(data.queues, mask + 1, "global (private and shared)")
        return
    for node in range(int(prog["nr_node_ids"])):
        table = data.queues[node]
        if not table.value_():
            continue
        yield from _hash_waiters(table, mask + 1, f"global, node {node}")


def mutex_blocked(prog: Program):
    """Tasks blocked on a kernel mutex, and which task holds it.

    ``task->blocked_on`` is set while a task sleeps in ``mutex_lock``. Reading
    the holder from the mutex turns one row into the start of a wait chain:
    the blocked task, the mutex, and the task to look at next.
    """
    for task in for_each_task(prog):
        mutex = ct.safe(lambda: task.blocked_on, None)
        if mutex is None or not ct.safe(lambda: mutex.value_(), 0):
            continue
        owner = ct.safe(lambda: mutex_owner(mutex), None)
        held = (
            _task_label(owner)
            if owner is not None and ct.safe(lambda: owner.value_(), 0)
            else "none recorded"
        )
        yield (
            (*_task_cells(task), f"{mutex.value_():#x}", held, *_object_cells(task)),
            task,
        )


def rt_mutex_blocked(prog: Program):
    """Tasks blocked on an rt_mutex, which is where priority inheritance runs.

    ``pi_blocked_on`` is the waiter a task is queued as. Outside PREEMPT_RT
    this is mostly futex PI, so an empty list is the normal state.
    """
    for task in for_each_task(prog):
        waiter = ct.safe(lambda: task.pi_blocked_on, None)
        if waiter is None or not ct.safe(lambda: waiter.value_(), 0):
            continue
        lock = ct.safe(lambda: f"{waiter.lock.value_():#x}", "?")
        yield (
            (*_task_cells(task), lock, *_object_cells(waiter)),
            waiter,
        )


def _gp_state_name(prog: Program, state: int) -> str:
    """The kernel's own name for a grace-period state.

    ``gp_state_names`` is an array of strings in the kernel, so the names come
    from the build being inspected rather than from a table here that a
    renumbering upstream would quietly invalidate.
    """
    names = ct.safe(lambda: prog["gp_state_names"], None)
    if names is None:
        return str(state)
    if not 0 <= state < len(names):
        return f"{state} (outside gp_state_names)"
    return ct.safe(lambda: names[state].string_().decode("utf-8", "replace"), str(state))


def rcu_grace_period(prog: Program):
    """Grace-period sequence, state, kthread and expedited counters.

    ``gp_seq`` counts grace periods in its upper bits and the phase in its
    lower two, so it advances by four per grace period. ``gp_state`` is the
    grace-period kthread's position in its own loop, named by the kernel's
    ``gp_state_names``.
    """
    state = prog["rcu_state"]
    flavour = ct.safe(lambda: state.name.string_().decode("utf-8", "replace"), "?")
    gp_seq = int(state.gp_seq)
    # include/linux/rcupdate.h: RCU_SEQ_CTR_SHIFT is 2 and RCU_SEQ_STATE_MASK
    # is (1 << it) - 1, so the count is gp_seq >> 2 and the phase the low two
    # bits. These are macros, so there is nothing in the debug info to read
    # them from; they are checked against that header, not assumed.
    yield Fact("flavour", flavour, "rcu_state.name")
    # The bottom two bits of gp_seq are the phase; the rest is the count, which
    # is why the number jumps by four per grace period rather than by one.
    yield Fact(
        "grace periods completed",
        f"{gp_seq >> 2} (gp_seq {gp_seq}, phase {gp_seq & 0x3})",
        "rcu_state.gp_seq",
    )
    yield Fact(
        "grace-period state",
        _gp_state_name(prog, int(state.gp_state)),
        "rcu_state.gp_state, named by gp_state_names",
    )
    kthread = ct.safe(lambda: state.gp_kthread, None)
    yield Fact(
        "grace-period kthread",
        _task_label(kthread) if kthread and kthread.value_() else "none",
        "rcu_state.gp_kthread",
    )
    yield Fact(
        "CPUs",
        f"{int(state.n_online_cpus)} online of {int(state.ncpus)} seen",
        "rcu_state.n_online_cpus / ncpus",
    )
    yield Fact(
        "expedited grace periods",
        str(int(state.expedited_sequence) >> 2),
        "rcu_state.expedited_sequence",
    )
    yield Fact(
        "force-quiescent-state scans",
        str(int(state.n_force_qs)),
        "rcu_state.n_force_qs",
    )


def rcu_cpus(prog: Program):
    """Per-CPU RCU state: how far behind this CPU is, and what it owes.

    A callback queued here runs when the grace period it was registered in
    completes, so the queue length is the freeing this CPU has deferred.
    """
    for cpu in for_each_online_cpu(prog):
        data = per_cpu(prog["rcu_data"], cpu)
        callbacks = ct.safe(lambda: int(data.cblist.len.counter), None)
        queued = "?" if callbacks is None else str(callbacks)
        needs_qs = "needs quiescent state" if data.core_needs_qs.value_() else "quiet"
        yield (
            f"cpu {cpu}  gp_seq {int(data.gp_seq):<10} {queued:>6} callbacks  {needs_qs}",
            data.address_of_(),
        )


def rcu_nodes(prog: Program):
    """The rcu_node tree that combines per-CPU quiescent states.

    The tree exists so that CPUs report into per-node masks instead of one
    global lock. A small machine has a single node, and the tree is then one
    level deep.
    """
    state = prog["rcu_state"]
    levels = state.level
    for level in range(len(levels)):
        first = levels[level]
        if not first.value_():
            continue
        end = None
        for later in range(level + 1, len(levels)):
            if levels[later].value_():
                end = levels[later]
                break
        node = first
        index = 0
        while True:
            if end is not None and node.value_() >= end.value_():
                break
            yield (
                f"level {level} node {index}  qsmask {int(node.qsmask):#x}  "
                f"gp_seq {int(node.gp_seq)}",
                node,
            )
            index += 1
            if end is None:
                break
            node = node + 1


register(
    Subsystem(
        key="sync",
        label="sync",
        doc="What tasks are waiting for: mutex holders, futex waiters, and the RCU grace period that defers freeing.",
        entries=[
            Entry(
                "mutex_blocked",
                "tasks blocked on a mutex",
                "task->blocked_on, with the mutex's recorded owner.",
                mutex_blocked,
                capability=mutex_wait_support,
                columns=(*WAITER_COLUMNS, "mutex", "held by", *OBJECT_COLUMNS),
            ),
            Entry(
                "futex_waiters",
                "futex waiters",
                "struct futex_q in the available global and optional process-private hash tables.",
                futex_waiters,
                capability=futex_support,
                columns=(*WAITER_COLUMNS, "futex word (user address)", "hash",
                         *OBJECT_COLUMNS),
            ),
            Entry(
                "rt_blocked",
                "tasks blocked on an rt_mutex",
                "task->pi_blocked_on, where priority inheritance applies.",
                rt_mutex_blocked,
                columns=(*WAITER_COLUMNS, "rt_mutex", *OBJECT_COLUMNS),
            ),
            FactEntry(
                "rcu",
                "RCU (read-copy-update) grace period",
                "A grace period ends once every CPU has passed a quiescent "
                "state, which is when RCU may free what a writer replaced. "
                "This is how far the current one has advanced.",
                rcu_grace_period,
            ),
            Entry(
                "rcu_cpus",
                "RCU state per CPU",
                "struct rcu_data: the grace period this CPU has reached, and the "
                "frees it has queued until that period ends.",
                rcu_cpus,
            ),
            Entry(
                "rcu_nodes",
                "RCU node tree",
                "struct rcu_node: the masks CPUs clear as they pass a quiescent "
                "state, combined upwards to end the grace period.",
                rcu_nodes,
            ),
        ],
    )
)
