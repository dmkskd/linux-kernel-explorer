"""System V IPC: message queues, semaphore arrays and shared memory.

Each of the three begins with a ``struct kern_ipc_perm``: the key a process
looks the object up by, the id the syscalls return, and the owner and mode
that decide who may use it. The lookup, the permission check and the removal
are shared code. What follows the perm differs: a list of messages, an array
of semaphore values, or a file holding pages.

An IPC object outlives the process that created it. A segment created by a
shell command is still present after the shell exits, which is what ipcs(1)
lists. The object belongs to an IPC namespace rather than to a process:
unshare the namespace and the same key names a different object.

The three ``ipc_ids`` in a namespace are IDR trees, indexed by the sequence
number encoded in each id. drgn walks them, so nothing here reimplements the
id-to-object lookup.
"""

from __future__ import annotations

from drgn import Object, Program
from drgn.helpers.linux.ipc import (
    for_each_sysv_msg_queue,
    for_each_sysv_sem_array,
    for_each_sysv_shm,
)
from drgn.helpers.linux.list import list_for_each_entry
from drgn.helpers.linux.pid import for_each_task

from ..core import ctypes as ct
from .format import task_comm
from .registry import Entry, Fact, FactEntry, Subsystem, register

# ipc/util.h: the three ipc_ids in a namespace, in the order they are indexed.
IPC_SEM_IDS = 0
IPC_MSG_IDS = 1
IPC_SHM_IDS = 2


def _key(perm: Object) -> str:
    """The key a process passes to msgget/semget/shmget.

    ipcs prints it in hex and so does this: a key is usually built by ftok()
    or written as a constant in a header, and neither reads as a decimal.
    ``0x00000000`` is IPC_PRIVATE -- no key, reachable only by id.
    """
    raw = ct.safe(lambda: int(perm.key), 0) & 0xFFFFFFFF
    return f"0x{raw:08x}"


def _identity(perm: Object) -> str:
    """id, key and mode, the part every IPC object has."""
    return (
        f"id {ct.safe(lambda: int(perm.id), -1):<6} key {_key(perm)} "
        f"uid {ct.safe(lambda: int(perm.uid.val), -1):<6} "
        f"{ct.safe(lambda: int(perm.mode), 0) & 0o7777:04o}"
    )


def _task_ipc_ns(task: Object) -> Object | None:
    """The IPC namespace a task is in, or None.

    A task on its way out has already dropped its nsproxy, and reading through
    the NULL pointer faults rather than returning zero -- so the pointer is
    read and tested before anything is taken from it.
    """
    nsproxy = ct.safe(lambda: task.nsproxy, None)
    if nsproxy is None or not ct.safe(lambda: nsproxy.value_(), 0):
        return None
    ns = ct.safe(lambda: nsproxy.ipc_ns, None)
    if ns is None or not ct.safe(lambda: ns.value_(), 0):
        return None
    return ns


def namespaces(prog: Program):
    """The IPC namespaces tasks are running in, with what each holds.

    There is no global list of them: an ipc_namespace is reachable only from a
    task's nsproxy, so this collects the distinct ones and counts the tasks in
    each. The initial namespace is always here, holding whatever was created
    without unsharing.
    """
    found: dict[int, tuple[Object, int]] = {}
    for task in for_each_task(prog):
        ns = _task_ipc_ns(task)
        if ns is None:
            continue
        inum = ct.safe(lambda: int(ns.ns.inum), 0)
        namespace, tasks = found.get(inum, (ns, 0))
        found[inum] = (namespace, tasks + 1)
    for inum, (ns, tasks) in sorted(found.items()):
        counts = " ".join(
            f"{ct.safe(lambda: int(ns.ids[index].in_use), 0)} {name}"
            for index, name in (
                (IPC_SEM_IDS, "sem"), (IPC_MSG_IDS, "msg"), (IPC_SHM_IDS, "shm")
            )
        )
        yield f"ipc ns {inum}  {counts}, {tasks} task(s)", ns


def _each_namespace(prog: Program):
    """Every distinct ipc_namespace, so a listing covers containers too."""
    seen: set[int] = set()
    for task in for_each_task(prog):
        ns = _task_ipc_ns(task)
        if ns is None:
            continue
        address = ns.value_()
        if address not in seen:
            seen.add(address)
            yield ns


