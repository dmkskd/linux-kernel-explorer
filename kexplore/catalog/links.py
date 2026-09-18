"""Curated relationships between structures.

Field navigation shows what a struct contains. It does not show the edges that
are not fields: a task's threads come from walking ``signal->thread_head``,
its runqueue from ``task_rq()``.

A Link names one edge out of a struct and how to traverse it, selected by hand
from the hundreds a struct like ``task_struct`` exposes. It also carries the
two things worth knowing about an edge besides where it goes: what it really is
(``origin`` -- a member read, a list walk, a helper call) and how you would get
the same information from userspace. Both live on the Link rather than in a
side table keyed by its label, so renaming a label cannot silently drop them.
"""

from __future__ import annotations

import functools
import time as _time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from drgn import Object, TypeKind, cast, container_of
from drgn.helpers.linux.block import (
    blk_rq_bytes,
    blk_rq_pos,
    part_name,
    request_queue_busy_iter,
)
from drgn.helpers.linux.cpumask import for_each_online_cpu
from drgn.helpers.linux.fs import mount_dst
from drgn.helpers.linux.idr import idr_for_each
from drgn.helpers.linux.irq import irq_desc_kstat_cpu, irq_to_desc
from drgn.helpers.linux.list import hlist_for_each_entry, list_for_each_entry
from drgn.helpers.linux.locking import (
    RwsemLocked,
    mutex_owner,
    rwsem_locked,
    rwsem_owner,
)
from drgn.helpers.linux.mm import (
    compound_head,
    decode_page_flags,
    follow_page,
    for_each_vma,
    page_size,
    page_to_pfn,
    page_to_phys,
    vma_name,
)
from drgn.helpers.linux.mmzone import for_each_online_pgdat
from drgn.helpers.linux.net import SOCKET_I, netdev_name, skb_shinfo
from drgn.helpers.linux.ipc import (
    for_each_sysv_msg_queue,
    for_each_sysv_sem_array,
    for_each_sysv_shm,
)
from drgn.helpers.linux.pid import for_each_task_in_group, pid_task
from drgn.helpers.linux.rbtree import rbtree_inorder_for_each_entry
from drgn.helpers.linux.sched import task_rq, task_state_to_char
from drgn.helpers.linux.slab import (
    slab_cache_for_each_allocated_object,
    slab_cache_is_merged,
    slab_cache_objects_per_slab,
    slab_cache_order,
)
from drgn.helpers.linux.xarray import xa_for_each

from ..core import ctypes as ct
from .decoders import (
    CLOCK_EVENT_STATES,
    CLOCK_NAMES,
    DELAYED_WORK_TIMER,
    HRTIMER_WAKEUP,
    WQ_FLAG_NAMES,
    request_op_name,
)
from .format import as_text, task_comm
from .walk import files_of, path_of

# A resolver returns one object or an iterable of labelled objects.
Resolver = Callable[[Object], "Object | Iterator[tuple[str, Object]]"]

# include/linux/page-flags.h: page->mapping is a tagged pointer. Bit 0 says the
# rest of it is an anon_vma rather than the file's address_space.
# The clocks whose ids clock_gettime also accepts.
CLOCK_GETTIME_IDS = frozenset(CLOCK_NAMES)

PAGE_MAPPING_ANON = 0x1
PAGE_MAPPING_FLAGS = 0x3

IPPROTO_TCP = 6
IPPROTO_UDP = 17
AF_UNIX = 1


@dataclass(frozen=True)
class Derived:
    """A computed value shown alongside the real fields.

    Values not stored in the struct: a page's pfn and decoded flags, an skb's
    headroom, a socket's address family. Rendered as read-only rows so they do
    not require a helper call in the REPL.
    """

    label: str
    doc: str
    compute: Callable[[Object], object]


def _name_storage(cache: Object) -> str:
    """Where a slab cache's name string physically lives.

    ``kmem_cache_create()`` calls ``kstrdup_const()``, which avoids the copy
    only when the string is in the *core kernel's* rodata. A module's string
    literal isn't, so module caches get a heap copy while built-in caches point
    straight at rodata.
    """
    address = cache.name.value_()
    try:
        symbol = cache.prog_.symbol(address)
        return f"kernel image rodata ({symbol.name}+{address - symbol.address:#x})"
    except LookupError:
        return "heap (kstrdup copy, module-created cache)"


@functools.lru_cache(maxsize=256)
def _user_name(uid: int) -> str | None:
    """Resolve a uid to a name via the local passwd database.

    The kernel stores only numeric ids -- names live in /etc/passwd, which is
    userspace. This is therefore only meaningful when inspecting the live local
    kernel; against a core dump from another machine the names would be this
    machine's. Callers label it accordingly.
    """
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError, OverflowError):
        return None


def _id_label(value: int) -> str:
    name = _user_name(value)
    return f"{value} ({name})" if name else str(value)


def _task_user(task: Object) -> str:
    """Real uid, plus the effective uid when it differs."""
    cred = task.cred
    uid = cred.uid.val.value_()
    euid = cred.euid.val.value_()
    text = _id_label(uid)
    if euid != uid:
        text += f", running as euid {_id_label(euid)}"
    return text


def _task_role(task: Object) -> str:
    """This task's role in its thread group.

    Every task is a thread, the leader included -- "process" names the group
    they share, never one of these structs. pid is the thread id, tgid the id
    of the group, which is the leader's pid.
    """
    pid = task.pid.value_()
    tgid = task.tgid.value_()
    threads = ct.safe(lambda: task.signal.nr_threads.value_(), 1)
    if pid == tgid:
        if threads > 1:
            return f"leader of a group of {threads} threads (pid == tgid)"
        return "the only thread in its group (pid == tgid)"
    return f"thread in the group led by {tgid} (pid != tgid)"


def _task_group(task: Object) -> str:
    cred = task.cred
    gid = cred.gid.val.value_()
    egid = cred.egid.val.value_()
    text = str(gid)
    if egid != gid:
        text += f", egid {egid}"
    return text


def _user_ns_label(task: Object) -> str:
    """Whether these ids are in the initial user namespace."""
    cred = task.cred
    ns = cred.user_ns
    init_ns = task.prog_["init_user_ns"].address_of_()
    if ns.value_() == init_ns.value_():
        return "init_user_ns"
    return f"{ns.value_():#x} (not init: ids differ inside the namespace)"


def _skb_device(skb: Object) -> str:
    """The net_device an skb belongs to, if that field still holds one.

    ``skb->dev`` shares storage with ``dev_scratch``: the kernel-doc says
    "alternate use of @dev when @dev would be %NULL". An skb sitting on a
    socket receive queue has no device, so the UDP path stores scratch data
    there and reading it as a pointer faults.
    """
    address = ct.safe(lambda: skb.dev.value_(), 0)
    if not address:
        return "none"
    name = ct.safe(lambda: as_text(netdev_name(skb.dev)), None)
    if name is None:
        return f"{address:#x}, not a device (dev_scratch in use)"
    return name


def _is_socket_file(file: Object) -> bool:
    """A struct file is a socket iff its f_op is socket_file_ops."""
    return file.f_op == file.prog_["socket_file_ops"].address_of_()



# ------------------------------------------------------------------ irq

# include/linux/sched.h. PF_WQ_WORKER marks a kworker thread, which is what
# makes the walk back from a task to its worker_pool worth attempting.
PF_WQ_WORKER = 0x00000020
# enum worker_pool_flags. A BH pool's work runs in softirq context, not in a
# kthread: kernel/softirq.c calls workqueue_softirq_action(), which runs
# bh_worker() on the first worker of the per-CPU bh_worker_pools. That worker
# exists on ``pool->workers`` like any other, but its ``task`` is NULL, which
# is what separates it from a pool backed by kworker threads.
POOL_BH = 0x1


def _irq_actions(desc: Object):
    """The handlers bound to this line, in the order the kernel calls them.

    A shared line has more than one, chained through ``action->next``; each
    handler decides whether the interrupt was its device's.
    """
    action = desc.action
    while action:
        name = ct.safe(lambda: action.name.string_().decode("utf-8", "replace"), "?")
        handler = ct.safe(lambda: desc.prog_.symbol(action.handler).name, "?")
        yield f"{name}  {handler}", action
        action = action.next


def _irq_count(desc: Object) -> str:
    """Times this line has fired, summed over the online CPUs.

    ``desc->tot_count`` only counts lines the kernel marked as non-per-CPU; an
    IPI reads zero there while its per-CPU counters are in the millions. The
    per-CPU array is what /proc/interrupts prints, so use that.
    """
    prog = desc.prog_
    total = sum(int(irq_desc_kstat_cpu(desc, cpu)) for cpu in for_each_online_cpu(prog))
    return f"{total} (summed over online CPUs)"


def _irq_hwirq(desc: Object) -> str:
    """The controller's own number for this line, next to the Linux one."""
    return f"{int(desc.irq_data.hwirq)} (Linux irq {int(desc.irq_data.irq)})"


def _action_desc(action: Object):
    """Back to the descriptor, found by the irq number the action records."""
    return irq_to_desc(action.prog_, int(action.irq))


def _pool_workers(pool: Object):
    """The workers of this pool, via ``pool->workers``.

    A worker normally runs as a kthread, and the row names it. A BH pool's
    single worker has no task at all: its work runs in softirq context, on
    whatever the CPU interrupted. Reading a name from that NULL task is what
    this used to do, and it faulted.
    """
    for worker in list_for_each_entry(
        "struct worker", pool.workers.address_of_(), "node"
    ):
        task = ct.safe(lambda: worker.task, None)
        busy = ct.safe(lambda: int(worker.current_work), 0)
        state = "running work" if busy else "idle"
        if task is None or not ct.safe(lambda: task.value_(), 0):
            yield f"no task (runs in softirq)  {state}", worker
            continue
        yield f"{ct.safe(lambda: task_comm(task), '?')}  {state}", worker


def _pool_worklist(pool: Object):
    """Work queued on this pool and not yet picked up by a worker."""
    for work in list_for_each_entry(
        "struct work_struct", pool.worklist.address_of_(), "entry"
    ):
        func = ct.safe(lambda: work.prog_.symbol(work.func).name, "?")
        yield func, work


def _pool_context(pool: Object) -> str:
    """Where this pool's work actually runs."""
    if int(pool.flags) & POOL_BH:
        return "softirq (BH pool: its worker has no task, so it cannot sleep)"
    cpu = int(pool.cpu)
    where = f"bound to cpu {cpu}" if cpu >= 0 else "unbound (any CPU)"
    return f"worker threads, {where}, nice {int(pool.attrs.nice)}"


def _wq_pwqs(wq: Object):
    """The per-CPU (or per-node) halves of this workqueue.

    A workqueue is one name; the queueing actually happens on a
    ``pool_workqueue`` per CPU, each attached to the worker pool that will run
    the work.
    """
    for pwq in list_for_each_entry(
        "struct pool_workqueue", wq.pwqs.address_of_(), "pwqs_node"
    ):
        pool = pwq.pool
        cpu = int(pool.cpu)
        where = f"cpu {cpu}" if cpu >= 0 else "unbound"
        yield f"pool {int(pool.id)} ({where})  {int(pwq.nr_active)} active", pwq


def _pwq_inactive(pwq: Object):
    """Work held back because the queue is at ``max_active``."""
    for work in list_for_each_entry(
        "struct work_struct", pwq.inactive_works.address_of_(), "entry"
    ):
        func = ct.safe(lambda: work.prog_.symbol(work.func).name, "?")
        yield func, work


def _wq_flags(wq: Object) -> str:
    flags = int(wq.flags)
    spelled = ",".join(text for bit, text in WQ_FLAG_NAMES if flags & bit)
    return f"{flags:#x} ({spelled or 'none of the public flags'})"


