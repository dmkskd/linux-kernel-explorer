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
from dataclasses import dataclass
from typing import Callable, Iterator

from drgn import Object, TypeKind, cast

from drgn.helpers.linux.fs import mount_dst
from drgn.helpers.linux.list import hlist_for_each_entry, list_for_each_entry
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
from drgn.helpers.linux.rbtree import rbtree_inorder_for_each_entry
from drgn.helpers.linux.slab import (
    slab_cache_for_each_allocated_object,
    slab_cache_is_merged,
    slab_cache_objects_per_slab,
    slab_cache_order,
)
from drgn.helpers.linux.pid import for_each_task_in_group
from drgn.helpers.linux.sched import task_rq, task_state_to_char

from ..core import ctypes as ct
from .format import as_text, task_comm
from .walk import files_of, path_of

# A resolver returns one object or an iterable of labelled objects.
Resolver = Callable[[Object], "Object | Iterator[tuple[str, Object]]"]

# include/linux/page-flags.h: page->mapping is a tagged pointer. Bit 0 says the
# rest of it is an anon_vma rather than the file's address_space.
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
    for vma in rbtree_inorder_for_each_entry(
        "struct vm_area_struct", space.i_mmap.rb_root.address_of_(), "shared.rb"
    ):
        yield vma


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


LINKS: dict[str, list[Link]] = {
    "task_struct": [
        Link("threads", "Every task sharing this thread group.", _threads,
             origin="task->signal->thread_head (thread_node)",
             userspace="ls /proc/<pid>/task, or ps -L -p <pid>"),
        Link("mm (address space)", "The mm_struct this task's memory lives in.",
             lambda t: t.mm,
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
             origin="walks task->mm->mm_mt (maple tree)",
             userspace="cat /proc/<pid>/maps"),
        Link("open files", "fd table: struct file per descriptor.", _open_files,
             origin="walks task->files->fdt->fd[]",
             userspace="ls -l /proc/<pid>/fd, or lsof -p <pid>"),
        Link("sockets", "Just the fds that are sockets, as struct socket.", _sockets,
             origin="task->files->fdt->fd[], f_op==socket_file_ops",
             userspace="ss -tanp | grep pid=<pid>"),
        Link("children", "Tasks this one forked, linked by sibling.", _children,
             origin="walks task->children, linked by sibling",
             userspace="pgrep -P <pid>"),
        Link("parent", "The real parent task.", lambda t: t.real_parent,
             origin="task->real_parent",
             userspace="ps -o ppid= -p <pid>"),
        Link("runqueue", "The struct rq this task is queued on.", task_rq,
             origin="task_rq() = cpu_rq(task_cpu(task))",
             userspace="ps -o psr= -p <pid> gives the CPU, not the rq"),
        Link("sched_entity", "CFS scheduling state (vruntime, load).",
             lambda t: t.se.address_of_(),
             origin="&task->se",
             userspace="cat /proc/<pid>/sched (sched_schedstats=1)"),
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
        ),
        Link("namespaces", "The nsproxy's namespaces.", _namespaces,
             origin="task->nsproxy members",
             userspace="ls -l /proc/<pid>/ns"),
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
        Link("cfs_rq (embedded root)", "rq embeds one cfs_rq directly; this is it.",
             lambda r: r.cfs.address_of_(),
             origin="&rq->cfs (member, not a pointer)"),
        Link("all cfs_rqs on this CPU", "One per cgroup, chained on leaf_cfs_rq_list.",
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
             userspace="the cgroup path under /sys/fs/cgroup"),
        Link("curr (running entity)", "The entity currently on the CPU here.",
             lambda c: c.curr, applies=lambda c: c.curr.value_() != 0,
             origin="cfs_rq->curr"),
    ],
    "sched_entity": [
        Link(
            "cfs_rq it sits on",
            "The queue this entity is queued in.",
            lambda se: se.cfs_rq,
            applies=lambda se: se.cfs_rq.value_() != 0,
            origin="se->cfs_rq",
        ),
        Link(
            "my_q (queue it owns)",
            "Group entities own a child cfs_rq; task entities do not, and this "
            "is how pick_next_entity descends the hierarchy.",
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
        Link("VMAs", "Every mapped region in this address space.",
             lambda m: ((f"{v.vm_start.value_():#x}", v) for v in for_each_vma(m)),
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
             userspace="the path column of /proc/<pid>/maps"),
        Link(
            "resident pages",
            "Physical pages actually backing this VMA, via page table walk.",
            lambda v: _vma_pages(v),
            origin="page table walk (follow_page)",
            userspace="/proc/<pid>/smaps shows Rss per mapping",
        ),
        Link("anon_vma", "Reverse mapping for anonymous pages.", lambda v: v.anon_vma,
             origin="vma->anon_vma"),
    ],
    "kmem_cache": [
        Link(
            "allocated objects",
            "Live objects in this cache, found by walking its slabs.",
            lambda c: (
                (f"{o.value_():#x}", o)
                for o in slab_cache_for_each_allocated_object(c, "void *")
            ),
            origin="walks the cache's slabs",
            userspace="slabtop, or /proc/slabinfo",
        ),
        Link("next cache", "The following entry in the global slab_caches list.",
             lambda c: cast("struct kmem_cache *", c.list.next),
             origin="cache->list.next, container_of",
             userspace="/proc/slabinfo lists them all"),
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
            userspace="/proc/zoneinfo describes the zones, not one page's",
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
             userspace="/proc/zoneinfo groups its zones under Node N"),
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
            "Every non-NULL entry of fd[], bounded by max_fds.",
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
        Link("sk (protocol half)", "struct sock: where the protocol state lives.",
             lambda s: s.sk,
             origin="socket->sk",
             userspace="ss -tanie"),
        Link("file", "The struct file this socket is exposed through.", lambda s: s.file,
             origin="socket->file"),
        Link("ops", "proto_ops: the protocol's VFS-facing operations.", lambda s: s.ops,
             origin="socket->ops"),
    ],
    "sock": [
        Link("socket (VFS half)", "Back to struct socket, if this has an fd.",
             lambda s: s.sk_socket,
             origin="sk->sk_socket"),
        Link("proto", "struct proto: tcp_prot, udp_prot, unix_stream_proto…",
             lambda s: s.sk_prot,
             origin="sk->sk_prot",
             userspace="ss -tani shows the protocol per socket"),
        Link(
            "as tcp_sock",
            "struct sock is the first member of tcp_sock, so this is a cast.",
            lambda s: cast("struct tcp_sock *", s),
            applies=lambda s: s.sk_protocol == IPPROTO_TCP,
            origin="cast, same address",
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
             userspace="findmnt, or /proc/self/mountinfo"),
        Link("mountpoint", "The dentry this is mounted on.", lambda m: m.mnt_mountpoint,
             origin="mount->mnt_mountpoint",
             userspace="findmnt -T <path>"),
        Link("parent mount", "The mount this is mounted under.", lambda m: m.mnt_parent,
             origin="mount->mnt_parent",
             userspace="findmnt shows the tree"),
    ],
    "super_block": [
        Link("root dentry", "Root of this filesystem.", lambda s: s.s_root,
             origin="sb->s_root",
             userspace="findmnt"),
        Link("fs type", "file_system_type describing it.", lambda s: s.s_type,
             origin="sb->s_type",
             userspace="findmnt -o FSTYPE"),
        Link("mounts", "Every place this filesystem is mounted.", _sb_mounts,
             applies=lambda s: _has_member(s, "s_mounts"),
             origin="sb->s_mounts",
             userspace="findmnt -o TARGET --source <device>"),
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
    "net_device": [
        Link("namespace", "The net namespace owning this device.", lambda d: d.nd_net.net,
             origin="dev->nd_net.net",
             userspace="ip netns identify"),
        Link("netdev_ops", "Device operations table.", lambda d: d.netdev_ops,
             origin="dev->netdev_ops"),
    ],
}