def message_queues(prog: Program):
    """struct msg_queue: what msgget(2) returns an id for.

    ``q_cbytes`` against ``q_qbytes`` is what blocks a sender. The limit is in
    bytes; the message count is not capped separately.
    """
    for ns in _each_namespace(prog):
        for queue in for_each_sysv_msg_queue(ns):
            perm = queue.q_perm
            yield (
                f"msq {_identity(perm)}  "
                f"{ct.safe(lambda: int(queue.q_qnum), 0)} message(s), "
                f"{ct.safe(lambda: int(queue.q_cbytes), 0)}/"
                f"{ct.safe(lambda: int(queue.q_qbytes), 0)} bytes",
                queue,
            )


def semaphore_arrays(prog: Program):
    """struct sem_array: semget(2) allocates a whole array, not one semaphore.

    An operation names a member by index, and semop(2) applies a set of them
    atomically. The array is the object the id refers to, and a single
    semaphore has no id of its own.
    """
    for ns in _each_namespace(prog):
        for array in for_each_sysv_sem_array(ns):
            perm = array.sem_perm
            count = ct.safe(lambda: int(array.sem_nsems), 0)
            values = ",".join(
                str(ct.safe(lambda: int(array.sems[index].semval), -1))
                for index in range(min(count, 8))
            )
            more = ",…" if count > 8 else ""
            yield (
                f"sem {_identity(perm)}  {count} semaphore(s) [{values}{more}]",
                array,
            )


def shared_memory(prog: Program):
    """struct shmid_kernel: a segment, and the file holding its pages.

    shmget(2) creates a file in a hidden tmpfs and hands back an id. Attaching
    it with shmat(2) maps that file, which is why the pages are found through
    ``shm_file`` and appear in the page cache like any other tmpfs file.
    """
    for ns in _each_namespace(prog):
        for segment in for_each_sysv_shm(ns):
            perm = segment.shm_perm
            yield (
                f"shm {_identity(perm)}  "
                f"{ct.safe(lambda: int(segment.shm_segsz), 0)} bytes, "
                f"{ct.safe(lambda: int(segment.shm_nattch), 0)} attached",
                segment,
            )


def queued_messages(prog: Program):
    """struct msg_msg still on a queue: sent, and not yet received.

    A message longer than one page is split, and the rest hangs off ``next``
    as further segments; the size here is the whole message.
    """
    for ns in _each_namespace(prog):
        for queue in for_each_sysv_msg_queue(ns):
            identifier = ct.safe(lambda: int(queue.q_perm.id), -1)
            for message in list_for_each_entry(
                "struct msg_msg", queue.q_messages.address_of_(), "m_list"
            ):
                yield (
                    f"msq {identifier:<6} type {ct.safe(lambda: int(message.m_type), 0):<8} "
                    f"{ct.safe(lambda: int(message.m_ts), 0)} bytes",
                    message,
                )


def waiting_tasks(prog: Program):
    """Tasks blocked inside semop(2), one row per pending operation.

    A task waits when the operation it asked for cannot be applied yet:
    decrementing past zero, or waiting for a value to reach zero.

    Two sets of lists hold them. An operation touching one semaphore is queued
    on that semaphore's own ``pending_alter`` or ``pending_const``, which is
    what lets semop take a single spinlock. An operation spanning several
    semaphores is queued on the array's lists instead, under the global lock.
    Both are walked here, or the common case is missed.
    """
    for ns in _each_namespace(prog):
        for array in for_each_sysv_sem_array(ns):
            identifier = ct.safe(lambda: int(array.sem_perm.id), -1)
            heads = [("array", "alter", array.pending_alter),
                     ("array", "const", array.pending_const)]
            for index in range(ct.safe(lambda: int(array.sem_nsems), 0)):
                member = array.sems[index]
                heads.append((f"sems[{index}]", "alter", member.pending_alter))
                heads.append((f"sems[{index}]", "const", member.pending_const))
            for where, kind, head in heads:
                for queue in list_for_each_entry(
                    "struct sem_queue", head.address_of_(), "list"
                ):
                    task = ct.safe(lambda: queue.sleeper, None)
                    who = ct.safe(lambda: task_comm(task), "?") if task else "(gone)"
                    pid = ct.safe(lambda: int(task.pid), -1) if task else -1
                    yield (
                        f"sem {identifier:<6} {where:<8} {kind:<5} "
                        f"pid {pid:<7} {who}",
                        queue,
                    )


ULONG_MAX = (1 << 64) - 1
# include/uapi/linux/shm.h: the SHMMAX and SHMALL defaults are ULONG_MAX backed
# off by 16MiB, so that a sysctl "read the limit, add to it, write it back"
# cannot wrap. A kernel left at the default therefore imposes no limit, and the
# raw number states that only to a reader who recognises it.
SHM_UNLIMITED = ULONG_MAX - (1 << 24)