def _task_worker(task: Object):
    """From a kworker task back to the ``struct worker`` the pools know it by.

    There is no pointer for this: ``task_struct`` has no worker field, so the
    only way is to search the pools. There are a couple of dozen, so this is a
    short search, not a scan of the whole kernel.
    """
    prog = task.prog_
    for _, address in idr_for_each(prog["worker_pool_idr"].address_of_()):
        pool = Object(prog, "struct worker_pool *", address)
        # A BH pool's worker has no task, so it can never be the one searched
        # for, and skipping it saves reading the list at all.
        if int(pool.flags) & POOL_BH:
            continue
        for worker in list_for_each_entry(
            "struct worker", pool.workers.address_of_(), "node"
        ):
            if worker.task.value_() == task.value_():
                return worker
    return None


def _work_func(work: Object) -> str:
    return ct.safe(lambda: work.prog_.symbol(work.func).name, "?")


def _worker_current(worker: Object) -> str:
    """What this worker is running, or what it ran last.

    ``current_func`` is only meaningful while an item is in flight;
    ``last_func`` keeps the previous one, which is what an idle kworker has.
    """
    prog = worker.prog_
    if ct.safe(lambda: int(worker.current_work), 0):
        return ct.safe(lambda: f"running {prog.symbol(worker.current_func).name}", "?")
    last = ct.safe(lambda: prog.symbol(worker.last_func).name, None)
    return f"idle, last ran {last}" if last else "idle, nothing run yet"


def _pci_irq_desc(dev: Object):
    """The interrupt line this PCI device's driver requested."""
    return irq_to_desc(dev.prog_, int(dev.irq))


# ----------------------------------------------------------------- timers

def _symbol_name(obj: Object, func) -> str:
    return ct.safe(lambda: obj.prog_.symbol(func).name, "?")


def _timer_func(fn):
    """Predicate: this timer's callback is ``fn``."""

    def check(timer: Object) -> bool:
        return _symbol_name(timer, timer.function) == fn

    return check


def _hrtimer_clock(timer: Object) -> int:
    return int(timer.base.clockid)


def _hrtimer_expires_in(timer: Object) -> str:
    """How far off this timer is, against the clock it is queued on.

    The clockid an hrtimer base carries is the number userspace passes to
    ``clock_gettime``, so the two are directly comparable -- but only when the
    kernel being inspected is this machine's. Against a core dump the answer
    would be measured from the wrong clock, so it is labelled rather than
    presented as the kernel's own view.
    """
    clockid = _hrtimer_clock(timer)
    if clockid not in CLOCK_GETTIME_IDS:
        return f"{int(timer.node.expires)} ns (clockid {clockid}, not readable here)"
    now = _time.clock_gettime(clockid)
    delta_ms = (int(timer.node.expires) / 1e9 - now) * 1000
    when = f"{delta_ms:.1f} ms" if delta_ms >= 0 else f"{-delta_ms:.1f} ms ago (due)"
    return f"{when}, measured against this machine's CLOCK_{CLOCK_NAMES[clockid]}"


def _hrtimer_mode(timer: Object) -> str:
    """Which context this one fires in, and whether it was set relative."""
    where = "softirq (TIMER)" if int(timer.is_soft) else "hardirq"
    return f"{where}, {'relative' if int(timer.is_rel) else 'absolute'} when set"


def _hrtimer_sleeper_task(timer: Object):
    """The task an ``hrtimer_wakeup`` timer will wake.

    ``hrtimer_sleeper`` embeds the timer and adds the task, so the task is one
    container_of away -- but only for timers whose callback is that function.
    """
    return container_of(timer, "struct hrtimer_sleeper", "timer").task


def _clock_base_timers(base: Object):
    for node in rbtree_inorder_for_each_entry(
        "struct timerqueue_node", base.active.rb_root.rb_root.address_of_(), "node"
    ):
        timer = container_of(node, "struct hrtimer", "node")
        yield _symbol_name(timer, timer.function), timer


def _cpu_base_clock_bases(cpu_base: Object):
    for index in range(len(cpu_base.clock_base)):
        base = cpu_base.clock_base[index]
        clock = CLOCK_NAMES.get(int(base.clockid), str(int(base.clockid)))
        half = "soft" if int(base.index) >= 4 else "hard"
        yield f"{clock} ({half})", base.address_of_()


def _wheel_base_timers(base: Object):
    for vector in range(len(base.vectors)):
        for timer in hlist_for_each_entry(
            "struct timer_list", base.vectors[vector].address_of_(), "entry"
        ):
            yield _symbol_name(timer, timer.function), timer


def _jiffy_ns(prog) -> int:
    """Nanoseconds in a jiffy, read from the jiffies clocksource.

    ``HZ`` is a compile-time constant and not a symbol, so it cannot be read
    from the kernel directly. The jiffies clocksource carries the same number
    as a mult/shift pair, which is where this comes from rather than a guess.
    """
    for source in list_for_each_entry(
        "struct clocksource", prog["clocksource_list"].address_of_(), "list"
    ):
        if source.name.string_() == b"jiffies":
            return int(source.mult) >> int(source.shift)
    return 0


def _timer_expires_in(timer: Object) -> str:
    prog = timer.prog_
    delta = int(timer.expires) - int(prog["jiffies"])
    per_jiffy = _jiffy_ns(prog)
    if not per_jiffy:
        return f"{delta} jiffies"
    hz = 1_000_000_000 // per_jiffy
    ms = delta * per_jiffy / 1e6
    when = f"{ms:.1f} ms" if delta >= 0 else f"{-ms:.1f} ms ago (due)"
    return f"{delta} jiffies = {when} (HZ={hz})"


def _delayed_work(timer: Object):
    return container_of(timer, "struct delayed_work", "timer").work.address_of_()


def _delayed_work_queue(timer: Object):
    return container_of(timer, "struct delayed_work", "timer").wq


def _clocksource_resolution(source: Object) -> str:
    """Nanoseconds per counter tick, from the mult/shift the kernel uses."""
    mult, shift = int(source.mult), int(source.shift)
    if not mult:
        return "no conversion set"
    ns = mult / (1 << shift)
    return f"{ns:.3f} ns per cycle (mult {mult} >> shift {shift})"


def _clock_event_state(device: Object) -> str:
    return CLOCK_EVENT_STATES.get(
        int(device.state_use_accessors), str(int(device.state_use_accessors))
    )


def _clock_event_handler(device: Object) -> str:
    """The function this device's interrupt calls, once something claims it.

    A detached device has none: nothing is armed on it, so there is no handler
    to name rather than an unresolvable one.
    """
    if not ct.safe(lambda: device.event_handler.value_(), 0):
        return "none (device not in use)"
    return _symbol_name(device, device.event_handler)



# --------------------------------------------------------- locks and waiting


def _mutex_state(lock: Object) -> str:
    """Whether this mutex is held, and by whom.

    ``mutex->owner`` packs flags into the low bits of the task pointer, so it
    is read through drgn's helper rather than cast directly.
    """
    owner = ct.safe(lambda: mutex_owner(lock), None)
    if owner is None or not ct.safe(lambda: owner.value_(), 0):
        return "unlocked"
    return f"held by {int(owner.pid)} {task_comm(owner)}"


def _rwsem_locked(sem: Object) -> bool:
    return ct.safe(lambda: rwsem_locked(sem) != RwsemLocked.UNLOCKED, False)


def _rwsem_state(sem: Object) -> str:
    """Unlocked, read-locked or write-locked, and who holds it if anyone.

    A read-locked rwsem has no single owner to report: the count says how many
    readers hold it, not which tasks they are.

    An unlocked one can still name a task. A writer clears ``sem->owner`` when
    it releases, but reader ownership is only cleared in a build with
    CONFIG_DEBUG_RWSEMS or CONFIG_DETECT_HUNG_TASK_BLOCKER
    (kernel/locking/rwsem.c), so the field is usually the last reader rather
    than a current holder. Reporting that as an owner would be wrong.
    """
    state = ct.safe(lambda: str(rwsem_locked(sem)).rsplit(".", 1)[-1].lower(), "?")
    owner = ct.safe(lambda: rwsem_owner(sem), None)
    named = owner is not None and ct.safe(lambda: owner.value_(), 0)
    if not named:
        return state
    who = f"{int(owner.pid)} {task_comm(owner)}"
    if _rwsem_locked(sem):
        return f"{state}, owner {who}"
    return f"{state} (owner field still names {who}, the last reader)"


def _mutex_waiters(lock: Object):
    """The tasks queued on this mutex, first to be handed it first.

    This kernel keeps the queue as ``mutex->first_waiter`` and a list running
    through each waiter, so the walk starts at the first waiter rather than at
    a list head in the mutex.
    """
    first = lock.first_waiter
    if not first.value_():
        return
    yield f"{int(first.task.pid)} {task_comm(first.task)} (first)", first
    for waiter in list_for_each_entry(
        "struct mutex_waiter", first.list.address_of_(), "list"
    ):
        if waiter.value_() == first.value_():
            break
        yield f"{int(waiter.task.pid)} {task_comm(waiter.task)}", waiter


def _futex_key(waiter: Object) -> str:
    """The address the futex is keyed on, as userspace sees it."""
    address = ct.safe(lambda: int(waiter.key.private.address), None)
    return f"{address:#x} (userspace address)" if address is not None else "?"


def _rcu_callbacks(data: Object) -> str:
    count = ct.safe(lambda: int(data.cblist.len.counter), None)
    return "?" if count is None else f"{count} waiting for a grace period"


@dataclass(frozen=True)
class Link:
    label: str
    doc: str
    resolve: Resolver
    # Some edges only exist for certain instances of a type -- a struct file
    # is only a socket if its f_op says so. Links whose predicate fails are
    # hidden rather than shown as dead ends.
    applies: Callable[[Object], bool] | None = None
    # What this edge actually is: a plain member read, or a walk or helper call
    # that has no corresponding field on the struct. A link is not an alias for
    # a field, and this is where the difference is stated.
    origin: str = ""
    # The same information as seen from userspace, so the mental model carries
    # to a production box where none of this is installed. "<pid>" is
    # substituted with the pid of the object being viewed. Left empty when
    # there is genuinely no equivalent, which is worth saying: it marks what
    # only a debugger can reach.
    userspace: str = ""

    def visible(self, obj: Object) -> bool:
        if self.applies is None:
            return True
        try:
            return bool(self.applies(obj))
        except Exception:  # noqa: BLE001 - an unreadable predicate just hides it
            return False


def task_label(task: Object) -> str:
    """pid, state character and comm, for list rows."""
    return f"{task.pid.value_():>7} {task_state_to_char(task)}  {task_comm(task)}"


# --------------------------------------------------------------- task_struct


def _thread_label(thread: Object) -> str:
    """A thread row. Only the leader is marked: the others are threads too."""
    marker = " [leader]" if thread.pid == thread.tgid else ""
    return f"{thread.pid.value_():>7}{marker}  {task_comm(thread)}"


def _threads(task: Object):
    for thread in for_each_task_in_group(task, include_self=True):
        yield _thread_label(thread), thread


def _signal_threads(signal: Object):
    """The same walk as ``task_struct/threads``, done one hop at a time.

    thread_head is a bare list_head, so landing on a signal_struct otherwise
    dead-ends: the list is there but nothing in the UI can step along it.
    """
    for thread in list_for_each_entry(
        "struct task_struct", signal.thread_head.address_of_(), "thread_node"
    ):
        yield _thread_label(thread), thread


def _children(task: Object):
    for child in list_for_each_entry(
        "struct task_struct", task.children.address_of_(), "sibling"
    ):
        yield task_label(child), child


def _open_files(task: Object):
    for fd, file in files_of(task):
        yield f"fd {fd:>3}  {path_of(file)}", file