DERIVED: dict[str, list[Derived]] = {
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
        Derived("= physical address", "Where this page actually is in RAM.",
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
        Derived("= slab size", "Size actually consumed per object.",
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


def userspace_for(link: "Link", obj: Object) -> str:
    """The link's userspace command, with <pid> filled in where we know it."""
    if not link.userspace:
        return "no userspace equivalent"
    if "<pid>" not in link.userspace:
        return link.userspace

    # Ask the type, not the exception: obj.pid raises AttributeError on a
    # struct file or mount, and ct.safe deliberately does not catch that.
    tag = ct.tag_of(obj.type_)
    pid = None
    if tag == "task_struct":
        pid = ct.safe(lambda: obj.pid.value_(), None)
    elif tag == "mm_struct":
        owner = ct.safe(lambda: obj.owner, None)
        if owner is not None and ct.safe(lambda: owner.value_(), 0):
            pid = ct.safe(lambda: owner.pid.value_(), None)

    if pid is None:
        # Keep the placeholder rather than inventing a pid: the command is
        # still correct, it just has to be filled in by hand.
        return link.userspace
    return link.userspace.replace("<pid>", str(pid))


def links_for(obj: Object) -> list[Link]:
    """Curated edges out of ``obj``, or none if we have no map for its type.

    Accepts a struct or a pointer to one: resolvers are always handed a
    pointer, because drgn's Linux helpers require one and member access works
    the same either way. Arrays and scalars have no tag and get no links.
    """
    return LINKS.get(ct.tag_of(obj.type_) or "", [])