def _ceiling(raw: int, unit: str) -> str:
    """A limit, with its unit, and named when it is the no-limit default."""
    if raw == SHM_UNLIMITED:
        return f"{raw} {unit} (the SHMMAX/SHMALL default, ULONG_MAX - 16MiB: no limit)"
    if raw == ULONG_MAX:
        return f"{raw} {unit} (ULONG_MAX: no limit)"
    return f"{raw} {unit}"


def limits(prog: Program):
    """What this namespace allows, and how much of it is in use.

    Every one of these is writable through sysctl, and the kernel enforces the
    value it holds now, not the one in a config file. The initial namespace is
    what sysctl changes unless a process has unshared.
    """
    ns = prog["init_ipc_ns"]

    def value(expression, evidence: str, doc: str, label: str, unit: str = ""):
        raw = ct.safe(expression, None)
        if raw is None:
            text = "<fault>"
        elif unit:
            text = _ceiling(raw, unit)
        else:
            text = str(raw)
        return Fact(label, text, f"{evidence} -- {doc}")

    yield value(lambda: int(ns.sem_ctls[0]), "init_ipc_ns.sem_ctls[0]",
                "SEMMSL: semaphores per array", "max semaphores per array",
                "semaphores")
    yield value(lambda: int(ns.sem_ctls[1]), "init_ipc_ns.sem_ctls[1]",
                "SEMMNS: semaphores system-wide", "max semaphores total", "semaphores")
    yield value(lambda: int(ns.sem_ctls[2]), "init_ipc_ns.sem_ctls[2]",
                "SEMOPM: operations per semop(2) call", "max operations per semop",
                "operations")
    yield value(lambda: int(ns.sem_ctls[3]), "init_ipc_ns.sem_ctls[3]",
                "SEMMNI: arrays in this namespace, sysctl kernel.sem", "max semaphore arrays",
                "arrays")
    yield value(lambda: int(ns.used_sems), "init_ipc_ns.used_sems",
                "counted as arrays are created and removed", "semaphores in use",
                "semaphores")
    yield value(lambda: int(ns.msg_ctlmax), "init_ipc_ns.msg_ctlmax",
                "sysctl kernel.msgmax", "max bytes in one message", "bytes")
    yield value(lambda: int(ns.msg_ctlmnb), "init_ipc_ns.msg_ctlmnb",
                "sysctl kernel.msgmnb, the default q_qbytes of a new queue",
                "max bytes queued on one queue", "bytes")
    yield value(lambda: int(ns.msg_ctlmni), "init_ipc_ns.msg_ctlmni",
                "sysctl kernel.msgmni", "max message queues", "queues")
    yield value(lambda: int(ns.shm_ctlmax), "init_ipc_ns.shm_ctlmax",
                "sysctl kernel.shmmax", "max size of one segment", "bytes")
    yield value(lambda: int(ns.shm_ctlall), "init_ipc_ns.shm_ctlall",
                "sysctl kernel.shmall", "max shared memory total", "pages")
    yield value(lambda: int(ns.shm_ctlmni), "init_ipc_ns.shm_ctlmni",
                "sysctl kernel.shmmni", "max shared memory segments", "segments")
    yield value(lambda: int(ns.shm_tot), "init_ipc_ns.shm_tot",
                "summed as segments are created", "shared memory in use", "pages")
    yield value(lambda: int(ns.mq_queues_count), "init_ipc_ns.mq_queues_count",
                "POSIX queues, the mq_open(3) kind, not the System V ones above",
                "POSIX message queues")


register(
    Subsystem(
        key="ipc",
        label="ipc",
        doc="System V message queues, semaphore arrays and shared memory, and the namespaces holding them.",
        entries=[
            Entry(
                "namespaces",
                "IPC namespaces",
                "struct ipc_namespace, collected from the tasks running in them.",
                namespaces,
            ),
            Entry(
                "msg",
                "message queues",
                "struct msg_queue, what ipcs -q lists.",
                message_queues,
            ),
            Entry(
                "sem",
                "semaphore arrays",
                "struct sem_array, what ipcs -s lists.",
                semaphore_arrays,
            ),
            Entry(
                "shm",
                "shared memory segments",
                "struct shmid_kernel, what ipcs -m lists.",
                shared_memory,
            ),
            Entry(
                "messages",
                "messages queued",
                "struct msg_msg sent but not yet received.",
                queued_messages,
            ),
            Entry(
                "waiters",
                "tasks waiting on a semaphore",
                "struct sem_queue: one pending semop(2) operation and the task blocked in it.",
                waiting_tasks,
            ),
            FactEntry(
                "limits",
                "limits and usage",
                "The sysctl-tunable ceilings in init_ipc_ns, and what is in use.",
                limits,
            ),
        ],
    )
)