def _vmas(task: Object):
    for vma in for_each_vma(task.mm):
        name = vma_name(vma)
        label = name.decode("utf-8", "replace") if name else "anon"
        yield f"{vma.vm_start.value_():#x}  {label}", vma


# vm_flags bits that decide what a mapping is allowed to do. The kernel has no
# "text segment" field: a mapping is the code because VM_EXEC is set on it, and
# one file usually contributes three VMAs (r-xp, r--p, rw-p) that are otherwise
# indistinguishable by name.
#
# The last character comes from VM_MAYSHARE, not VM_SHARED: show_map_vma prints
# `flags & VM_MAYSHARE ? 's' : 'p'` (fs/proc/task_mmu.c). The two differ -- on
# this kernel /sys/fs/selinux/status is mapped VM_MAYSHARE without VM_SHARED --
# so reading VM_SHARED here prints a string the kernel never would.
VM_READ, VM_WRITE, VM_EXEC, VM_MAYSHARE = 0x1, 0x2, 0x4, 0x80


def vma_perms(vma: Object) -> str:
    """The rwxp string /proc/<pid>/maps prints, read from vm_flags."""
    flags = vma.vm_flags.value_()
    return (
        ("r" if flags & VM_READ else "-")
        + ("w" if flags & VM_WRITE else "-")
        + ("x" if flags & VM_EXEC else "-")
        + ("s" if flags & VM_MAYSHARE else "p")
    )


def _mm_vmas(mm: Object):
    for vma in for_each_vma(mm):
        name = vma_name(vma)
        label = name.decode("utf-8", "replace") if name else "anon"
        yield f"{vma.vm_start.value_():#x}  {vma_perms(vma)}  {label}", vma


def _sockets(task: Object):
    for fd, file in files_of(task):
        if _is_socket_file(file):
            yield f"fd {fd:>3}", SOCKET_I(file.f_inode)


def _vma_pages(vma: Object, limit: int = 512):
    """Walk the page tables for this VMA and yield the pages that are resident.

    A VMA describes what may be mapped; only part of it is backed by physical
    pages at any moment.
    """
    mm = vma.vm_mm
    start, end = vma.vm_start.value_(), vma.vm_end.value_()
    count = 0
    for addr in range(start, end, 4096):
        if count >= limit:
            return
        page = ct.safe(lambda a=addr: follow_page(mm, a), None)
        if page is None or not page.value_():
            continue
        count += 1
        yield f"{addr:#x}  pfn {page_to_pfn(page).value_()}", page


def _page_zone(page: Object) -> Object | None:
    """The zone this frame was allocated from, found by its pfn.

    The kernel stores the zone index in bits of ``page->flags`` and reads it
    back with a shift whose width depends on the build. Searching the zones for
    the one whose pfn range contains this page reaches the same zone by reading
    fields that exist on every configuration.
    """
    pfn = page_to_pfn(page).value_()
    for pgdat in for_each_online_pgdat(page.prog_):
        for index in range(pgdat.nr_zones.value_()):
            zone = pgdat.node_zones[index]
            start = zone.zone_start_pfn.value_()
            spanned = zone.spanned_pages.value_()
            if spanned and start <= pfn < start + spanned:
                return zone.address_of_()
    return None


def _mapping_vmas(page: Object) -> Iterator[Object]:
    """Every VMA that the reverse mapping offers for this page.

    Which tree to walk is decided by one bit of ``page->mapping``. Anonymous
    memory points at an ``anon_vma`` whose red-black tree holds an
    ``anon_vma_chain`` per VMA sharing those pages, which after a fork means
    the VMAs of several processes. File memory points at the file's
    ``address_space``, whose ``i_mmap`` interval tree holds every VMA mapping
    that file anywhere in the system.
    """
    mapping = page.mapping.value_()
    if not mapping:
        return
    if mapping & PAGE_MAPPING_ANON:
        anon_vma = Object(page.prog_, "struct anon_vma *", mapping & ~PAGE_MAPPING_FLAGS)
        for avc in rbtree_inorder_for_each_entry(
            "struct anon_vma_chain", anon_vma.rb_root.rb_root.address_of_(), "rb"
        ):
            yield avc.vma
        return
    space = cast("struct address_space *", page.mapping)
    yield from rbtree_inorder_for_each_entry(
        "struct vm_area_struct", space.i_mmap.rb_root.address_of_(), "shared.rb"
    )


def _page_mappers(page: Object, limit: int = 64, scan: int = 4096):
    """The VMAs mapping this page, checked rather than assumed.

    Both reverse-mapping trees answer "may map", not "does map", and they are
    two different kinds of "may". A VMA whose range does not cover the folio's
    offset is in the tree only because it maps some other part of the same file
    or anon area, and is dropped here: an ``i_mmap`` tree for a shared library
    holds one VMA per mapping process per segment. A VMA that does cover the
    offset still may never have faulted the page in, which is what a fork
    leaves behind, so each one gets the check ``rmap_walk`` performs: compute
    the address the folio's index lands on and walk that address space's page
    tables to see whether the same page comes back.

    ``scan`` bounds the tree walk itself, because a popular file can be mapped
    by more VMAs than are worth reading to find the few that cover this page.
    """
    shift = page.prog_["PAGE_SHIFT"].value_()
    folio = cast("struct folio *", compound_head(page))
    index = folio.index.value_()
    head = compound_head(page).value_()
    found_count = 0
    for seen, vma in enumerate(_mapping_vmas(page)):
        if found_count >= limit or seen >= scan:
            return
        start, end = vma.vm_start.value_(), vma.vm_end.value_()
        addr = start + ((index - vma.vm_pgoff.value_()) << shift)
        if not start <= addr < end:
            continue
        found_count += 1
        name = as_text(vma_name(vma)) if vma_name(vma) else "anon"
        here = ct.safe(lambda: follow_page(vma.vm_mm, addr), None)
        mapped = (here is not None and here.value_()
                  and compound_head(here).value_() == head)
        state = "maps it" if mapped else "not faulted in"
        yield f"{addr:#x}  {state}  ({name})", vma


def _fdtable_files(fdt: Object):
    """Expand fd[] using max_fds, which the type cannot express.

    ``fdt->fd`` is ``struct file **``: a pointer to an array whose length lives
    in the sibling field ``max_fds``. Following the pointer alone reaches only
    fd 0, so the bound has to be supplied here.
    """
    limit = ct.safe(lambda: fdt.max_fds.value_(), 0)
    for index in range(min(limit, 4096)):
        file = ct.safe(lambda i=index: fdt.fd[i], None)
        if file is None or not file.value_():
            continue
        yield f"fd {index:>3}  {path_of(file)}", file


def _leaf_cfs_rqs(rq: Object):
    """Every cfs_rq on this CPU, not just the embedded root one.

    With group scheduling there is one cfs_rq per (task_group, CPU). They are
    allocated separately from the rq and chained onto rq->leaf_cfs_rq_list.
    """
    from drgn.helpers.linux.sched import task_group_name

    for cfs in list_for_each_entry(
        "struct cfs_rq", rq.leaf_cfs_rq_list.address_of_(), "leaf_cfs_rq_list"
    ):
        name = ct.safe(lambda c=cfs: as_text(task_group_name(c.tg)), "?")
        yield f"{name}  nr_queued={cfs.nr_queued.value_()}", cfs


def _namespaces(task: Object):
    nsproxy = task.nsproxy
    # A zombie has already dropped its nsproxy in exit_task_namespaces();
    # member_() only builds a reference, so the NULL would fault on read.
    if not nsproxy.value_():
        return
    for field in ("mnt_ns", "uts_ns", "ipc_ns", "net_ns", "pid_ns_for_children", "cgroup_ns"):
        value = ct.safe(lambda f=field: nsproxy.member_(f), None)
        if value is not None and value.value_():
            yield field, value


# ----------------------------------------------------------------- vfs


def _has_member(obj: Object, name: str) -> bool:
    """Whether the struct behind ``obj`` declares ``name`` on this kernel."""
    aggregate = ct.struct_type(obj.type_)
    return aggregate is not None and aggregate.has_member(name)


def _sb_mount_label(mount: Object) -> str:
    """A mount, with the namespace it is mounted in.

    The filesystem type is the same for every mount of one superblock, and the
    path repeats: a container that mounts /tmp from the host's tmpfs gives two
    rows reading "/tmp". The namespace inode number is what tells them apart,
    and it is the number ``readlink /proc/<pid>/ns/mnt`` prints.
    """
    dst = as_text(mount_dst(mount))
    inum = None
    if _has_member(mount, "mnt_ns"):
        inum = ct.safe(lambda: mount.mnt_ns.ns.inum.value_(), None)
    return f"{dst}  ns:{inum}" if inum else f"{dst}  (no namespace)"


def _sb_mounts(sb: Object):
    """Every mount using this superblock.

    One filesystem instance can be mounted in many places: bind mounts, btrfs
    subvolumes, and the same tree seen from another mount namespace all share
    one super_block. The chain is spelled two ways depending on the kernel, so
    read the member rather than the release: a ``struct mount *`` head threaded
    through mount->mnt_next_for_sb, or a list_head walked through
    mount->mnt_instance.
    """
    head = sb.s_mounts
    if ct.strip(head.type_).kind == TypeKind.POINTER:
        while head.value_():
            yield _sb_mount_label(head), head
            head = head.mnt_next_for_sb
        return
    for mount in list_for_each_entry(
        "struct mount", head.address_of_(), "mnt_instance"
    ):
        yield _sb_mount_label(mount), mount


def _fs_type_supers(fs_type: Object):
    """Every live superblock of this filesystem type.

    One file_system_type is shared by every instance -- all the tmpfs mounts
    point at the same one -- so this is how you get from the type back to the
    individual filesystems. Each is labelled with where it is mounted, since
    s_id is the same string for all of them.
    """
    for sb in hlist_for_each_entry(
        "struct super_block", fs_type.fs_supers.address_of_(), "s_instances"
    ):
        first = None
        if _has_member(sb, "s_mounts"):
            first = ct.safe(lambda s=sb: next(_sb_mounts(s), None), None)
        where = "(not mounted)"
        if first is not None:
            where = ct.safe(lambda m=first[1]: as_text(mount_dst(m)), "?")
        yield f"{as_text(sb.s_id.string_())}  {where}", sb


def _sock_prot(s: Object) -> Object:
    """The struct proto behind a sock.

    Newer kernels moved the pointer into the common part: read sk->sk_prot
    where it still exists, sk->__sk_common.skc_prot where it does not.
    """
    if _has_member(s, "sk_prot"):
        return s.sk_prot
    return s.__sk_common.skc_prot


# --------------------------------------------------------------------- block


def _disk_partitions(disk: Object):
    """The partitions of a disk, from the xarray it indexes them in.

    Index 0 is the whole disk (``part0``), so a disk with no partition table
    still yields one entry, and the numbering matches the name: index 2 is
    vda2.
    """
    for index, entry in xa_for_each(disk.part_tbl):
        bdev = Object(disk.prog_, "struct block_device *", entry)
        yield f"{index}  {as_text(part_name(bdev))}", bdev


def _queue_hw_queues(queue: Object):
    """The hardware queues of a request_queue, in queue_num order."""
    for index in range(int(queue.nr_hw_queues)):
        hctx = queue.queue_hw_ctx[index]
        yield f"hctx {index}  {int(hctx.nr_ctx)} cpu queue(s)", hctx


def _hctx_sw_queues(hctx: Object):
    """The per-CPU software queues mapped to this hardware queue.

    ``hctx->ctxs`` is the array the mapping built; ``nr_ctx`` is how much of
    it is used, and reading past that returns stale pointers.
    """
    for index in range(int(hctx.nr_ctx)):
        ctx = hctx.ctxs[index]
        yield f"cpu {int(ctx.cpu)}", ctx


def _queue_in_flight(queue: Object):
    """Requests this queue has dispatched and not yet completed.

    The tag bitmap is the only registry of live requests, so this is a walk of
    the bitmap rather than of a list.
    """
    for request in request_queue_busy_iter(queue):
        tag = ct.safe(lambda: int(request.tag), -1)
        yield f"tag {tag}  {request_op_name(request.cmd_flags)}", request


def _request_bios(request: Object):
    """The bios merged into this request, in submission order.

    A request starts as one bio and grows by merging adjacent ones, which is
    what an I/O scheduler is for. ``biotail`` is the last of the chain.
    """
    bio = request.bio
    while bio:
        sector = ct.safe(lambda: int(bio.bi_iter.bi_sector), 0)
        yield f"sector {sector}", bio
        bio = bio.bi_next


def _bio_pages(bio: Object):
    """The pages this bio transfers, from its bio_vec array.

    ``bi_vcnt`` counts the vectors that were filled in; ``bi_max_vecs`` is the
    allocation, and the entries between the two hold nothing. One vector can
    cover more than a page: since multipage bvecs, ``bv_len`` runs over as many
    physically contiguous pages as were available, and ``bv_page`` is the first
    of them. So the count here is vectors, not pages transferred.
    """
    for index in range(int(bio.bi_vcnt)):
        vec = bio.bi_io_vec[index]
        length = ct.safe(lambda: int(vec.bv_len), 0)
        yield f"bv_page[{index}]  first of {length} bytes", vec.bv_page


def _disk_capacity(disk: Object) -> str:
    """Size of the whole disk, from part0's sector count."""
    sectors = ct.safe(lambda: int(disk.part0.bd_nr_sectors), 0)
    return f"{sectors} sectors, {sectors * 512 / (1 << 30):.1f} GiB"


def _queue_path(queue: Object) -> str:
    """Whether this queue takes requests or bios.

    ``mq_ops`` is the driver's blk-mq table. NULL means the driver registered
    a ``submit_bio`` instead, and then no request is ever built.
    """
    if ct.safe(lambda: queue.mq_ops.value_(), 0):
        return f"blk-mq, {int(queue.nr_hw_queues)} hardware queue(s)"
    return "bio-based: the driver's submit_bio takes each bio directly"


def _queue_elevator(queue: Object) -> str:
    """The I/O scheduler sorting this queue, or none."""
    if not ct.safe(lambda: queue.elevator.value_(), 0):
        return "none (requests dispatch in the order they arrive)"
    return ct.safe(
        lambda: as_text(queue.elevator.type.elevator_name.string_()), "?"
    )


def _hctx_tags(hctx: Object) -> str:
    """How much of this queue's depth is in use.

    A request holds a tag from allocation to completion, so tags in use is the
    count of requests the driver is working on.
    """
    depth = ct.safe(lambda: int(hctx.tags.nr_tags), 0)
    active = ct.safe(lambda: int(hctx.nr_active), 0)
    return f"{active} of {depth} tag(s) in use"


def _request_target(request: Object) -> str:
    """What this request asks the device for: where, how much, which way."""
    op = request_op_name(request.cmd_flags)
    sector = ct.safe(lambda: int(blk_rq_pos(request)), 0)
    nbytes = ct.safe(lambda: int(blk_rq_bytes(request)), 0)
    return f"{op} {nbytes} bytes at sector {sector}"


def _bio_target(bio: Object) -> str:
    """The device and sector this bio was submitted against."""
    name = ct.safe(lambda: as_text(part_name(bio.bi_bdev)), "?")
    sector = ct.safe(lambda: int(bio.bi_iter.bi_sector), 0)
    size = ct.safe(lambda: int(bio.bi_iter.bi_size), 0)
    return f"{request_op_name(bio.bi_opf)} {size} bytes on {name} at sector {sector}"


def _bdev_devt(bdev: Object) -> str:
    """The major:minor a /dev node names this device by."""
    dev = ct.safe(lambda: int(bdev.bd_dev), 0)
    return f"{dev >> 20}:{dev & 0xFFFFF}"


# ----------------------------------------------------------------------- ipc


def _ipc_task(pid: Object):
    """The task a ``struct pid *`` on an IPC object still refers to.

    These record who last acted on the object: the last sender, the creator,
    the process that ran the last semop. The creating process has usually
    exited, and the pid then holds no task. The IPC objects keep a struct pid
    rather than a task_struct so that this case reads as NULL instead of as a
    pointer to whatever now occupies that memory.
    """
    if not ct.safe(lambda: pid.value_(), 0):
        return None
    task = ct.safe(lambda: pid_task(pid, pid.prog_["PIDTYPE_PID"]), None)
    # pid_task answers with a NULL object rather than nothing when the pid has
    # no task left, and a link resolving to NULL is a row that goes nowhere.
    if task is None or not ct.safe(lambda: task.value_(), 0):
        return None
    return task


def _ipc_pid_label(pid: Object) -> str:
    """"pid, and the program running under it, or what became of it."""
    if not ct.safe(lambda: pid.value_(), 0):
        return "none recorded"
    task = _ipc_task(pid)
    if task is None or not ct.safe(lambda: task.value_(), 0):
        return "the process has exited"
    return f"{ct.safe(lambda: int(task.pid), -1)}  {ct.safe(lambda: task_comm(task), '?')}"


def _task_shm_segments(task: Object):
    """The segments this task created, which it is still on the hook for.

    ``shm_clist`` holds the segments this task created. The kernel keeps it so
    that shm_rmid_forced can destroy them when the creator exits. Segments this
    task has attached are found instead among its VMAs, as mappings of the
    segment's file.
    """
    for segment in list_for_each_entry(
        "struct shmid_kernel", task.sysvshm.shm_clist.address_of_(), "shm_clist"
    ):
        yield (
            f"shmid {ct.safe(lambda: int(segment.shm_perm.id), -1)}  "
            f"{ct.safe(lambda: int(segment.shm_segsz), 0)} bytes",
            segment,
        )


def _list_nonempty(head: Object) -> bool:
    """Whether a list_head has entries, and is safe to walk.

    An empty list points at itself. A list that was never initialised has a
    NULL next instead, which walking dereferences: the idle tasks reach the
    crawl with sysvshm.shm_clist in that state, since INIT_TASK does not set
    it up. Both cases have to be excluded before the walk.
    """
    following = ct.safe(lambda: head.next.value_(), 0)
    return bool(following) and following != head.address_of_().value_()


def _ipc_key(perm: Object) -> str:
    """The key, as ipcs prints it, plus what IPC_PRIVATE means."""
    raw = ct.safe(lambda: int(perm.key), 0) & 0xFFFFFFFF
    if raw == 0:
        return "0x00000000 (IPC_PRIVATE: no key, reachable only by id)"
    return f"0x{raw:08x}"


def _ipc_owner(perm: Object) -> str:
    """Owner and creator ids, with names where this is the local machine."""
    uid = ct.safe(lambda: int(perm.uid.val), -1)
    gid = ct.safe(lambda: int(perm.gid.val), -1)
    cuid = ct.safe(lambda: int(perm.cuid.val), -1)
    owner = f"uid {_id_label(uid)}, gid {_id_label(gid)}"
    if cuid != uid:
        return f"{owner} (created by uid {_id_label(cuid)})"
    return owner


def _ipc_mode(perm: Object) -> str:
    """The permission bits, and who they let in."""
    mode = ct.safe(lambda: int(perm.mode), 0) & 0o7777
    bits = "".join(
        letter if mode & bit else "-"
        for letter, bit in (
            ("r", 0o400), ("w", 0o200), ("r", 0o040), ("w", 0o020),
            ("r", 0o004), ("w", 0o002),
        )
    )
    return f"{mode:04o}  {bits[:2]} owner, {bits[2:4]} group, {bits[4:]} other"


def _ns_message_queues(ns: Object):
    for queue in for_each_sysv_msg_queue(ns):
        yield (
            f"msqid {ct.safe(lambda: int(queue.q_perm.id), -1)}  "
            f"{ct.safe(lambda: int(queue.q_qnum), 0)} message(s)",
            queue,
        )


def _ns_semaphore_arrays(ns: Object):
    for array in for_each_sysv_sem_array(ns):
        yield (
            f"semid {ct.safe(lambda: int(array.sem_perm.id), -1)}  "
            f"{ct.safe(lambda: int(array.sem_nsems), 0)} semaphore(s)",
            array,
        )


def _ns_shared_memory(ns: Object):
    for segment in for_each_sysv_shm(ns):
        yield (
            f"shmid {ct.safe(lambda: int(segment.shm_perm.id), -1)}  "
            f"{ct.safe(lambda: int(segment.shm_segsz), 0)} bytes",
            segment,
        )


def _ipc_ns_counts(ns: Object) -> str:
    """How many of each kind this namespace holds."""
    names = ((0, "semaphore array"), (1, "message queue"), (2, "shared memory segment"))
    parts = []
    for index, name in names:
        count = ct.safe(lambda: int(ns.ids[index].in_use), 0)
        parts.append(f"{count} {name}" + ("s" if count != 1 else ""))
    return ", ".join(parts)


def _msg_messages(queue: Object):
    for message in list_for_each_entry(
        "struct msg_msg", queue.q_messages.address_of_(), "m_list"
    ):
        yield (
            f"type {ct.safe(lambda: int(message.m_type), 0)}  "
            f"{ct.safe(lambda: int(message.m_ts), 0)} bytes",
            message,
        )


def _msg_receivers(queue: Object):
    for receiver in list_for_each_entry(
        "struct msg_receiver", queue.q_receivers.address_of_(), "r_list"
    ):
        task = ct.safe(lambda: receiver.r_tsk, None)
        label = ct.safe(lambda: task_comm(task), "?") if task else "?"
        yield f"waiting: {label}", receiver


def _msg_usage(queue: Object) -> str:
    """Why a sender would block here: the byte budget, not the count."""
    used = ct.safe(lambda: int(queue.q_cbytes), 0)
    limit = ct.safe(lambda: int(queue.q_qbytes), 0)
    count = ct.safe(lambda: int(queue.q_qnum), 0)
    return f"{count} message(s), {used} of {limit} bytes used"


def _sem_members(array: Object):
    """The individual semaphores, which is what an operation names by index."""
    for index in range(ct.safe(lambda: int(array.sem_nsems), 0)):
        member = array.sems[index]
        yield f"sems[{index}] = {ct.safe(lambda: int(member.semval), -1)}", member.address_of_()


def _sem_pending(head: Object):
    for queue in list_for_each_entry("struct sem_queue", head.address_of_(), "list"):
        task = ct.safe(lambda: queue.sleeper, None)
        who = ct.safe(lambda: task_comm(task), "?") if task else "(gone)"
        yield f"{ct.safe(lambda: int(queue.nsops), 0)} operation(s), {who}", queue


def _sem_values(array: Object) -> str:
    count = ct.safe(lambda: int(array.sem_nsems), 0)
    values = ",".join(
        str(ct.safe(lambda: int(array.sems[index].semval), -1))
        for index in range(min(count, 12))
    )
    return f"[{values}{',…' if count > 12 else ''}]"


def _sem_queue_operations(queue: Object) -> str:
    """What the blocked task asked for, and whether it changes a value."""
    count = ct.safe(lambda: int(queue.nsops), 0)
    alter = ct.safe(lambda: int(queue.alter), 0)
    kind = "would change a value" if alter else "waiting for zero"
    return f"{count} operation(s), {kind}"


def _shm_pages(segment: Object) -> str:
    """The segment's size, in bytes and in pages."""
    size = ct.safe(lambda: int(segment.shm_segsz), 0)
    return f"{size} bytes, {(size + 4095) // 4096} page(s) at 4K"


LINKS: dict[str, list[Link]] = {
    "task_struct": [
        Link("threads", "Tasks sharing task->signal and task->tgid (thread group).", _threads,
             origin="task->signal->thread_head (thread_node)",
             userspace="ls /proc/<pid>/task, or ps -L -p <pid>"),
        Link("mm (address space)", "Userspace address space in task->mm.",
             lambda t: t.mm,
             # Deliberately still offered for a kernel thread, whose task->mm
             # is NULL: the empty row says a kthread has no address space of
             # its own, which is worth seeing. The links that walk *through*
             # mm are the ones that have to be hidden.
             origin="task->mm",
             userspace="grep VmRSS /proc/<pid>/status"),
        Link(
            "active_mm (borrowed)",
            "The mm whose page tables are loaded. A kthread borrows the "
            "previous task's rather than switching (lazy TLB).",
            lambda t: t.active_mm,
            applies=lambda t: t.mm != t.active_mm and t.active_mm.value_() != 0,
            origin="task->active_mm",
        ),
        Link("VMAs", "Mapped regions of this task's address space.", _vmas,
             # A kthread has no mm, and walking mm->mm_mt through NULL faults.
             applies=lambda t: t.mm.value_() != 0,
             origin="walks task->mm->mm_mt (maple tree)",
             userspace="cat /proc/<pid>/maps"),
        Link("open files", "fd table: struct file per descriptor.", _open_files,
             origin="walks task->files->fdt->fd[]",
             userspace="ls -l /proc/<pid>/fd, or lsof -p <pid>"),
        Link("sockets", "File descriptors whose file->f_op is socket_file_ops.", _sockets,
             origin="task->files->fdt->fd[], f_op==socket_file_ops",
             userspace="ss -tanp | grep pid=<pid>"),
        Link("children", "Tasks this one forked, linked by sibling.", _children,
             origin="walks task->children, linked by sibling",
             userspace="pgrep -P <pid>"),
        Link("parent", "The real parent task.", lambda t: t.real_parent,
             origin="task->real_parent",
             userspace="ps -o ppid= -p <pid>"),
        Link(
            "worker (kworker)",
            "The struct worker this kthread runs as, and through it the pool "
            "it takes work from.",
            _task_worker,
            applies=lambda t: int(t.flags) & PF_WQ_WORKER,
            origin="searches the worker pools for a worker whose task is this one",
        ),
        Link(
            "blocked on (mutex)",
            "The mutex this task is sleeping on, which records its holder.",
            lambda t: t.blocked_on,
            applies=lambda t: ct.safe(lambda: t.blocked_on.value_(), 0) != 0,
            origin="task->blocked_on",
            userspace="cat /proc/<pid>/stack",
        ),
        Link("runqueue", "The struct rq this task is queued on.", task_rq,
             origin="task_rq() = cpu_rq(task_cpu(task))",
             userspace="ps -o psr= -p <pid>  # the CPU, not the rq"),
        Link("sched_entity", "CFS scheduling state (vruntime, load).",
             lambda t: t.se.address_of_(),
             origin="&task->se",
             userspace="cat /proc/<pid>/sched"),
        Link("cred (subjective)", "Credentials this task acts with.", lambda t: t.cred,
             origin="task->cred",
             userspace="grep -E 'Uid|Gid' /proc/<pid>/status"),
        Link(
            "real_cred (objective)",
            "Credentials this task is; differs from cred only while acting on "
            "another task's behalf.",
            lambda t: t.real_cred,
            applies=lambda t: t.real_cred.value_() != t.cred.value_(),
            origin="task->real_cred",
            userspace="grep -E 'Uid|Gid' /proc/<pid>/status",
        ),
        Link("namespaces", "The nsproxy's namespaces.", _namespaces,
             origin="task->nsproxy members",
             userspace="ls -l /proc/<pid>/ns"),
        Link("shared memory created",
             "System V segments this task created and still owns.",
             _task_shm_segments,
             applies=lambda t: (_has_member(t, "sysvshm")
                               and _list_nonempty(t.sysvshm.shm_clist)),
             origin="walks task->sysvshm.shm_clist (shmid_kernel->shm_clist)",
             userspace="ipcs -m  # match the cpid column"),
        Link("signal", "signal_struct: shared signal state for the group.",
             lambda t: t.signal,
             origin="task->signal",
             userspace="grep -E 'Sig|Shd' /proc/<pid>/status"),
        Link("fs (cwd/root)", "fs_struct: working directory and root.", lambda t: t.fs,
             origin="task->fs",
             userspace="ls -l /proc/<pid>/cwd /proc/<pid>/root"),
    ],
    "rq": [
        Link("curr (running task)", "The task currently executing on this CPU.",
             lambda r: r.curr,
             origin="rq->curr",
             userspace="ps -eo pid,psr,comm --sort=psr"),
        Link("idle task", "This CPU's swapper task.", lambda r: r.idle,
             origin="rq->idle"),
        Link("cfs_rq (embedded root)", "Root CFS runqueue embedded in struct rq.",
             lambda r: r.cfs.address_of_(),
             origin="&rq->cfs (member, not a pointer)"),
        Link("all cfs_rqs on this CPU", "CFS runqueues on rq->leaf_cfs_rq_list.",
             _leaf_cfs_rqs,
             origin="walks rq->leaf_cfs_rq_list"),
        Link("rt_rq", "The realtime-class runqueue.", lambda r: r.rt.address_of_(),
             origin="&rq->rt"),
        Link("dl_rq", "The deadline-class runqueue.", lambda r: r.dl.address_of_(),
             origin="&rq->dl"),
    ],
    "cfs_rq": [
        Link("rq (this CPU)", "The runqueue this cfs_rq belongs to.", lambda c: c.rq,
             origin="cfs_rq->rq"),
        Link("task_group", "The cgroup whose share this queue represents.", lambda c: c.tg,
             origin="cfs_rq->tg",
             userspace="cat /proc/<pid>/cgroup"),
        Link("curr (running entity)", "The entity currently on the CPU here.",
             lambda c: c.curr, applies=lambda c: c.curr.value_() != 0,
             origin="cfs_rq->curr"),
    ],
    "sched_entity": [
        Link(
            "cfs_rq it sits on",
            "CFS runqueue containing this scheduling entity.",
            lambda se: se.cfs_rq,
            applies=lambda se: se.cfs_rq.value_() != 0,
            origin="se->cfs_rq",
        ),
        Link(
            "my_q (queue it owns)",
            "Child cfs_rq for a group entity; NULL for a task entity.",
            lambda se: se.my_q,
            applies=lambda se: se.my_q.value_() != 0,
            origin="se->my_q (NULL for task entities)",
        ),
        Link("parent entity", "The entity one level up the hierarchy.",
             lambda se: se.parent, applies=lambda se: se.parent.value_() != 0,
             origin="se->parent"),
    ],
    "signal_struct": [
        Link("thread_head (the group's tasks)",
             "The list task_struct/threads walks; each task links in by thread_node.",
             _signal_threads,
             origin="&signal->thread_head (thread_node)",
             userspace="ls /proc/<pid>/task"),
        Link("curr_target (next signal target)",
             "Where the next group-directed signal gets delivered, not the leader.",
             lambda s: s.curr_target, applies=lambda s: s.curr_target.value_() != 0,
             origin="signal->curr_target"),
    ],
    "mm_struct": [
        Link("VMAs", "vm_area_struct instances in the address space's maple tree.",
             _mm_vmas,
             origin="walks the VMA tree",
             userspace="cat /proc/<pid>/maps"),
        Link("owner", "The task that owns this mm.", lambda m: m.owner,
             origin="mm->owner"),
    ],
    "vm_area_struct": [
        Link("mm", "The address space containing this region.", lambda v: v.vm_mm,
             origin="vma->vm_mm",
             userspace="cat /proc/<pid>/maps"),
        Link("file", "The mapped file, if this isn't anonymous memory.", lambda v: v.vm_file,
             origin="vma->vm_file",
             userspace="awk '{print $6}' /proc/<pid>/maps"),
        Link(
            "resident pages",
            "Present struct page mappings found by walking the VMA's page tables.",
            lambda v: _vma_pages(v),
            origin="page table walk (follow_page)",
            userspace="grep Rss /proc/<pid>/smaps",
        ),
        Link("anon_vma", "Reverse mapping for anonymous pages.", lambda v: v.anon_vma,
             origin="vma->anon_vma"),
    ],
    "kmem_cache": [
        Link(
            "allocated objects",
            "Allocated objects found by walking the cache's slabs.",
            lambda c: (
                (f"{o.value_():#x}", o)
                for o in slab_cache_for_each_allocated_object(c, "void *")
            ),
            origin="walks the cache's slabs",
            userspace="slabtop, or cat /proc/slabinfo",
        ),
        Link("next cache", "The following entry in the global slab_caches list.",
             lambda c: cast("struct kmem_cache *", c.list.next),
             origin="cache->list.next, container_of",
             userspace="cat /proc/slabinfo"),
    ],
    "page": [
        Link("compound head", "The head page, if this is a tail of a compound page.",
             compound_head,
             origin="compound_head() helper"),
        Link("as folio", "struct folio overlays struct page in modern kernels.",
             lambda p: cast("struct folio *", p),
             origin="cast, same address"),
        Link(
            "zone",
            "The zone this frame was allocated from, and so the free lists it "
            "returns to.",
            _page_zone,
            applies=lambda p: _page_zone(p) is not None,
            origin="the zone whose pfn range contains page_to_pfn(page)",
            userspace="cat /proc/zoneinfo  # per zone, not per page",
        ),
        Link(
            "mapped by",
            "The VMAs whose page tables reach this page: reverse mapping, so "
            "the answer can span several processes.",
            _page_mappers,
            applies=lambda p: p.mapping.value_() != 0,
            origin="page->mapping: anon_vma tree or i_mmap, then a page table "
                   "walk to confirm each candidate",
            userspace="no equivalent; /proc/<pid>/pagemap goes the other way",
        ),
    ],
    "zone": [
        Link("node", "The NUMA node this zone is part of.",
             lambda z: z.zone_pgdat,
             origin="zone->zone_pgdat",
             userspace="grep '^Node' /proc/zoneinfo"),
    ],
    "sk_buff": [
        Link("dev", "The net_device this skb is associated with.", lambda s: s.dev,
             applies=lambda s: s.dev.value_() != 0,
             origin="skb->dev (union with dev_scratch)"),
        Link("sk (owning socket)", "The socket that owns this skb, if any.", lambda s: s.sk,
             applies=lambda s: s.sk.value_() != 0,
             origin="skb->sk"),
        Link("shinfo", "skb_shared_info: frags, gso, and the frag list.",
             lambda s: skb_shinfo(s),
             origin="skb_shinfo(), past skb->end"),
        Link("next in queue", "The following skb in this queue.", lambda s: s.next,
             applies=lambda s: s.next.value_() != 0,
             origin="skb->next"),
    ],
    "file": [
        Link("dentry", "The directory entry this file refers to.", lambda f: f.f_path.dentry,
             origin="file->f_path.dentry",
             userspace="readlink /proc/<pid>/fd/<n>"),
        Link("inode", "The inode behind it.", lambda f: f.f_inode,
             origin="file->f_inode",
             userspace="stat -L /proc/<pid>/fd/<n>"),
        Link("f_op", "File operations table.", lambda f: f.f_op,
             origin="file->f_op"),
        Link(
            "socket",
            "This fd is a socket: SOCKET_I() recovers it from the inode.",
            lambda f: SOCKET_I(f.f_inode),
            applies=_is_socket_file,
            origin="SOCKET_I(f_inode), container_of",
            userspace="ss -tanp",
        ),
    ],
    "files_struct": [
        Link(
            "fdt (current fd table)",
            "The live fdtable. It is swapped on resize, so read it once.",
            lambda f: f.fdt,
            origin="files->fdt",
            userspace="ls -l /proc/<pid>/fd",
        ),
        Link(
            "open files",
            "Non-NULL fdt->fd[0..max_fds) entries.",
            lambda f: _fdtable_files(f.fdt),
            origin="files->fdt->fd[0..max_fds)",
            userspace="ls -l /proc/<pid>/fd",
        ),
    ],
    "fdtable": [
        Link(
            "fd[] entries",
            "The descriptor array. Its length is max_fds, not part of the type.",
            _fdtable_files,
            origin="fdt->fd[0..max_fds), skipping NULL",
            userspace="ls -l /proc/<pid>/fd",
        ),
    ],
    "socket": [
        Link("sk (struct sock)", "Protocol state referenced by socket->sk.",
             lambda s: s.sk,
             origin="socket->sk",
             userspace="ss -tanie"),
        Link("file", "The struct file this socket is exposed through.", lambda s: s.file,
             origin="socket->file",
             userspace="ls -l /proc/<pid>/fd"),
        Link("ops", "Protocol-specific struct proto_ops table.", lambda s: s.ops,
             origin="socket->ops"),
    ],
    "sock": [
        Link("socket (struct socket)", "Associated struct socket; NULL when no file descriptor exists.",
             lambda s: s.sk_socket,
             origin="sk->sk_socket"),
        Link("proto", "struct proto: tcp_prot, udp_prot, unix_stream_proto…",
             _sock_prot,
             origin="sk->sk_prot, or sk->__sk_common.skc_prot on kernels that moved it",
             userspace="ss -tani"),
        Link(
            "as tcp_sock",
            "struct sock is the first member of tcp_sock, so this is a cast.",
            lambda s: cast("struct tcp_sock *", s),
            applies=lambda s: s.sk_protocol == IPPROTO_TCP,
            origin="cast, same address",
            userspace="ss -ti",
        ),
        Link(
            "as udp_sock",
            "Same layering: udp_sock embeds inet_sock embeds sock.",
            lambda s: cast("struct udp_sock *", s),
            applies=lambda s: s.sk_protocol == IPPROTO_UDP,
            origin="cast, same address",
        ),
        Link(
            "as unix_sock",
            "AF_UNIX's own wrapper around struct sock.",
            lambda s: cast("struct unix_sock *", s),
            applies=lambda s: s.__sk_common.skc_family == AF_UNIX,
            origin="cast, same address",
        ),
    ],
    "mount": [
        Link("superblock", "The filesystem instance.", lambda m: m.mnt.mnt_sb,
             origin="mount->mnt.mnt_sb",
             userspace="findmnt, or cat /proc/self/mountinfo"),
        Link("mountpoint", "The dentry this is mounted on.", lambda m: m.mnt_mountpoint,
             origin="mount->mnt_mountpoint",
             userspace="findmnt -T <path>"),
        Link("parent mount", "The mount this is mounted under.", lambda m: m.mnt_parent,
             origin="mount->mnt_parent",
             userspace="findmnt"),
    ],
    "super_block": [
        Link("root dentry", "Root of this filesystem.", lambda s: s.s_root,
             origin="sb->s_root",
             userspace="findmnt"),
        Link("fs type", "file_system_type describing it.", lambda s: s.s_type,
             origin="sb->s_type",
             userspace="findmnt -o FSTYPE"),
        Link("block device", "The block_device this filesystem was mounted from.",
             lambda s: s.s_bdev,
             applies=lambda s: _has_member(s, "s_bdev") and s.s_bdev.value_() != 0,
             origin="sb->s_bdev",
             userspace="findmnt -o SOURCE"),
        Link("mounts", "struct mount instances referencing this super_block.", _sb_mounts,
             applies=lambda s: _has_member(s, "s_mounts"),
             origin="sb->s_mounts",
             userspace="findmnt -o TARGET --source <device>"),
    ],
    "gendisk": [
        Link("request queue", "The queue every I/O to this disk goes through.",
             lambda d: d.queue,
             origin="disk->queue",
             userspace="ls /sys/block/<disk>/queue/"),
        Link("whole disk (part0)", "The block_device covering the whole disk.",
             lambda d: d.part0,
             origin="disk->part0",
             userspace="ls -l /dev/<disk>"),
        Link("partitions", "Every block_device carved out of this disk.",
             _disk_partitions,
             origin="walks the disk->part_tbl xarray",
             userspace="cat /proc/partitions"),
        Link("block_device_operations", "The driver's entry points for this disk.",
             lambda d: d.fops,
             origin="disk->fops"),
    ],
    "block_device": [
        Link("disk", "The gendisk this device or partition belongs to.",
             lambda b: b.bd_disk,
             origin="bdev->bd_disk",
             userspace="lsblk"),
        Link("request queue", "The queue this device's I/O goes through.",
             lambda b: b.bd_queue,
             origin="bdev->bd_queue",
             userspace="ls /sys/block/<disk>/queue/"),
        Link("page cache", "The address_space caching this device's blocks.",
             lambda b: b.bd_mapping,
             applies=lambda b: _has_member(b, "bd_mapping"),
             origin="bdev->bd_mapping",
             userspace="grep ^Buffers /proc/meminfo"),
        Link("device", "The driver-model device, as sysfs sees it.",
             lambda b: b.bd_device.address_of_(),
             origin="&bdev->bd_device",
             userspace="ls -l /sys/class/block/<name>"),
    ],
    "request_queue": [
        Link("disk", "The gendisk this queue serves.", lambda q: q.disk,
             applies=lambda q: q.disk.value_() != 0,
             origin="q->disk",
             userspace="lsblk"),
        Link("hardware queues", "The blk_mq_hw_ctx a driver dispatches from.",
             _queue_hw_queues,
             applies=lambda q: int(q.nr_hw_queues) > 0,
             origin="q->queue_hw_ctx[0 .. nr_hw_queues)",
             userspace="ls /sys/kernel/debug/block/<disk>/hctx*"),
        Link("in flight", "Requests dispatched and not yet completed.",
             _queue_in_flight,
             applies=lambda q: q.mq_ops.value_() != 0,
             origin="walks the tag bitmap (blk_mq_queue_tag_busy_iter)",
             userspace="cat /sys/block/<disk>/inflight"),
        Link("elevator", "The I/O scheduler instance sorting this queue.",
             lambda q: q.elevator,
             applies=lambda q: q.elevator.value_() != 0,
             origin="q->elevator",
             userspace="cat /sys/block/<disk>/queue/scheduler"),
        Link("tag set", "The tag set shared by this device's queues.",
             lambda q: q.tag_set,
             applies=lambda q: q.tag_set.value_() != 0,
             origin="q->tag_set",
             userspace="cat /sys/block/<disk>/queue/nr_requests"),
        Link("limits", "What this device accepts: block size, segments, sizes.",
             lambda q: q.limits.address_of_(),
             origin="&q->limits",
             userspace="ls /sys/block/<disk>/queue/"),
    ],
    "blk_mq_hw_ctx": [
        Link("request queue", "The queue this hardware queue belongs to.",
             lambda h: h.queue,
             origin="hctx->queue"),
        Link("software queues", "The per-CPU queues feeding this one.",
             _hctx_sw_queues,
             applies=lambda h: int(h.nr_ctx) > 0,
             origin="hctx->ctxs[0 .. nr_ctx)"),
        Link("tags", "The request pool and its bitmap: one tag per request.",
             lambda h: h.tags,
             applies=lambda h: h.tags.value_() != 0,
             origin="hctx->tags",
             userspace="cat /sys/kernel/debug/block/<disk>/hctx0/tags"),
        Link("scheduler tags", "The extra tags the I/O scheduler queues against.",
             lambda h: h.sched_tags,
             applies=lambda h: h.sched_tags.value_() != 0,
             origin="hctx->sched_tags"),
    ],
    "blk_mq_ctx": [
        Link("request queue", "The queue this per-CPU queue stages for.",
             lambda c: c.queue,
             origin="ctx->queue"),
    ],
    "blk_mq_tag_set": [
        Link("queues", "Every request_queue sharing this tag set.",
             lambda t: (
                 (as_text(q.disk.disk_name.string_()) if q.disk else "?", q)
                 for q in list_for_each_entry(
                     "struct request_queue", t.tag_list.address_of_(), "tag_set_list"
                 )
             ),
             applies=lambda t: _has_member(t, "tag_list"),
             origin="walks tag_set->tag_list (q->tag_set_list)"),
    ],
    "request": [
        Link("request queue", "The queue this request was allocated on.",
             lambda r: r.q,
             applies=lambda r: r.q.value_() != 0,
             origin="rq->q"),
        Link("hardware queue", "The hardware queue it dispatches from.",
             lambda r: r.mq_hctx,
             applies=lambda r: r.mq_hctx.value_() != 0,
             origin="rq->mq_hctx"),
        Link("software queue", "The per-CPU queue it was submitted on.",
             lambda r: r.mq_ctx,
             applies=lambda r: r.mq_ctx.value_() != 0,
             origin="rq->mq_ctx"),
        Link("bios", "The bios merged into this request.", _request_bios,
             applies=lambda r: r.bio.value_() != 0,
             origin="walks rq->bio, chained by bio->bi_next"),
        Link("partition", "The block_device the I/O is accounted to.",
             lambda r: r.part,
             applies=lambda r: _has_member(r, "part") and r.part.value_() != 0,
             origin="rq->part",
             userspace="cat /proc/diskstats"),
    ],
    "bio": [
        Link("block device", "The device or partition this bio targets.",
             lambda b: b.bi_bdev,
             applies=lambda b: b.bi_bdev.value_() != 0,
             origin="bio->bi_bdev",
             userspace="lsblk"),
        Link("pages", "The pages this bio transfers.", _bio_pages,
             applies=lambda b: int(b.bi_vcnt) > 0,
             origin="bio->bi_io_vec[0 .. bi_vcnt), each bv_page"),
        Link("next bio", "The next bio in a merged chain.", lambda b: b.bi_next,
             applies=lambda b: b.bi_next.value_() != 0,
             origin="bio->bi_next"),
    ],
    "elevator_queue": [
        Link("scheduler", "The elevator_type this instance runs.", lambda e: e.type,
             origin="elevator->type",
             userspace="cat /sys/block/<disk>/queue/scheduler"),
    ],
    "ipc_namespace": [
        Link("message queues", "System V message queues created in this namespace.",
             _ns_message_queues,
             applies=lambda n: int(n.ids[1].in_use) > 0,
             origin="walks ns->ids[IPC_MSG_IDS].ipcs_idr",
             userspace="ipcs -q"),
        Link("semaphore arrays", "System V semaphore arrays in this namespace.",
             _ns_semaphore_arrays,
             applies=lambda n: int(n.ids[0].in_use) > 0,
             origin="walks ns->ids[IPC_SEM_IDS].ipcs_idr",
             userspace="ipcs -s"),
        Link("shared memory", "System V shared memory segments in this namespace.",
             _ns_shared_memory,
             applies=lambda n: int(n.ids[2].in_use) > 0,
             origin="walks ns->ids[IPC_SHM_IDS].ipcs_idr",
             userspace="ipcs -m"),
    ],
    "msg_queue": [
        Link("permissions", "kern_ipc_perm: the key, owner and mode.",
             lambda q: q.q_perm.address_of_(),
             origin="&queue->q_perm",
             userspace="ipcs -q"),
        Link("messages", "Messages sent and not yet received.", _msg_messages,
             applies=lambda q: int(q.q_qnum) > 0,
             origin="walks queue->q_messages (msg_msg->m_list)",
             userspace="ipcs -q  # the messages column"),
        Link("receivers", "Tasks blocked in msgrcv(2) on this queue.", _msg_receivers,
             applies=lambda q: _list_nonempty(q.q_receivers),
             origin="walks queue->q_receivers (msg_receiver->r_list)"),
        Link("last sender", "The process that last put a message on this queue.",
             lambda q: _ipc_task(q.q_lspid),
             applies=lambda q: _ipc_task(q.q_lspid) is not None,
             origin="pid_task(queue->q_lspid)",
             userspace="ipcs -q -i <msqid>  # lspid"),
        Link("last receiver", "The process that last took a message off it.",
             lambda q: _ipc_task(q.q_lrpid),
             applies=lambda q: _ipc_task(q.q_lrpid) is not None,
             origin="pid_task(queue->q_lrpid)",
             userspace="ipcs -q -i <msqid>  # lrpid"),
    ],
    "sem_array": [
        Link("permissions", "kern_ipc_perm: the key, owner and mode.",
             lambda a: a.sem_perm.address_of_(),
             origin="&array->sem_perm",
             userspace="ipcs -s"),
        Link("semaphores", "The individual semaphores an operation indexes into.",
             _sem_members,
             applies=lambda a: int(a.sem_nsems) > 0,
             origin="array->sems[0 .. sem_nsems)",
             userspace="ipcs -s -i <semid>"),
        Link("pending (alter)",
             "Operations spanning several semaphores that would change a value.",
             lambda a: _sem_pending(a.pending_alter),
             applies=lambda a: _list_nonempty(a.pending_alter),
             origin="walks array->pending_alter (sem_queue->list)"),
        Link("pending (const)",
             "Operations spanning several semaphores waiting for a zero.",
             lambda a: _sem_pending(a.pending_const),
             applies=lambda a: _list_nonempty(a.pending_const),
             origin="walks array->pending_const (sem_queue->list)"),
    ],
    "sem": [
        Link("pending (alter)",
             "Operations on this one semaphore that would change its value.",
             lambda s: _sem_pending(s.pending_alter),
             applies=lambda s: _list_nonempty(s.pending_alter),
             origin="walks sem->pending_alter (sem_queue->list)"),
        Link("pending (const)",
             "Operations on this one semaphore waiting for it to reach zero.",
             lambda s: _sem_pending(s.pending_const),
             applies=lambda s: _list_nonempty(s.pending_const),
             origin="walks sem->pending_const (sem_queue->list)"),
        Link("last operation by", "The process whose semop(2) last changed this value.",
             lambda s: _ipc_task(s.sempid),
             applies=lambda s: _ipc_task(s.sempid) is not None,
             origin="pid_task(sem->sempid)",
             userspace="ipcs -s -i <semid>  # the pid column"),
    ],
    "sem_queue": [
        Link("blocked task", "The task waiting inside semop(2) for this operation.",
             lambda q: q.sleeper,
             applies=lambda q: q.sleeper.value_() != 0,
             origin="sem_queue->sleeper",
             userspace="cat /proc/<pid>/stack"),
    ],
    "msg_receiver": [
        Link("blocked task", "The task waiting inside msgrcv(2).", lambda r: r.r_tsk,
             applies=lambda r: r.r_tsk.value_() != 0,
             origin="msg_receiver->r_tsk"),
    ],
    "msg_msg": [
        Link("next segment", "The rest of a message too long for one page.",
             lambda m: m.next,
             applies=lambda m: m.next.value_() != 0,
             origin="msg_msg->next (struct msg_msgseg)"),
    ],
    "shmid_kernel": [
        Link("permissions", "kern_ipc_perm: the key, owner and mode.",
             lambda s: s.shm_perm.address_of_(),
             origin="&segment->shm_perm",
             userspace="ipcs -m"),
        Link("file", "The tmpfs file holding the segment's pages.",
             lambda s: s.shm_file,
             applies=lambda s: s.shm_file.value_() != 0,
             origin="segment->shm_file",
             userspace="none: the file has no path, it lives in an internal mount"),
        Link("namespace", "The IPC namespace this segment belongs to.",
             lambda s: s.ns,
             applies=lambda s: s.ns.value_() != 0,
             origin="segment->ns",
             userspace="readlink /proc/<pid>/ns/ipc"),
        Link("creator", "The process that called shmget(2), if it is still running.",
             lambda s: _ipc_task(s.shm_cprid),
             applies=lambda s: _ipc_task(s.shm_cprid) is not None,
             origin="pid_task(segment->shm_cprid)",
             userspace="ipcs -m -i <shmid>  # cpid"),
        Link("last attached or detached by",
             "The process that last called shmat(2) or shmdt(2).",
             lambda s: _ipc_task(s.shm_lprid),
             applies=lambda s: _ipc_task(s.shm_lprid) is not None,
             origin="pid_task(segment->shm_lprid)",
             userspace="ipcs -m -i <shmid>  # lpid"),
    ],
    "file_system_type": [
        Link("superblocks", "Live filesystems of this type.", _fs_type_supers,
             origin="fs_type->fs_supers (s_instances)",
             userspace="findmnt -t <name>"),
        Link("module", "The module providing this filesystem.",
             lambda f: f.owner,
             applies=lambda f: f.owner.value_() != 0,
             origin="fs_type->owner",
             userspace="lsmod"),
    ],
    "irq_desc": [
        Link("actions", "The handlers registered on this line.", _irq_actions,
             applies=lambda d: d.action.value_() != 0,
             origin="walks desc->action, chained by action->next",
             userspace="cat /proc/interrupts  # the name column"),
        Link("irq_chip", "The controller that masks, acks and routes this line.",
             lambda d: d.irq_data.chip,
             origin="desc->irq_data.chip",
             userspace="cat /proc/interrupts  # the chip column"),
        Link("irq_data", "Per-line controller state: hwirq, domain, chip_data.",
             lambda d: d.irq_data.address_of_(),
             origin="&desc->irq_data"),
    ],
    "irqaction": [
        Link("irq_desc", "The line this handler is bound to.", _action_desc,
             origin="irq_to_desc(action->irq)",
             userspace="cat /proc/interrupts"),
        Link(
            "handler thread",
            "The kthread that runs the threaded half of this handler.",
            lambda a: a.thread,
            applies=lambda a: a.thread.value_() != 0,
            origin="action->thread",
            userspace="ps -e | grep irq/",
        ),
        Link("next action", "The next handler on this shared line.",
             lambda a: a.next,
             applies=lambda a: a.next.value_() != 0,
             origin="action->next"),
    ],
    "worker_pool": [
        Link("workers", "The workers attached to this pool.", _pool_workers,
             origin="walks pool->workers (worker->node)",
             userspace="ps -e | grep kworker"),
        Link("queued work", "Work items waiting for a worker in this pool.",
             _pool_worklist,
             origin="walks pool->worklist (work_struct->entry)"),
    ],
    "worker": [
        Link("task", "The kthread this worker runs as.", lambda w: w.task,
             # A BH pool's worker has none: its work runs in softirq context.
             applies=lambda w: ct.safe(lambda: w.task.value_(), 0) != 0,
             origin="worker->task",
             userspace="ps -o comm= -p <pid>"),
        Link("pool", "The pool this worker belongs to.", lambda w: w.pool,
             origin="worker->pool"),
        Link("current work", "The work item this worker is running.",
             lambda w: w.current_work,
             applies=lambda w: w.current_work.value_() != 0,
             origin="worker->current_work"),
        Link("current workqueue", "The workqueue the running item came from.",
             lambda w: w.current_pwq.wq,
             applies=lambda w: w.current_pwq.value_() != 0,
             origin="worker->current_pwq->wq"),
    ],
    "workqueue_struct": [
        Link("pool_workqueues", "The per-CPU halves of this queue and their pools.",
             _wq_pwqs,
             origin="walks wq->pwqs (pwqs_node)"),
        Link(
            "rescuer",
            "The thread that runs this queue's work when no worker can be "
            "created, which is why a WQ_MEM_RECLAIM queue can make progress "
            "under memory pressure.",
            lambda w: w.rescuer.task,
            applies=lambda w: w.rescuer.value_() != 0,
            origin="wq->rescuer->task",
            userspace="ps -e | grep -- '-rescuer'",
        ),
    ],
    "pool_workqueue": [
        Link("pool", "The worker pool that runs this half's work.", lambda p: p.pool,
             origin="pwq->pool"),
        Link("workqueue", "The workqueue this half belongs to.", lambda p: p.wq,
             origin="pwq->wq"),
        Link("inactive work", "Work held back by max_active.", _pwq_inactive,
             origin="walks pwq->inactive_works (work_struct->entry)"),
    ],
    "mutex": [
        Link("owner", "The task holding this mutex.", mutex_owner,
             applies=lambda m: ct.safe(lambda: mutex_owner(m).value_(), 0) != 0,
             origin="mutex_owner(): mutex->owner with its flag bits masked off",
             userspace="no userspace equivalent"),
        Link("waiters", "Tasks queued for this mutex.", _mutex_waiters,
             applies=lambda m: m.first_waiter.value_() != 0,
             origin="walks mutex->first_waiter and the waiter list"),
    ],
    "mutex_waiter": [
        Link("task", "The task waiting here.", lambda w: w.task,
             origin="waiter->task"),
    ],
    "rw_semaphore": [
        Link("owner", "The task holding this rwsem for writing.", rwsem_owner,
             # Hidden when the rwsem is unlocked: the field survives a reader's
             # release outside debug builds, and a stale reader is not an owner.
             applies=lambda s: _rwsem_locked(s)
             and ct.safe(lambda: rwsem_owner(s).value_(), 0) != 0,
             origin="rwsem_owner(): sem->owner with its flag bits masked off"),
    ],
    "futex_q": [
        Link("task", "The task parked on this futex.", lambda q: q.task,
             origin="futex_q->task",
             userspace="cat /proc/<pid>/stack, or gdb -p <pid>"),
        Link("pi_state", "Priority-inheritance state, for a PI futex.",
             lambda q: q.pi_state,
             applies=lambda q: q.pi_state.value_() != 0,
             origin="futex_q->pi_state"),
    ],
    "rcu_data": [
        Link("rcu_node", "The node this CPU reports quiescent states to.",
             lambda d: d.mynode,
             origin="rcu_data->mynode"),
    ],
    "rcu_node": [
        Link("parent", "The node above this one in the tree.",
             lambda n: n.parent,
             applies=lambda n: n.parent.value_() != 0,
             origin="rcu_node->parent"),
    ],
    "hrtimer": [
        Link("clock base", "The per-CPU queue this timer is on.",
             lambda t: t.base,
             origin="hrtimer->base"),
        Link(
            "sleeping task",
            "The task this timer will wake. Only set for a timer armed by a "
            "sleep or a timeout, whose callback is hrtimer_wakeup.",
            _hrtimer_sleeper_task,
            applies=_timer_func(HRTIMER_WAKEUP),
            origin="container_of(timer, struct hrtimer_sleeper, timer)->task",
            userspace="ps -o wchan= -p <pid>",
        ),
    ],
    "hrtimer_clock_base": [
        Link("queued timers", "The timers on this base, in expiry order.",
             _clock_base_timers,
             origin="walks base->active (timerqueue red-black tree)"),
        Link("cpu base", "The CPU-wide hrtimer state this base belongs to.",
             lambda b: b.cpu_base,
             origin="base->cpu_base"),
    ],
    "hrtimer_cpu_base": [
        Link("clock bases", "The per-clock queues on this CPU.",
             _cpu_base_clock_bases,
             origin="cpu_base->clock_base[]"),
        Link("next timer", "The timer this CPU is programmed to fire next.",
             lambda b: b.next_timer,
             applies=lambda b: b.next_timer.value_() != 0,
             origin="cpu_base->next_timer"),
    ],
    "timer_list": [
        Link(
            "delayed work",
            "The work item this timer will queue. Only set for a timer armed "
            "by schedule_delayed_work, whose callback is delayed_work_timer_fn.",
            _delayed_work,
            applies=_timer_func(DELAYED_WORK_TIMER),
            origin="container_of(timer, struct delayed_work, timer)->work",
        ),
        Link("workqueue", "The queue that delayed work will be put on.",
             _delayed_work_queue,
             applies=_timer_func(DELAYED_WORK_TIMER),
             origin="container_of(timer, struct delayed_work, timer)->wq"),
    ],
    "timer_base": [
        Link("pending timers", "Timers in this base's wheel buckets.",
             _wheel_base_timers,
             origin="walks base->vectors[] (hlist per bucket)"),
        Link("running timer", "The timer whose callback is running now.",
             lambda b: b.running_timer,
             applies=lambda b: b.running_timer.value_() != 0,
             origin="base->running_timer"),
    ],
    "tick_device": [
        Link("clock event device", "The device this CPU takes its tick from.",
             lambda d: d.evtdev,
             applies=lambda d: d.evtdev.value_() != 0,
             origin="tick_device->evtdev",
             userspace="cat /sys/devices/system/clockevents/clockevent0/current_device"),
    ],
    "pci_dev": [
        Link("irq line", "The interrupt descriptor for this device's irq.",
             _pci_irq_desc,
             applies=lambda d: int(d.irq) != 0,
             origin="irq_to_desc(dev->irq)",
             userspace="cat /proc/interrupts"),
    ],
    "net_device": [
        Link("namespace", "The net namespace owning this device.", lambda d: d.nd_net.net,
             origin="dev->nd_net.net",
             userspace="readlink /proc/self/ns/net"),
        Link("netdev_ops", "Device operations table.", lambda d: d.netdev_ops,
             origin="dev->netdev_ops"),
    ],
}


DERIVED: dict[str, list[Derived]] = {
    "kern_ipc_perm": [
        Derived("= key", "What a process passes to msgget/semget/shmget.", _ipc_key),
        Derived("= owner", "Who owns this object, and who created it.", _ipc_owner),
        Derived("= mode", "The permission bits, and who they let in.", _ipc_mode),
    ],
    "ipc_namespace": [
        Derived("= holds", "What exists in this namespace.", _ipc_ns_counts),
    ],
    "msg_queue": [
        Derived("= usage", "Messages queued, and the byte budget a sender waits on.",
                _msg_usage),
        Derived("= last sender", "The process that last sent to this queue.",
                lambda q: _ipc_pid_label(q.q_lspid)),
    ],
    "sem_array": [
        Derived("= values", "The current value of each semaphore.", _sem_values),
    ],
    "sem": [
        Derived("= last operation by", "The process whose semop(2) last changed it.",
                lambda s: _ipc_pid_label(s.sempid)),
    ],
    "sem_queue": [
        Derived("= waiting for", "What this blocked task asked semop(2) to do.",
                _sem_queue_operations),
    ],
    "shmid_kernel": [
        Derived("= size", "How much memory this segment covers.", _shm_pages),
        Derived("= creator", "The process that called shmget(2).",
                lambda s: _ipc_pid_label(s.shm_cprid)),
    ],
    "msg_msg": [
        Derived("= message", "The type a receiver selects on, and the size.",
                lambda m: f"type {ct.safe(lambda: int(m.m_type), 0)}, "
                          f"{ct.safe(lambda: int(m.m_ts), 0)} bytes"),
    ],

    "gendisk": [
        Derived("= capacity", "How much the whole disk holds.", _disk_capacity),
    ],
    "request_queue": [
        Derived("= path", "Whether I/O here becomes requests or stays bios.",
                _queue_path),
        Derived("= elevator", "The I/O scheduler sorting this queue.",
                _queue_elevator),
    ],
    "blk_mq_hw_ctx": [
        Derived("= tags in use", "Requests this hardware queue is working on.",
                _hctx_tags),
    ],
    "request": [
        Derived("= transfer", "The operation, its size, and where on the device.",
                _request_target),
    ],
    "bio": [
        Derived("= transfer", "The operation, its size, and where it is going.",
                _bio_target),
    ],
    "block_device": [
        Derived("= dev_t", "The major:minor a /dev node names this by.",
                _bdev_devt),
    ],
    "mutex": [
        Derived("= state", "Whether this mutex is held, and by whom.", _mutex_state),
    ],
    "rw_semaphore": [
        Derived("= state", "Unlocked, read-locked or write-locked.", _rwsem_state),
    ],
    "futex_q": [
        Derived("= futex key", "The address this futex is keyed on.", _futex_key),
    ],
    "rcu_data": [
        Derived("= callbacks", "Callbacks queued on this CPU.", _rcu_callbacks),
    ],
    "hrtimer": [
        Derived("= expires in", "Time until this timer fires.", _hrtimer_expires_in),
        Derived("= function", "The callback this timer will run.",
                lambda t: _symbol_name(t, t.function)),
        Derived("= runs in", "Which context the callback runs in.", _hrtimer_mode),
    ],
    "timer_list": [
        Derived("= expires in", "Jiffies until this timer fires, and in ms.",
                _timer_expires_in),
        Derived("= function", "The callback this timer will run.",
                lambda t: _symbol_name(t, t.function)),
    ],
    "clocksource": [
        Derived("= resolution", "Nanoseconds per counter cycle.",
                _clocksource_resolution),
    ],
    "clock_event_device": [
        Derived("= state", "Whether this device is armed, and how.",
                _clock_event_state),
        Derived("= event handler", "The function the device's interrupt calls.",
                _clock_event_handler),
    ],
    "irq_desc": [
        Derived("= count", "Times this line has fired.", _irq_count),
        Derived("= hwirq", "The controller's number for this line.", _irq_hwirq),
    ],
    "irqaction": [
        Derived("= handler",
                "The function called in interrupt context for this action.",
                lambda a: ct.safe(lambda: a.prog_.symbol(a.handler).name, "?")),
    ],
    "worker_pool": [
        Derived("= runs work in", "Which context this pool's work runs in.",
                _pool_context),
    ],
    "worker": [
        Derived("= doing", "What this worker is running, or last ran.",
                _worker_current),
    ],
    "workqueue_struct": [
        Derived("= flags", "The public wq_flags bits set on this queue.", _wq_flags),
    ],
    "work_struct": [
        Derived("= func", "The function that will run for this work item.",
                _work_func),
    ],
    "softirq_action": [
        Derived("= handler", "The function this softirq vector runs.",
                lambda a: ct.safe(lambda: a.prog_.symbol(a.action).name, "(none)")),
    ],
    "task_struct": [
        Derived(
            "= role (pid vs tgid)",
            "pid is this thread's id, tgid its group's. Equal means this task "
            "leads the group -- and a group is what userspace calls a process.",
            _task_role,
        ),
        Derived(
            "= user (cred->uid.val)",
            "kuid_t wraps a single uid_t, so the scalar is task->cred->uid.val. "
            "The name comes from the local passwd file; the kernel stores only ids.",
            _task_user,
        ),
        Derived(
            "= group (cred->gid.val)",
            "task->cred->gid.val, and egid when it differs.",
            _task_group,
        ),
        Derived(
            "= user namespace (cred->user_ns)",
            "Which user_ns these ids are relative to.",
            _user_ns_label,
        ),
    ],
    "cred": [
        Derived("= uid (uid.val)", "Real user id.", lambda c: _id_label(c.uid.val.value_())),
        Derived(
            "= euid (euid.val)",
            "Effective user id -- what permission checks use.",
            lambda c: _id_label(c.euid.val.value_()),
        ),
        Derived(
            "= suid (suid.val)",
            "Saved user id -- what setuid() may restore.",
            lambda c: _id_label(c.suid.val.value_()),
        ),
        Derived(
            "= fsuid (fsuid.val)",
            "Filesystem user id -- used for file access checks.",
            lambda c: _id_label(c.fsuid.val.value_()),
        ),
        Derived(
            "= gid / egid",
            "Real and effective group ids.",
            lambda c: f"{c.gid.val.value_()} / {c.egid.val.value_()}",
        ),
    ],
    "page": [
        Derived("= pfn", "Page frame number: index into the vmemmap array.",
                lambda p: page_to_pfn(p).value_()),
        Derived("= physical address", "Physical address derived by page_to_phys().",
                lambda p: hex(page_to_phys(p).value_())),
        Derived("= size", "Page size, accounting for compound pages.",
                lambda p: page_size(p).value_()),
        Derived("= flags", "PG_* flags decoded from page->flags.", decode_page_flags),
        Derived("= refcount", "_refcount: how many references pin this page.",
                lambda p: p._refcount.counter.value_()),
    ],
    "sk_buff": [
        Derived("= len", "Total packet length, including paged data.",
                lambda s: s.len.value_()),
        Derived("= data_len", "Bytes held in frags rather than the linear area.",
                lambda s: s.data_len.value_()),
        Derived("= headroom", "data - head: room to push headers.",
                lambda s: s.data.value_() - s.head.value_()),
        Derived("= tailroom", "end - tail: room to append payload.",
                lambda s: s.end.value_() - s.tail.value_()),
        Derived("= truesize", "Total memory charged to the socket for this skb.",
                lambda s: s.truesize.value_()),
        Derived(
            "= device",
            "skb->dev is a union with dev_scratch: once queued on a socket the "
            "device would be NULL, so the receive path reuses the storage.",
            _skb_device,
        ),
    ],
    "kmem_cache": [
        Derived("= name", "cache->name, a const char * -- the slabinfo name.",
                lambda c: as_text(c.name.string_())),
        Derived("= name string lives in", "Kernel rodata for built-in caches; "
                "kstrdup'd heap for module ones.", lambda c: _name_storage(c)),
        Derived("= object size", "Size of one object, before SLUB padding.",
                lambda c: c.object_size.value_()),
        Derived("= slab size", "Per-object allocation size including SLUB metadata and alignment.",
                lambda c: c.size.value_()),
        Derived("= order", "Page allocator order backing each slab.", slab_cache_order),
        Derived("= objects per slab", "How many objects fit in one slab.",
                slab_cache_objects_per_slab),
        Derived("= merged", "SLUB merges compatible caches; then names are aliases.",
                slab_cache_is_merged),
    ],
    "vm_area_struct": [
        Derived("= range", "Virtual address range this VMA covers.",
                lambda v: f"{v.vm_start.value_():#x}-{v.vm_end.value_():#x}"),
        Derived("= size", "Length of the mapping in bytes.",
                lambda v: v.vm_end.value_() - v.vm_start.value_()),
        Derived("= name", "Backing file or anonymous.",
                lambda v: as_text(vma_name(v)) if vma_name(v) else "anon"),
    ],
}


def derived_for(obj: Object) -> list[Derived]:
    """Computed rows for ``obj``, or none if it isn't a single tagged struct.

    Arrays and scalars have no tag -- an array of structs is not itself a
    struct, so it gets no derived rows.
    """
    return DERIVED.get(ct.tag_of(obj.type_) or "", [])


def userspace_for(link: Link, obj: Object) -> str:
    """The link's command, with whatever this struct can fill in filled in.

    The rules live in ``userspace.placeholders`` and are shared with the field
    commands, so a struct file resolves <pid> the same way whether the command
    is on a link or on a field.
    """
    if not link.userspace:
        return "no userspace equivalent"
    if "<" not in link.userspace:
        return link.userspace
    from .userspace import fill, placeholders

    return fill(link.userspace, placeholders(obj, ct.tag_of(obj.type_) or ""))


def links_for(obj: Object) -> list[Link]:
    """Curated edges out of ``obj``, or none if we have no map for its type.

    Accepts a struct or a pointer to one: resolvers are always handed a
    pointer, because drgn's Linux helpers require one and member access works
    the same either way. Arrays and scalars have no tag and get no links.
    """
    return LINKS.get(ct.tag_of(obj.type_) or "", [])
